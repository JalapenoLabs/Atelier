"""The catalog use cases every front door calls: scan a project, list its sources, set a status.

The CLI today, and the GUI's RPC and the manager's MCP later, are thin adapters over this class, so they can never
disagree about what a source is. Each call opens its own `CatalogStore`, which keeps the class safe to share across
threads (SQLite connections are not).
"""

from __future__ import annotations

import logging
from pathlib import Path

from atelierd.catalog.discovery import discover_sources
from atelierd.catalog.models import CameraBookmark, Reference, ScanResult, Source, SourceStatus
from atelierd.catalog.references import add_reference, collect_references
from atelierd.catalog.store import CatalogStore
from atelierd.config import load_config
from atelierd.project import Project, ensure_state_ignored

logger = logging.getLogger(__name__)


class Catalog:
    """One project's live catalog."""

    def __init__(self, project: Project) -> None:
        self.project = project

    def scan(self) -> ScanResult:
        """Discover the project's sources and references and store them, keeping every source's status."""
        # Read afresh on every scan, so an edit to atelier.toml applies on the next one.
        config = load_config(self.project.config_path)
        discovered, unmatched_overrides = discover_sources(self.project.root, config)
        references = collect_references(self.project.root, config)

        # Ignore the state folder before creating it, so version control never sees it appear.
        ensure_state_ignored(self.project)
        with CatalogStore(self.project.database_path) as store:
            change = store.apply_scan(discovered, references)
            sources = store.list_sources()

        logger.info(f"[Catalog] Scanned {self.project.name}: {len(sources)} sources, {len(references)} references")
        return ScanResult(
            sources=tuple(sources),
            references=tuple(references),
            change=change,
            unmatched_overrides=tuple(unmatched_overrides),
        )

    def list_sources(self) -> list[Source]:
        """Return the stored catalog, as of the last scan."""
        with CatalogStore(self.project.database_path) as store:
            return store.list_sources()

    def list_references(self) -> list[Reference]:
        """Return the stored reference images, as of the last scan."""
        with CatalogStore(self.project.database_path) as store:
            return store.list_references()

    def set_status(self, source_path: str, status: SourceStatus) -> None:
        """Set a source's review status."""
        with CatalogStore(self.project.database_path) as store:
            store.set_status(source_path, status)
        logger.info(f"[Catalog] {self.project.name}: {source_path} is now {status.value}")

    def list_bookmarks(self, source_path: str) -> list[CameraBookmark]:
        """Return a source's camera bookmarks, sorted by name."""
        with CatalogStore(self.project.database_path) as store:
            return store.list_bookmarks(source_path)

    def save_bookmark(self, source_path: str, bookmark: CameraBookmark) -> None:
        """Save a camera bookmark on a source, replacing any of the same name."""
        with CatalogStore(self.project.database_path) as store:
            store.save_bookmark(source_path, bookmark)
        logger.info(f"[Catalog] {self.project.name}: saved bookmark '{bookmark.name}' on {source_path}")

    def delete_bookmark(self, source_path: str, name: str) -> None:
        """Delete a source's camera bookmark."""
        with CatalogStore(self.project.database_path) as store:
            store.delete_bookmark(source_path, name)
        logger.info(f"[Catalog] {self.project.name}: deleted bookmark '{name}' on {source_path}")

    def add_reference(self, image: Path) -> str:
        """Copy an image into the project's reference drop folder and return its project-relative path.

        The new reference is listed from the next scan, which the watcher runs once the copy settles.
        """
        config = load_config(self.project.config_path)
        return add_reference(self.project.root, config, image)
