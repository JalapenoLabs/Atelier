"""The revision use cases every front door calls: record, list, restore, prune, and standard renders.

`Revisions` is synchronous: each call does its work (hashing, copying, Blender) and returns. `RevisionTracker`
(`tracker.py`) runs it in the background for the watcher and the window. Each call opens its own stores, so one
instance is safe to share across threads; a per-source lock keeps a restore and a recording of the same source from
interleaving within the process, and the object store's lock keeps a prune from deleting objects a recording is
about to name, across processes too.

What a revision holds is a source's files as the catalog knows them: its `.blend`, builder, README, exports and
renders. A restore writes them back, except files shared with other sources: anything matching `shared` in
`atelier.toml`, and a builder outside the source's own folder (a README's rebuild command running a project-wide
script). Those are snapshotted, so a compare still shows them, but never written by a restore.
"""

from __future__ import annotations

import logging
import shutil
import threading
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath

from wcmatch import glob

from atelierd.catalog.discovery import GLOB_FLAGS
from atelierd.catalog.models import Source
from atelierd.catalog.store import CatalogStore
from atelierd.config import load_config
from atelierd.exports import ConvertedExport, convert_file
from atelierd.project import Project
from atelierd.revisions.models import (
    FileKind,
    PruneResult,
    RenderState,
    RestoreMark,
    RestoreResult,
    Revision,
    RevisionFile,
    RevisionTrigger,
    SourceHistory,
    StandardRenderSet,
)
from atelierd.revisions.objects import ObjectStore
from atelierd.revisions.renders import (
    RENDERS_DIR_NAME,
    link_render_set,
    read_render_set,
    render_names,
    render_set,
)
from atelierd.revisions.store import RevisionStore

logger = logging.getLogger(__name__)

OBJECTS_DIR_NAME = "objects"


class SourceBusyError(Exception):
    """A restore was asked for while a worker is running on the source."""


def _no_source_is_busy(source_path: str) -> bool:
    """Until workers exist, nothing else ever writes a source's files on Atelier's behalf."""
    return False


