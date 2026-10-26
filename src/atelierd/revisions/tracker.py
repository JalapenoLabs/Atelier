"""Keeping a project's revisions current in the background: recording after changes, rendering standard views.

`RevisionTracker` owns two thread pools: one thread records revisions in order (hashing and copying files), and a
small pool renders standard views in headless Blender, at most `STANDARD_RENDER_WORKERS` at a time and each at low
priority, so renders never crowd out the machine. Everything it finishes is reported as a `RevisionEvent` on one of
those threads.

It is fed by the catalog watcher's settled changes (`handle_catalog_event`), and called directly by whatever else
changes a source: a worker turn ends (`record`), or a reviewer restores (`restore`). The window asks it for the
renders of whatever it shows that has none yet (`request_standard_renders`), which is how a baseline's exports get
rendered.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from atelierd.catalog import CatalogEvent
from atelierd.constants import STANDARD_RENDER_WORKERS
from atelierd.revisions.models import (
    RenderState,
    RestoreResult,
    Revision,
    RevisionEvent,
    RevisionEventKind,
    RevisionTrigger,
    SourceHistory,
)
from atelierd.revisions.service import Revisions

logger = logging.getLogger(__name__)


class RevisionTracker:
    """Records revisions and renders their standard views in the background for one project."""

    def __init__(
        self,
        revisions: Revisions,
        on_event: Callable[[RevisionEvent], None],
        render_workers: int = STANDARD_RENDER_WORKERS,
    ) -> None:
        self.revisions = revisions
        self._on_event = on_event
        self._record_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="revisions")
        self._render_executor = ThreadPoolExecutor(max_workers=render_workers, thread_name_prefix="render")
        self._pending_guard = threading.Lock()
        # (revision id, export path) of every render queued or running.
        self._pending: set[tuple[int, str]] = set()

    def start(self) -> None:
        """Catch up in the background: record every source that changed while nothing watched, or has no revision."""
        self._submit_record(self._catch_up)

    def stop(self) -> None:
        """Stop taking work and drop what has not started; a running Blender finishes on its own."""
        self._record_executor.shutdown(wait=False, cancel_futures=True)
        self._render_executor.shutdown(wait=False, cancel_futures=True)

    # ####################### #
    #     CORE ENTRY POINTS   #
    # ####################### #

    def record(self, source_path: str, trigger: RevisionTrigger) -> Revision | None:
        """Snapshot a source now and render its changed exports in the background.

        Returns the new revision, or None when the source's files are exactly its newest revision's. Blocks while
        the files are hashed and copied. A worker calls this after each turn, with `RevisionTrigger.WORKER_TURN`.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        revision = self.revisions.record(source_path, trigger)
        if revision:
            self._after_recording(revision)
        return revision

    def restore(self, source_path: str, revision_id: int) -> RestoreResult:
        """Restore a source to an earlier revision (see `Revisions.restore`). Blocks while the files are written.

        Raises:
            SourceBusyError: a worker is running on the source.
            KeyError: the source is not present, or the revision does not exist.
            ValueError: the revision belongs to another source.
        """
        result = self.revisions.restore(source_path, revision_id)
        for revision in {result.pre_restore.id: result.pre_restore, result.restored.id: result.restored}.values():
            self._after_recording(revision)
        return result

    def handle_catalog_event(self, event: CatalogEvent) -> None:
        """Record, in the background, every source a settled change touched, added or reshaped."""
        source_paths = sorted({*event.touched, *event.change.added, *event.change.changed})
        for source_path in source_paths:
            self._submit_record(partial(self._record_in_background, source_path))

    def request_standard_renders(self, revision_id: int, export_path: str) -> None:
        """Render one export of a revision in the background, unless its set exists, failed, or is on its way."""
        with self._pending_guard:
            if (revision_id, export_path) in self._pending:
                return
        revision = self.revisions.get_revision(revision_id)
        render = next((item for item in self.revisions.standard_renders(revision) if item.export == export_path), None)
        if render is None:
            raise KeyError(f"Revision {revision_id} has no export '{export_path}'")
        if render.state is RenderState.MISSING:
            self._queue_render(revision, export_path)

    def history(self, source_path: str) -> SourceHistory:
        """Return a source's revisions and standard renders, with renders queued or running marked pending."""
        history = self.revisions.history(source_path)
        with self._pending_guard:
            pending = set(self._pending)
        render_sets = tuple(
            render.model_copy(update={"state": RenderState.PENDING})
            if render.state is RenderState.MISSING and (render.revision_id, render.export) in pending
            else render
            for render in history.standard_renders
        )
        return history.model_copy(update={"standard_renders": render_sets})

    # -----------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------

    def _catch_up(self) -> None:
        for revision in self.revisions.catch_up():
            self._after_recording(revision)

    def _record_in_background(self, source_path: str) -> None:
        try:
            self.record(source_path, RevisionTrigger.CHANGE)
        except KeyError:
            # Removed by the same settled change: its .blend is gone, so there is nothing to snapshot.
            logger.debug(f"[Tracker] {source_path} is no longer present, recording nothing")

    def _after_recording(self, revision: Revision) -> None:
        """Report a new revision and queue the renders it needs."""
        self._on_event(RevisionEvent(kind=RevisionEventKind.RECORDED, source=revision.source, revision_id=revision.id))
        for export_path in self.revisions.exports_to_render(revision):
            self._queue_render(revision, export_path)

    def _queue_render(self, revision: Revision, export_path: str) -> None:
        key = (revision.id, export_path)
        with self._pending_guard:
            if key in self._pending:
                return
            self._pending.add(key)

        def render() -> None:
            try:
                self.revisions.render_standard(revision.id, export_path)
            finally:
                with self._pending_guard:
                    self._pending.discard(key)
            self._on_event(
                RevisionEvent(kind=RevisionEventKind.RENDERED, source=revision.source, revision_id=revision.id)
            )

        self._submit(self._render_executor, render, f"rendering {export_path} for revision {revision.id}")

    def _submit_record(self, work: Callable[[], None]) -> None:
        self._submit(self._record_executor, work, "recording revisions")

    @staticmethod
    def _submit(executor: ThreadPoolExecutor, work: Callable[[], None], description: str) -> None:
        """Run work on a pool, logging any failure, which a future nobody waits on would otherwise swallow."""

        def guarded() -> None:
            try:
                work()
            except Exception as error:
                logger.exception(f"[Tracker] Failed {description}: {error}")

        try:
            executor.submit(guarded)
        except RuntimeError:
            logger.debug(f"[Tracker] Stopped; not {description}")
