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

logger = logging.getLogger(__name__)


class StudioBackend(Protocol):
    """What the window needs from the core, for one open project at a time."""

    def list_projects(self) -> list[str]:
        """Return the names of the projects this machine knows, sorted."""
        ...

    def open_project(self, reference: str, on_event: Callable[[CatalogEvent], None]) -> ScanResult:
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
        self._watch_stop: threading.Event | None = None
        self._watch_thread: threading.Thread | None = None

    def list_projects(self) -> list[str]:
        """Return the names of the projects this machine knows, sorted."""
        return sorted(load_registry(self._registry_path).projects)

    def open_project(self, reference: str, on_event: Callable[[CatalogEvent], None]) -> ScanResult:
        """Open a project by name or path, scan it, and report each later change to `on_event` (on any thread)."""
        self.close_project()

        project_root = resolve_project_root(reference, self._registry_path)
        state_dir = self._state_root / project_root.name if self._state_root else None
        project = open_project(project_root, state_dir=state_dir)
        register_project(project.root, self._registry_path)

        catalog = Catalog(project)
        result = catalog.scan()
        self._catalog = catalog

        stop_event = threading.Event()
        watcher = CatalogWatcher(catalog, on_event=on_event)
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

    def _open_catalog(self) -> Catalog:
        if self._catalog is None:
            raise RuntimeError("No project is open")
        return self._catalog