class Revisions:
    """One project's revisions."""

    def __init__(self, project: Project, is_source_busy: Callable[[str], bool] = _no_source_is_busy) -> None:
        """Create the service.

        Args:
            project: the project whose revisions these are.
            is_source_busy: whether a worker is running on a source; a restore is refused while one is.
        """
        self.project = project
        self.objects = ObjectStore(project.state_dir / OBJECTS_DIR_NAME)
        self.renders_dir = project.state_dir / RENDERS_DIR_NAME
        self._is_source_busy = is_source_busy
        self._locks_guard = threading.Lock()
        self._source_locks: dict[str, threading.Lock] = {}

    # ####################### #
    #        RECORDING        #
    # ####################### #

    def record(
        self,
        source_path: str,
        trigger: RevisionTrigger,
        restored_from: int | None = None,
    ) -> Revision | None:
        """Snapshot a source's files now, and return the new revision, or None when nothing changed since the last.

        This is the call to make after anything that may have changed a source, such as a worker turn.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        with self._source_lock(source_path):
            return self._record_locked(source_path, trigger, restored_from)

    def catch_up(self) -> list[Revision]:
        """Record every present source whose files differ from its newest revision, or that has none.

        A source with no revision gets a baseline; one that changed while nothing was watching gets a change.
        Returns the revisions recorded.
        """
        with CatalogStore(self.project.database_path) as catalog_store:
            sources = catalog_store.list_sources()

        recorded: list[Revision] = []
        for source in sources:
            with RevisionStore(self.project.database_path) as store:
                has_revision = store.latest_revision(source.path) is not None
            trigger = RevisionTrigger.CHANGE if has_revision else RevisionTrigger.BASELINE
            try:
                revision = self.record(source.path, trigger)
            except (KeyError, OSError) as error:
                logger.warning(f"[Revisions] Could not catch up on {source.path}: {error}")
                continue
            if revision:
                recorded.append(revision)
        logger.info(f"[Revisions] Caught up on {self.project.name}: {len(recorded)} revisions recorded")
        return recorded

    # ####################### #
    #          READS          #
    # ####################### #

    def get_revision(self, revision_id: int) -> Revision:
        """Return one revision.

        Raises:
            KeyError: there is no such revision.
        """
        with RevisionStore(self.project.database_path) as store:
            return store.get_revision(revision_id)

    def history(self, source_path: str) -> SourceHistory:
        """Return a source's revisions, newest first, with their standard renders and its restore mark."""
        with RevisionStore(self.project.database_path) as store:
            revisions = store.list_revisions(source_path)
            restore_mark = store.get_restore_mark(source_path)
        render_sets = [render for revision in revisions for render in self.standard_renders(revision)]
        return SourceHistory(
            source=source_path,
            revisions=tuple(revisions),
            standard_renders=tuple(render_sets),
            restore_mark=restore_mark,
        )

    def standard_renders(self, revision: Revision) -> list[StandardRenderSet]:
        """Return each of a revision's exports' standard render sets, as they stand on disk."""
        exports = revision.exports
        names = render_names([export.path for export in exports])
        folder = self.renders_dir / str(revision.id)
        return [
            read_render_set(folder, revision.id, export.path, export.sha256, names[export.path]) for export in exports
        ]

    def get_restore_mark(self, source_path: str) -> RestoreMark | None:
        """Return the revision a source was last restored to, until a worker turn has been told about it."""
        with RevisionStore(self.project.database_path) as store:
            return store.get_restore_mark(source_path)

    def clear_restore_mark(self, source_path: str) -> None:
        """Forget a source's restore, once a worker turn has been told about it."""
        with RevisionStore(self.project.database_path) as store:
            store.clear_restore_mark(source_path)

    def object_path(self, sha256: str) -> Path:
        """Return where a snapshotted file's bytes live."""
        return self.objects.path_of(sha256)

    def convert_revision_export(self, revision_id: int, export_path: str) -> ConvertedExport:
        """Return one of a revision's exports converted to `.glb`, from its snapshotted bytes. Blocks on Blender.

        Raises:
            KeyError: the revision does not exist or has no such export.
            ExportConversionError: Blender could not convert it.
        """
        export = self._revision_export(self.get_revision(revision_id), export_path)
        return convert_file(self.project, self.objects.path_of(export.sha256), export.sha256, export_path)

    # ####################### #
    #     STANDARD RENDERS    #
    # ####################### #

    def exports_to_render(self, revision: Revision) -> list[str]:
        """Return the exports a new revision needs rendered.

        Recording already shared every set another revision had for the same bytes. Of the exports still without
        one, only those that changed since the previous revision are rendered; a baseline's exports wait until
        someone asks for them.
        """
        with RevisionStore(self.project.database_path) as store:
            previous = next(
                (other for other in store.list_revisions(revision.source) if other.number == revision.number - 1),
                None,
            )
        previous_manifest = previous.manifest if previous else {}

        to_render: list[str] = []
        for render in self.standard_renders(revision):
            if render.state is not RenderState.MISSING:
                continue
            is_changed = previous_manifest.get(render.export) != render.export_sha256
            if revision.trigger is not RevisionTrigger.BASELINE and is_changed:
                to_render.append(render.export)
        return to_render

    def render_standard(self, revision_id: int, export_path: str) -> StandardRenderSet:
        """Render one export of a revision unless its set exists. Blocks for as long as Blender takes.

        The new set is shared with every other revision holding the same bytes.

        Raises:
            KeyError: the revision does not exist or has no such export.
        """
        revision = self.get_revision(revision_id)
        export = self._revision_export(revision, export_path)
        if self._render_set_of(revision, export_path).state is RenderState.MISSING:
            self._reuse_render_set(revision, export_path)
        existing = self._render_set_of(revision, export_path)
        if existing.state is not RenderState.MISSING:
            return existing

        name = render_names([file.path for file in revision.exports])[export_path]
        folder = self.renders_dir / str(revision.id)
        rendered = render_set(
            self.objects.path_of(export.sha256),
            self.project.root / PurePosixPath(export_path).parent,
            folder,
            revision.id,
            export_path,
            export.sha256,
            name,
        )

        # Every other revision whose export has these bytes would show the same pictures, so it gets them too.
        for holder, holder_set in self._sets_for_same_bytes(revision, export_path):
            if holder_set.state is RenderState.MISSING:
                holder_name = render_names([file.path for file in holder.exports])[export_path]
                link_render_set(folder, name, self.renders_dir / str(holder.id), holder_name)
        return rendered

    # ####################### #
    #         RESTORE         #
    # ####################### #

    def restore(self, source_path: str, revision_id: int) -> RestoreResult:
        """Write a whole earlier revision of a source back into the project, after snapshotting the current state.

        Files the revision did not have are deleted, so the source's files end up exactly the revision's, apart from
        shared files, which are left as they are. The source is then marked as restored.

        Raises:
            SourceBusyError: a worker is running on the source.
            KeyError: the source is not present, or the revision does not exist.
            ValueError: the revision belongs to another source.
        """
        if self._is_source_busy(source_path):
            raise SourceBusyError(f"{source_path} has a worker running; restore it once the worker is idle")

        with self._source_lock(source_path):
            target = self.get_revision(revision_id)
            if target.source != source_path:
                raise ValueError(f"Revision {revision_id} belongs to {target.source}, not {source_path}")
            source = self._present_source(source_path)

            self._record_locked(source_path, RevisionTrigger.PRE_RESTORE)
            with RevisionStore(self.project.database_path) as store:
                pre_restore = store.latest_revision(source_path)
            if pre_restore is None:
                raise RuntimeError(f"{source_path} has no revision after snapshotting it")

            written, deleted, skipped = self._write_revision(source, target, pre_restore)
            restored = self._record_locked(
                source_path,
                RevisionTrigger.RESTORE,
                restored_from=target.id,
                extra_files=target.files,
            )
            with RevisionStore(self.project.database_path) as store:
                store.set_restore_mark(source_path, target.id)

        logger.info(
            f"[Revisions] Restored {source_path} to r{target.number}: {len(written)} written, {len(deleted)} "
            f"deleted, {len(skipped)} shared files left alone"
        )
        return RestoreResult(
            target=target,
            pre_restore=pre_restore,
            restored=restored or pre_restore,
            written=tuple(written),
            deleted=tuple(deleted),
            skipped=tuple(skipped),
        )

    # ####################### #
    #          PRUNE          #
    # ####################### #

    def prune(self, keep: int) -> PruneResult:
        """Keep each source's newest `keep` revisions and delete the rest, with what no kept revision needs.

        A revision a source is marked restored to is kept too. Objects and render folders no kept revision names are
        deleted.

        Raises:
            ValueError: `keep` is below 1; a source's newest revision is always kept.
        """
        if keep < 1:
            raise ValueError(f"prune must keep at least one revision per source, not {keep}")

        with self.objects.exclusive_lock(), RevisionStore(self.project.database_path) as store:
            pruned_ids = store.revisions_beyond(keep)
            store.delete_revisions(pruned_ids)
            referenced = store.referenced_hashes()
            remaining_ids = store.revision_ids()

            objects_removed = 0
            bytes_freed = 0
            for sha256 in self.objects.list_hashes() - referenced:
                bytes_freed += self.objects.delete(sha256)
                objects_removed += 1
            # Leftovers of a copy that was interrupted; no writer is running while the lock is held.
            for partial_file in self.objects.objects_dir.glob("*.partial"):
                partial_file.unlink()

        render_folders_removed = 0
        if self.renders_dir.is_dir():
            for folder in self.renders_dir.iterdir():
                if folder.is_dir() and folder.name.isdigit() and int(folder.name) not in remaining_ids:
                    shutil.rmtree(folder)
                    render_folders_removed += 1

        result = PruneResult(
            revisions_removed=len(pruned_ids),
            objects_removed=objects_removed,
            bytes_freed=bytes_freed,
            render_folders_removed=render_folders_removed,
        )
        logger.info(f"[Revisions] Pruned {self.project.name} to {keep} revisions per source: {result}")
        return result

    # -----------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------

    def _source_lock(self, source_path: str) -> threading.Lock:
        with self._locks_guard:
            return self._source_locks.setdefault(source_path, threading.Lock())

    def _present_source(self, source_path: str) -> Source:
        with CatalogStore(self.project.database_path) as catalog_store:
            source = catalog_store.get_source(source_path)
        if source is None:
            raise KeyError(f"No present source '{source_path}'")
        return source

    def _record_locked(
        self,
        source_path: str,
        trigger: RevisionTrigger,
        restored_from: int | None = None,
        extra_files: Iterable[RevisionFile] = (),
    ) -> Revision | None:
        """Snapshot the source's files, plus `extra_files` the catalog may not list yet (a restore's), if present."""
        source = self._present_source(source_path)
        kind_by_path = _source_files(source)
        for extra_file in extra_files:
            kind_by_path.setdefault(extra_file.path, extra_file.kind)

        with self.objects.shared_lock(), RevisionStore(self.project.database_path) as store:
            files: list[RevisionFile] = []
            for path, kind in sorted(kind_by_path.items()):
                snapshot = self._snapshot_file(store, path, kind)
                if snapshot:
                    files.append(snapshot)
            revision = store.insert_revision_if_changed(source_path, files, trigger, restored_from)

        if revision:
            logger.info(f"[Revisions] Recorded r{revision.number} of {source_path} ({trigger.value})")
            # An export whose bytes another revision has already rendered (unchanged, or brought back by a restore)
            # shows that set at once.
            for export in revision.exports:
                self._reuse_render_set(revision, export.path)
        return revision

    def _snapshot_file(self, store: RevisionStore, path: str, kind: FileKind) -> RevisionFile | None:
        """Store one file's bytes, reading it only when its size or modification time changed since last time."""
        file = self.project.root / path
        try:
            before = file.stat()
        except FileNotFoundError:
            # Deleted between the scan that listed it and now; the next settled change records its absence.
            logger.debug(f"[Revisions] {path} is gone, leaving it out of the snapshot")
            return None

        cached = store.cached_hash(path, before.st_size, before.st_mtime_ns)
        if cached and self.objects.has(cached):
            return RevisionFile(path=path, kind=kind, sha256=cached, size=before.st_size)

        sha256, size = self.objects.store(file)
        after = file.stat()
        # Remember the hash only if the file did not change while it was read.
        if (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns):
            store.remember_hash(path, after.st_size, after.st_mtime_ns, sha256)
        return RevisionFile(path=path, kind=kind, sha256=sha256, size=size)

    def _write_revision(
        self,
        source: Source,
        target: Revision,
        current: Revision,
    ) -> tuple[list[str], list[str], list[str]]:
        """Make the source's files match `target`'s, leaving shared files alone. Returns written, deleted, skipped."""
        config = load_config(self.project.config_path)
        source_folder = PurePosixPath(source.path).parent

        def is_shared(file: RevisionFile) -> bool:
            if glob.globmatch(file.path, list(config.shared), flags=GLOB_FLAGS):
                return True
            return file.kind is FileKind.BUILDER and PurePosixPath(file.path).parent != source_folder

        written: list[str] = []
        deleted: list[str] = []
        skipped: list[str] = []
        current_manifest = current.manifest
        for file in target.files:
            if current_manifest.get(file.path) == file.sha256:
                continue
            if is_shared(file):
                skipped.append(file.path)
                continue
            self.objects.write_to(file.sha256, self.project.root / file.path)
            written.append(file.path)

        target_manifest = target.manifest
        for file in current.files:
            if file.path in target_manifest:
                continue
            if is_shared(file):
                skipped.append(file.path)
                continue
            (self.project.root / file.path).unlink(missing_ok=True)
            deleted.append(file.path)
        return written, deleted, skipped

    def _reuse_render_set(self, revision: Revision, export_path: str) -> bool:
        """Link in another revision's set for the same export bytes, if one exists. Returns whether it did."""
        name = render_names([file.path for file in revision.exports])[export_path]
        for holder, holder_set in self._sets_for_same_bytes(revision, export_path):
            if holder_set.state is RenderState.MISSING:
                continue
            holder_name = render_names([file.path for file in holder.exports])[export_path]
            link_render_set(self.renders_dir / str(holder.id), holder_name, self.renders_dir / str(revision.id), name)
            logger.debug(f"[Revisions] Reused revision {holder.id}'s renders of {export_path} for {revision.id}")
            return True
        return False

    def _sets_for_same_bytes(self, revision: Revision, export_path: str) -> list[tuple[Revision, StandardRenderSet]]:
        """Return the source's other revisions whose export at `export_path` has the same bytes, with their sets."""
        export = self._revision_export(revision, export_path)
        with RevisionStore(self.project.database_path) as store:
            holder_ids = store.revisions_holding(revision.source, export_path, export.sha256)
        holders = [self.get_revision(holder_id) for holder_id in holder_ids if holder_id != revision.id]
        return [(holder, self._render_set_of(holder, export_path)) for holder in holders]

    def _render_set_of(self, revision: Revision, export_path: str) -> StandardRenderSet:
        return next(render for render in self.standard_renders(revision) if render.export == export_path)

    @staticmethod
    def _revision_export(revision: Revision, export_path: str) -> RevisionFile:
        export = next((file for file in revision.exports if file.path == export_path), None)
        if export is None:
            raise KeyError(f"Revision {revision.id} has no export '{export_path}'")
        return export


def _source_files(source: Source) -> dict[str, FileKind]:
    """Every file the catalog lists for a source, by path, with what part of it each is."""
    kind_by_path = {source.path: FileKind.BLEND}
    if source.builder:
        kind_by_path[source.builder] = FileKind.BUILDER
    if source.readme:
        kind_by_path[source.readme] = FileKind.README
    for export in source.exports:
        kind_by_path[export] = FileKind.EXPORT
    for render in source.renders:
        kind_by_path[render] = FileKind.RENDER
    return kind_by_path
