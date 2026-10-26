"""The GUI's one door into the core: everything the window reads or changes goes through a `StudioBackend`.

Today `InProcessBackend` calls the core directly, in this process. The daemon's RPC will replace it with a client
that satisfies the same protocol, so the swap touches this module and nothing that draws. That is why the protocol
speaks only in core models and project-relative paths, and why nothing above it imports `atelierd`'s services.

Every method may block (scans, SQLite, Blender), so the window calls them off the UI thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from atelierd.catalog import (
    CameraBookmark,
    Catalog,
    CatalogEvent,
    CatalogWatcher,
    Reference,
    ScanResult,
    Source,
    SourceStatus,
)
from atelierd.exports import ConvertedExport, convert_export
from atelierd.project import open_project
from atelierd.registry import load_registry, register_project, resolve_project_root
from atelierd.revisions import RestoreResult, RevisionEvent, Revisions, RevisionTracker, SourceHistory

logger = logging.getLogger(__name__)

# Everything the core reports about the open project as it happens: settled file changes, new revisions, renders.
type StudioEvent = CatalogEvent | RevisionEvent


class StudioBackend(Protocol):
    """What the window needs from the core, for one open project at a time."""

    def list_projects(self) -> list[str]:
        """Return the names of the projects this machine knows, sorted."""
        ...

    def open_project(self, reference: str, on_event: Callable[[StudioEvent], None]) -> ScanResult:
        """Open a project by name or path, scan it, and report each later change to `on_event` (on any thread)."""
        ...

    def close_project(self) -> None:
        """Stop following the open project, if any."""
        ...

    @property
    def project_name(self) -> str:
        """The open project's name, or an empty string."""
        ...

    def resolve(self, project_path: str) -> Path:
        """Return the absolute path of a project-relative path in the open project."""
        ...

    def rescan(self) -> ScanResult:
        """Scan the open project again now, rather than when the watcher next notices a change."""
        ...

    def list_sources(self) -> list[Source]:
        """Return the open project's sources."""
        ...

    def list_references(self) -> list[Reference]:
        """Return the open project's reference images."""
        ...

    def set_status(self, source_path: str, status: SourceStatus) -> None:
        """Set a source's review status."""
        ...

    def list_bookmarks(self, source_path: str) -> list[CameraBookmark]:
        """Return a source's camera bookmarks."""
        ...

    def save_bookmark(self, source_path: str, bookmark: CameraBookmark) -> None:
        """Save a camera bookmark on a source."""
        ...

    def delete_bookmark(self, source_path: str, name: str) -> None:
        """Delete a source's camera bookmark."""
        ...

    def add_reference(self, image: Path) -> str:
        """Copy an image into the reference drop folder and return its project-relative path."""
        ...

    def convert_export(self, export_path: str) -> ConvertedExport:
        """Return an export converted to `.glb` with its stats, converting it on a cache miss."""
        ...

    def get_history(self, source_path: str) -> SourceHistory:
        """Return a source's revisions, newest first, with their standard renders and its restore mark."""
        ...

    def request_standard_renders(self, revision_id: int, export_path: str) -> None:
        """Render one export of a revision in the background, unless its renders exist or are on their way."""
        ...

    def convert_revision_export(self, revision_id: int, export_path: str) -> ConvertedExport:
        """Return one of a revision's exports, as snapshotted, converted to `.glb` with its stats."""
        ...

    def object_path(self, sha256: str) -> Path:
        """Return where a snapshotted file's bytes are on disk."""
        ...

    def restore_revision(self, source_path: str, revision_id: int) -> RestoreResult:
        """Write a whole earlier revision of a source back into the project, snapshotting the current state first."""
        ...


