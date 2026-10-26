"""Keeping the catalog live: rescan when the project's art changes and settles.

The watcher watches the configured roots, the folders references and output overrides draw from, and
`atelier.toml` itself. A batch of changes is held until it has been quiet for `settle_ms`, so a Blender save (a
temporary file and a rename) or a builder writing dozens of exports becomes one rescan, not hundreds.

Editing `atelier.toml` can move the roots, and saving it by rename ends the watch on the old file, so a change to it,
or to the set of paths to watch, restarts the watch. Every (re)start rescans first, so nothing that changed while
no watch was running is missed.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field
from watchfiles import Change, watch
from wcmatch import glob

from atelierd.catalog.discovery import GLOB_FLAGS, is_walkable_folder
from atelierd.catalog.models import CatalogChange, Source
from atelierd.catalog.service import Catalog
from atelierd.config import ConfigError, load_config
from atelierd.constants import SOURCE_SUFFIX, WATCH_MAX_BATCH_MS, WATCH_SETTLE_MS

logger = logging.getLogger(__name__)


class CatalogEvent(BaseModel):
    """One settled batch of changes, after the rescan it caused."""

    model_config = ConfigDict(frozen=True)

    change: CatalogChange
    touched: tuple[str, ...] = Field(
        default=(),
        description="Present sources one of whose files (the .blend, builder, README, an export or a render) was "
        "written or deleted, whether or not the catalog's shape changed. Revisions are recorded from these.",
    )


class CatalogWatcher:
    """Watch one project and rescan its catalog whenever its files settle after a change."""

    def __init__(
        self,
        catalog: Catalog,
        on_event: Callable[[CatalogEvent], None],
        settle_ms: int = WATCH_SETTLE_MS,
        max_batch_ms: int = WATCH_MAX_BATCH_MS,
    ) -> None:
        self._catalog = catalog
        self._on_event = on_event
        self._settle_ms = settle_ms
        self._max_batch_ms = max_batch_ms
        # The sources as of the last rescan, to know which files a source had before a change removed them.
        self._known_sources: tuple[Source, ...] = ()

    def run(self, stop_event: threading.Event) -> None:
        """Watch until `stop_event` is set or the watch is interrupted (Ctrl+C). Blocks; run it on its own thread."""
        project = self._catalog.project
        while not stop_event.is_set():
            watch_paths = self._watch_paths()
            logger.info(f"[Watcher] Watching {project.name}: {', '.join(str(path) for path in watch_paths)}")
            # Catch up on anything that changed before this watch started, including while it restarted.
            self._rescan(changed_paths=set())

            is_restart_needed = False
            for changes in watch(
                *watch_paths,
                watch_filter=self._is_relevant,
                step=self._settle_ms,
                debounce=self._max_batch_ms,
                stop_event=stop_event,
                raise_interrupt=False,
            ):
                changed_paths = {Path(changed_path) for _, changed_path in changes}
                self._rescan(changed_paths)
                # An editor saves atelier.toml by renaming a new file over it, which ends the watch on the old one,
                # and an edit can move the roots; either way the watch starts again on the current paths.
                is_restart_needed = project.config_path in changed_paths or self._watch_paths() != watch_paths
                if is_restart_needed:
                    logger.info(f"[Watcher] Restarting the watch on {project.name}")
                    break

            # watchfiles ends the watch on its own only when stopped or interrupted: then the watcher is done too.
            if not is_restart_needed:
                logger.info(f"[Watcher] Stopped watching {project.name}")
                return

    # -----------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------

    def _rescan(self, changed_paths: set[Path]) -> None:
        """Rescan after one settled batch and report the result, when it changed anything."""
        project = self._catalog.project
        try:
            result = self._catalog.scan()
        except Exception as error:
            # A half-typed atelier.toml, a folder deleted mid-walk or a briefly locked state.db must not end the
            # watcher; the next settled batch rescans.
            logger.exception(f"[Watcher] Rescanning {project.name} failed, waiting for the next change: {error}")
            return

        relative_paths = {
            changed_path.relative_to(project.root).as_posix()
            for changed_path in changed_paths
            if changed_path.is_relative_to(project.root)
        }
        # A deleted export is gone from the rescanned source, so its files before the scan count too.
        previous_files = {source.path: _files_of(source) for source in self._known_sources}
        touched = tuple(
            source.path
            for source in result.sources
            if relative_paths & (_files_of(source) | previous_files.get(source.path, set()))
        )
        self._known_sources = result.sources

        event = CatalogEvent(change=result.change, touched=touched)
        if event.change.is_empty and not event.touched:
            logger.debug(f"[Watcher] {len(changed_paths)} changes left the catalog as it was")
            return
        self._on_event(event)

    def _watch_paths(self) -> list[Path]:
        """Return the existing folders and files whose changes can change the catalog."""
        project = self._catalog.project
        try:
            config = load_config(project.config_path)
        except ConfigError as error:
            logger.error(f"[Watcher] Watching only {project.config_path.name} until it is valid again: {error}")
            return [project.config_path]

        candidate_paths = {project.root / root for root in config.roots}
        for glob_pattern in config.references.globs:
            candidate_paths.add(project.root / _static_prefix(glob_pattern))
        for override in config.sources.values():
            for glob_pattern in override.outputs or ():
                candidate_paths.add(project.root / _static_prefix(glob_pattern))

        # Watching a parent and its child twice would deliver every event twice; keep the outermost folders only.
        existing_folders = sorted(path for path in candidate_paths if path.is_dir())
        outermost_folders: list[Path] = []
        for folder in existing_folders:
            if not any(folder.is_relative_to(kept_folder) for kept_folder in outermost_folders):
                outermost_folders.append(folder)

        # The config file is watched by itself: watching the project root would mean watching all of Content/.
        # A config created later is picked up by the next scan, and from then on watched.
        if project.config_path.is_file():
            outermost_folders.append(project.config_path)
        return outermost_folders

    def _is_relevant(self, change: Change, changed_path: str) -> bool:
        """Drop changes that can never affect the catalog: private folders, state, Blender's backups."""
        path = Path(changed_path)
        project = self._catalog.project
        if path == project.config_path:
            return True
        if not path.is_relative_to(project.root) or path.is_relative_to(project.state_dir):
            return False

        relative_path = path.relative_to(project.root)
        if not all(is_walkable_folder(folder_name) for folder_name in relative_path.parent.parts):
            return False
        # `.blend1`, `.blend2` and `.blend@` contain the suffix without ending in it.
        is_blend_backup = SOURCE_SUFFIX in relative_path.name and not relative_path.name.endswith(SOURCE_SUFFIX)
        return not is_blend_backup


def _files_of(source: Source) -> set[str]:
    """Every project-relative path a source is made of."""
    files = {source.path, *source.exports, *source.renders}
    if source.builder:
        files.add(source.builder)
    if source.readme:
        files.add(source.readme)
    return files


def _static_prefix(glob_pattern: str) -> str:
    """Return the leading folders of a glob that contain no pattern characters, such as `SourceArt/References`."""
    static_parts: list[str] = []
    for part in PurePosixPath(glob_pattern).parts:
        if glob.is_magic(part, flags=GLOB_FLAGS):
            break
        static_parts.append(part)
    return "/".join(static_parts)