class InProcessBackend:
    """A `StudioBackend` that runs the core in this process and watches the open project on a thread."""

    def __init__(self, registry_path: Path, state_root: Path | None = None) -> None:
        """Create the backend.

        Args:
            registry_path: the machine's project registry.
            state_root: when set, each project's state lives in `<state_root>/<project name>/` instead of its
                `.atelier/` folder, which leaves the projects themselves untouched.
        """
        self._registry_path = registry_path
        self._state_root = state_root
        self._catalog: Catalog | None = None
        self._tracker: RevisionTracker | None = None
        self._watch_stop: threading.Event | None = None
        self._watch_thread: threading.Thread | None = None

    def list_projects(self) -> list[str]:
        """Return the names of the projects this machine knows, sorted."""
        return sorted(load_registry(self._registry_path).projects)

    def open_project(self, reference: str, on_event: Callable[[StudioEvent], None]) -> ScanResult:
        """Open a project by name or path, scan it, and report each later change to `on_event` (on any thread).

        Revisions start catching up in the background: every source gets a baseline the first time, and one that
        changed while the project was closed gets a revision for it.
        """
        self.close_project()

        project_root = resolve_project_root(reference, self._registry_path)
        state_dir = self._state_root / project_root.name if self._state_root else None
        project = open_project(project_root, state_dir=state_dir)
        register_project(project.root, self._registry_path)

        catalog = Catalog(project)
        result = catalog.scan()
        self._catalog = catalog
        tracker = RevisionTracker(Revisions(project), on_event=on_event)
        tracker.start()
        self._tracker = tracker

        def on_catalog_event(event: CatalogEvent) -> None:
            on_event(event)
            tracker.handle_catalog_event(event)

        stop_event = threading.Event()
        watcher = CatalogWatcher(catalog, on_event=on_catalog_event)
        self._watch_stop = stop_event
        self._watch_thread = threading.Thread(
            target=watcher.run,
            args=(stop_event,),
            name=f"watch-{project.name}",
            daemon=True,
        )
        self._watch_thread.start()
        return result

    def close_project(self) -> None:
        """Stop following the open project, if any."""
        if self._watch_stop:
            self._watch_stop.set()
        if self._watch_thread:
            self._watch_thread.join(timeout=5)
            if self._watch_thread.is_alive():
                logger.warning(f"[Backend] Watcher {self._watch_thread.name} did not stop within 5 s")
        if self._tracker:
            self._tracker.stop()
        self._tracker = None
        self._catalog = None
        self._watch_stop = None
        self._watch_thread = None

    @property
    def project_name(self) -> str:
        """The open project's name, or an empty string."""
        return self._catalog.project.name if self._catalog else ""

    def resolve(self, project_path: str) -> Path:
        """Return the absolute path of a project-relative path in the open project."""
        return self._open_catalog().project.root / project_path

    def rescan(self) -> ScanResult:
        """Scan the open project again now, rather than when the watcher next notices a change."""
        return self._open_catalog().scan()

    def list_sources(self) -> list[Source]:
        """Return the open project's sources."""
        return self._open_catalog().list_sources()

    def list_references(self) -> list[Reference]:
        """Return the open project's reference images."""
        return self._open_catalog().list_references()

    def set_status(self, source_path: str, status: SourceStatus) -> None:
        """Set a source's review status."""
        self._open_catalog().set_status(source_path, status)

    def list_bookmarks(self, source_path: str) -> list[CameraBookmark]:
        """Return a source's camera bookmarks."""
        return self._open_catalog().list_bookmarks(source_path)

    def save_bookmark(self, source_path: str, bookmark: CameraBookmark) -> None:
        """Save a camera bookmark on a source."""
        self._open_catalog().save_bookmark(source_path, bookmark)

    def delete_bookmark(self, source_path: str, name: str) -> None:
        """Delete a source's camera bookmark."""
        self._open_catalog().delete_bookmark(source_path, name)

    def add_reference(self, image: Path) -> str:
        """Copy an image into the reference drop folder and return its project-relative path."""
        return self._open_catalog().add_reference(image)

    def convert_export(self, export_path: str) -> ConvertedExport:
        """Return an export converted to `.glb` with its stats, converting it on a cache miss."""
        return convert_export(self._open_catalog().project, export_path)

    def get_history(self, source_path: str) -> SourceHistory:
        """Return a source's revisions, newest first, with their standard renders and its restore mark."""
        return self._open_tracker().history(source_path)

    def request_standard_renders(self, revision_id: int, export_path: str) -> None:
        """Render one export of a revision in the background, unless its renders exist or are on their way."""
        self._open_tracker().request_standard_renders(revision_id, export_path)

    def convert_revision_export(self, revision_id: int, export_path: str) -> ConvertedExport:
        """Return one of a revision's exports, as snapshotted, converted to `.glb` with its stats."""
        return self._open_tracker().revisions.convert_revision_export(revision_id, export_path)

    def object_path(self, sha256: str) -> Path:
        """Return where a snapshotted file's bytes are on disk."""
        return self._open_tracker().revisions.object_path(sha256)

    def restore_revision(self, source_path: str, revision_id: int) -> RestoreResult:
        """Write a whole earlier revision of a source back into the project, snapshotting the current state first.

        No worker runs in this process, so every source is idle as far as the restore's check is concerned.
        """
        return self._open_tracker().restore(source_path, revision_id)

    def _open_tracker(self) -> RevisionTracker:
        if self._tracker is None:
            raise RuntimeError("No project is open")
        return self._tracker

    def _open_catalog(self) -> Catalog:
        if self._catalog is None:
            raise RuntimeError("No project is open")
        return self._catalog
