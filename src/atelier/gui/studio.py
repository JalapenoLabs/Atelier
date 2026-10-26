"""The window's view-model: the state QML binds to and the actions it calls, over a `StudioBackend`.

`Studio` holds only what is on screen (the open project, the selection, the filter) and asks the backend for
everything else, off the UI thread through a `TaskRunner`. QML never talks to the backend directly.

Loading an export follows `docs/viewer.md`: the FBX goes straight to Qt Quick 3D's `RuntimeLoader` for an instant
picture while Blender converts it in the background, and the converted `.glb` replaces it as soon as it is ready,
because assimp's reading of a Blender-made FBX gets its colours and texture paths wrong. The reviewer can keep the
direct load instead, to see what assimp makes of the file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ConfigDict
from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

from atelier.gui.backend import StudioBackend
from atelier.gui.gallery import group_renders_by_stem
from atelier.gui.list_model import RowListModel
from atelier.gui.tasks import TaskRunner
from atelier.gui.tree import SourceRow, build_source_rows
from atelierd.catalog import CameraBookmark, CatalogEvent, Reference, ScanResult, Source, SourceStatus
from atelierd.exports import ConvertedExport

logger = logging.getLogger(__name__)


class LoadPath:
    """How the stage's model was loaded, as the stats overlay names it."""

    DIRECT = "FBX (direct)"
    BLENDER = "glTF (Blender)"


class ConversionState:
    """Where the background Blender conversion of the selected export stands."""

    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# ========================================================================================
# Rows of the lists QML shows
# ========================================================================================


class ExportRow(BaseModel):
    """One export in the export picker."""

    model_config = ConfigDict(frozen=True)

    path: str
    name: str


class RenderRow(BaseModel):
    """One render in the gallery; consecutive rows of one stem form a group."""

    model_config = ConfigDict(frozen=True)

    stem: str
    path: str
    name: str
    url: str


class ReferenceRow(BaseModel):
    """One image in the references rail."""

    model_config = ConfigDict(frozen=True)

    path: str
    name: str
    tag: str
    url: str


class BookmarkRow(BaseModel):
    """One camera bookmark, in Unreal's frame (Z up, cm)."""

    model_config = ConfigDict(frozen=True)

    name: str
    target_x: float
    target_y: float
    target_z: float
    yaw: float
    pitch: float
    distance: float


class Studio(QObject):
    """State and actions of the review window."""

    projectsChanged = Signal()
    projectChanged = Signal()
    selectionChanged = Signal()
    filterChanged = Signal()
    exportChanged = Signal()
    modelChanged = Signal()
    statsChanged = Signal()
    stageChanged = Signal()
    readmeChanged = Signal()
    messageChanged = Signal()
    busyChanged = Signal()
    stageReadyChanged = Signal()

    # Emitted by the watcher's thread; connected queued, so the handler runs on the UI thread.
    _catalogEventArrived = Signal(object)

    def __init__(self, backend: StudioBackend, tasks: TaskRunner) -> None:
        super().__init__()
        self._backend = backend
        self._tasks = tasks

        self._projects: list[str] = []
        self._project_name = ""
        self._sources: list[Source] = []
        self._collapsed: set[str] = set()
        self._filter_text = ""
        self._status_filter = ""
        self._selected_source = ""
        self._selected_export = ""
        self._readme_text = ""
        self._stage_image = ""
        self._message = ""
        self._busy_count = 0

        # The model on the stage and how it got there.
        self._model_url = ""
        self._model_revision = 0
        self._load_path = ""
        self._direct_failed = False
        self._is_direct_preferred = False
        self._is_model_reported = False
        self._conversion_state = ConversionState.IDLE
        self._converted: ConvertedExport | None = None
        # Bumped whenever the export on the stage changes, so a late conversion of an older one is ignored.
        self._load_generation = 0

        self._source_rows = RowListModel(SourceRow)
        self._export_rows = RowListModel(ExportRow)
        self._render_rows = RowListModel(RenderRow)
        self._reference_rows = RowListModel(ReferenceRow)
        self._bookmark_rows = RowListModel(BookmarkRow)

        self._catalogEventArrived.connect(self._on_catalog_event)

    # ####################### #
    #       PROPERTIES        #
    # ####################### #

    def _get_projects(self) -> list[str]:
        return self._projects

    def _get_project_name(self) -> str:
        return self._project_name

    def _get_source_rows(self) -> QObject:
        return self._source_rows

    def _get_export_rows(self) -> QObject:
        return self._export_rows

    def _get_render_rows(self) -> QObject:
        return self._render_rows

    def _get_reference_rows(self) -> QObject:
        return self._reference_rows

    def _get_bookmark_rows(self) -> QObject:
        return self._bookmark_rows

    def _get_status_values(self) -> list[str]:
        return [status.value for status in SourceStatus]

    def _get_filter_text(self) -> str:
        return self._filter_text

    def _set_filter_text(self, text: str) -> None:
        if text == self._filter_text:
            return
        self._filter_text = text
        self.filterChanged.emit()
        self._rebuild_tree()

    def _get_status_filter(self) -> str:
        return self._status_filter

    def _set_status_filter(self, status: str) -> None:
        if status == self._status_filter:
            return
        self._status_filter = status
        self.filterChanged.emit()
        self._rebuild_tree()

    def _get_selected_source(self) -> str:
        return self._selected_source

    def _get_selected_name(self) -> str:
        return PurePosixPath(self._selected_source).stem if self._selected_source else ""

    def _get_selected_status(self) -> str:
        source = self._find_source(self._selected_source)
        return source.status.value if source else ""

    def _get_selected_export(self) -> str:
        return self._selected_export

    def _get_readme_text(self) -> str:
        return self._readme_text

    def _get_model_url(self) -> str:
        return self._model_url

    def _get_model_revision(self) -> int:
        return self._model_revision

    def _get_load_path(self) -> str:
        return self._load_path

    def _get_conversion_state(self) -> str:
        return self._conversion_state

    def _get_stats(self) -> dict[str, Any]:
        if not self._converted:
            return {}
        stats = self._converted.stats
        size_x, size_y, size_z = stats.size_cm
        return {
            "triangles": stats.triangles,
            "materials": len(stats.materials),
            "materialNames": list(stats.materials),
            "collisionHulls": len(stats.collision_hulls),
            "isSkinned": stats.is_skinned,
            "sizeX": size_x,
            "sizeY": size_y,
            "sizeZ": size_z,
            "boundsMin": list(stats.bounds_min_cm),
            "boundsMax": list(stats.bounds_max_cm),
        }

    def _get_stage_image(self) -> str:
        return self._stage_image

    def _get_message(self) -> str:
        return self._message

    def _get_is_busy(self) -> bool:
        return self._busy_count > 0

    def _get_is_stage_ready(self) -> bool:
        """Whether the stage shows what it will settle on: a reported model with its stats, or an image."""
        if self._stage_image:
            return True
        if not self._selected_export:
            return bool(self._selected_source)
        is_conversion_settled = self._conversion_state in (ConversionState.DONE, ConversionState.FAILED)
        return self._is_model_reported and is_conversion_settled

    projects = Property(list, _get_projects, notify=projectsChanged)
    projectName = Property(str, _get_project_name, notify=projectChanged)
    sourceRows = Property(QObject, _get_source_rows, constant=True)
    exportRows = Property(QObject, _get_export_rows, constant=True)
    renderRows = Property(QObject, _get_render_rows, constant=True)
    referenceRows = Property(QObject, _get_reference_rows, constant=True)
    bookmarkRows = Property(QObject, _get_bookmark_rows, constant=True)
    statusValues = Property(list, _get_status_values, constant=True)
    filterText = Property(str, _get_filter_text, _set_filter_text, notify=filterChanged)
    statusFilter = Property(str, _get_status_filter, _set_status_filter, notify=filterChanged)
    selectedSource = Property(str, _get_selected_source, notify=selectionChanged)
    selectedName = Property(str, _get_selected_name, notify=selectionChanged)
    selectedStatus = Property(str, _get_selected_status, notify=selectionChanged)
    selectedExport = Property(str, _get_selected_export, notify=exportChanged)
    readmeText = Property(str, _get_readme_text, notify=readmeChanged)
    modelUrl = Property(str, _get_model_url, notify=modelChanged)
    modelRevision = Property(int, _get_model_revision, notify=modelChanged)
    loadPath = Property(str, _get_load_path, notify=modelChanged)
    conversionState = Property(str, _get_conversion_state, notify=statsChanged)
    stats = Property(dict, _get_stats, notify=statsChanged)
    stageImage = Property(str, _get_stage_image, notify=stageChanged)
    message = Property(str, _get_message, notify=messageChanged)
    isBusy = Property(bool, _get_is_busy, notify=busyChanged)
    isStageReady = Property(bool, _get_is_stage_ready, notify=stageReadyChanged)

    @property
    def export_paths(self) -> list[str]:
        """The selected source's exports, as the export picker lists them."""
        return [row.path for row in self._export_rows.rows if isinstance(row, ExportRow)]

    @property
    def render_paths(self) -> list[str]:
        """The selected source's renders, in gallery order."""
        return [row.path for row in self._render_rows.rows if isinstance(row, RenderRow)]

    # ####################### #
    #        PROJECTS         #
    # ####################### #

    @Slot()
    def refreshProjects(self) -> None:
        """Reload the machine's list of known projects."""
        self._run_catalog(self._backend.list_projects, self._on_projects)

    @Slot(str)
    def openProject(self, reference: str) -> None:
        """Open a project by name or path, replacing the open one."""
        if not reference:
            logger.debug("[Studio] openProject called without a project")
            return
        self._show_message(f"Opening {reference}...")
        self._run_catalog(
            lambda: self._backend.open_project(reference, on_event=self._catalogEventArrived.emit),
            self._on_project_opened,
        )

    def close(self) -> None:
        """Stop background work; called once as the window closes."""
        self._backend.close_project()
        self._tasks.shutdown()

    def _on_projects(self, projects: list[str]) -> None:
        self._projects = projects
        self.projectsChanged.emit()

    def _on_project_opened(self, result: ScanResult) -> None:
        self._project_name = self._backend.project_name
        self._collapsed.clear()
        self._sources = list(result.sources)
        self._clear_selection()
        self._rebuild_tree()
        self._set_references(list(result.references))
        self.projectChanged.emit()
        self._show_message(f"{self._project_name}: {len(result.sources)} sources")
        # Opening by path registers a project the picker may not list yet.
        self.refreshProjects()

    @Slot(object)
    def _on_catalog_event(self, event: CatalogEvent) -> None:
        """Follow a settled change on disk: refresh the lists, and reload the selection if its files changed."""
        is_selection_touched = self._selected_source in (*event.touched, *event.change.changed)
        self._run_catalog(self._backend.list_sources, lambda sources: self._on_sources(sources, is_selection_touched))
        if event.change.references_changed:
            self._run_catalog(self._backend.list_references, self._set_references)

    def _on_sources(self, sources: list[Source], is_selection_touched: bool = False) -> None:
        self._sources = sources
        self._rebuild_tree()
        if self._selected_source and not self._find_source(self._selected_source):
            self._show_message(f"{self._selected_source} is gone")
            self._clear_selection()
            return
        if is_selection_touched:
            self._show_selection(keep_export=True)
        self.selectionChanged.emit()

    # ####################### #
    #     TREE AND STATUS     #
    # ####################### #

    @Slot(str)
    def toggleFolder(self, folder_path: str) -> None:
        """Open or close a folder in the tree."""
        if folder_path in self._collapsed:
            self._collapsed.remove(folder_path)
        else:
            self._collapsed.add(folder_path)
        self._rebuild_tree()

    @Slot(str)
    def selectSource(self, source_path: str) -> None:
        """Show a source: its exports on the stage, its renders, README and bookmarks beside it."""
        if not self._find_source(source_path):
            logger.warning(f"[Studio] selectSource: no source '{source_path}' in {self._project_name}")
            return
        if source_path == self._selected_source:
            return
        self._selected_source = source_path
        self._show_selection(keep_export=False)
        self.selectionChanged.emit()

    @Slot(str)
    def setSourceStatus(self, status: str) -> None:
        """Set the selected source's review status."""
        source_path = self._selected_source
        if not source_path:
            logger.debug("[Studio] setSourceStatus called with nothing selected")
            return

        def set_status() -> list[Source]:
            self._backend.set_status(source_path, SourceStatus(status))
            return self._backend.list_sources()

        self._run_catalog(set_status, self._on_sources)

    def _rebuild_tree(self) -> None:
        status = SourceStatus(self._status_filter) if self._status_filter else None
        rows = build_source_rows(self._sources, self._filter_text, status, frozenset(self._collapsed))
        self._source_rows.set_rows(rows)

    def _clear_selection(self) -> None:
        self._selected_source = ""
        self._export_rows.set_rows([])
        self._render_rows.set_rows([])
        self._bookmark_rows.set_rows([])
        self._readme_text = ""
        self.readmeChanged.emit()
        self._stage_image = ""
        self.stageChanged.emit()
        self._set_export("")
        self.selectionChanged.emit()

    def _show_selection(self, keep_export: bool) -> None:
        """Fill the stage and the context panes from the selected source."""
        source = self._find_source(self._selected_source)
        if not source:
            logger.warning(f"[Studio] The selected source '{self._selected_source}' is not in the catalog")
            return

        self._export_rows.set_rows([ExportRow(path=path, name=PurePosixPath(path).stem) for path in source.exports])
        self._render_rows.set_rows(self._render_rows_for(source))
        self._readme_text = self._read_readme(source)
        self.readmeChanged.emit()
        self._refresh_bookmarks()

        export = self._selected_export if keep_export and self._selected_export in source.exports else ""
        export = export or (source.exports[0] if source.exports else "")
        self._stage_image = ""
        if not export and source.renders:
            self._stage_image = self._file_url(source.renders[0])
        self.stageChanged.emit()
        self._set_export(export, force_reload=keep_export)

    def _render_rows_for(self, source: Source) -> list[RenderRow]:
        rows: list[RenderRow] = []
        for group in group_renders_by_stem(source.renders):
            for path in group.renders:
                rows.append(
                    RenderRow(stem=group.stem, path=path, name=PurePosixPath(path).name, url=self._file_url(path))
                )
        return rows

    def _read_readme(self, source: Source) -> str:
        if not source.readme:
            return ""
        readme_file = self._backend.resolve(source.readme)
        try:
            return readme_file.read_text(encoding="utf-8")
        except OSError as error:
            logger.warning(f"[Studio] Could not read {readme_file}: {error}")
            return ""

    # ####################### #
    #     STAGE AND MODEL     #
    # ####################### #

    @Slot(str)
    def selectExport(self, export_path: str) -> None:
        """Put one of the selected source's exports on the stage."""
        if export_path == self._selected_export and not self._stage_image:
            return
        self._stage_image = ""
        self.stageChanged.emit()
        self._set_export(export_path)

    @Slot(str)
    def showRender(self, render_path: str) -> None:
        """Show a render full size on the stage."""
        self._stage_image = self._file_url(render_path)
        self.stageChanged.emit()
        self.stageReadyChanged.emit()

    @Slot()
    def showModel(self) -> None:
        """Go back from a render to the 3D viewport."""
        self._stage_image = ""
        self.stageChanged.emit()
        self.stageReadyChanged.emit()

    @Slot(bool, str)
    def reportModelLoad(self, is_loaded: bool, error_text: str) -> None:
        """Called by the viewport once `RuntimeLoader` has loaded, or failed to load, the model URL."""
        self._is_model_reported = True
        if not is_loaded and self._load_path == LoadPath.DIRECT:
            logger.warning(f"[Studio] Direct load of {self._selected_export} failed, using the .glb: {error_text}")
            self._direct_failed = True
            self._switch_to_converted()
        elif not is_loaded:
            self._show_message(f"Could not load {self._selected_export}: {error_text}")
        self.stageReadyChanged.emit()

    @Slot(bool)
    def setDirectPreferred(self, is_preferred: bool) -> None:
        """Keep showing the FBX as Qt loads it directly, instead of switching to Blender's conversion."""
        if is_preferred == self._is_direct_preferred:
            return
        self._is_direct_preferred = is_preferred
        if not is_preferred:
            self._switch_to_converted()
        elif self._selected_export:
            self._set_export(self._selected_export, force_reload=True)

    def _set_export(self, export_path: str, force_reload: bool = False) -> None:
        """Load an export onto the stage directly, and start the conversion that brings its stats and its `.glb`."""
        if export_path == self._selected_export and not force_reload:
            return
        self._selected_export = export_path
        self._load_generation += 1
        self._direct_failed = False
        self._is_model_reported = False
        self._converted = None
        self.exportChanged.emit()

        if not export_path:
            self._conversion_state = ConversionState.IDLE
            self._set_model("", "")
            self.statsChanged.emit()
            self.stageReadyChanged.emit()
            return

        self._conversion_state = ConversionState.RUNNING
        self.statsChanged.emit()
        self._set_model(self._file_url(export_path), LoadPath.DIRECT)

        generation = self._load_generation
        self._tasks.run_conversion(
            lambda: self._backend.convert_export(export_path),
            lambda converted: self._on_converted(generation, converted),
            lambda error: self._on_conversion_failed(generation, error),
        )
        self.stageReadyChanged.emit()

    def _on_converted(self, generation: int, converted: ConvertedExport) -> None:
        if generation != self._load_generation:
            logger.debug(f"[Studio] Dropping a stale conversion of {converted.export}")
            return
        self._converted = converted
        self._conversion_state = ConversionState.DONE
        if self._direct_failed or not self._is_direct_preferred:
            self._switch_to_converted()
        self.statsChanged.emit()
        self.stageReadyChanged.emit()

    def _on_conversion_failed(self, generation: int, error: Exception) -> None:
        if generation != self._load_generation:
            return
        logger.error(f"[Studio] Conversion failed: {error}")
        self._conversion_state = ConversionState.FAILED
        self.statsChanged.emit()
        self._show_message(f"Blender could not convert {PurePosixPath(self._selected_export).name}")
        self.stageReadyChanged.emit()

    def _switch_to_converted(self) -> None:
        """Load the converted `.glb` once it exists; until then the conversion's arrival does it."""
        if not self._converted:
            logger.debug("[Studio] Waiting for the conversion before switching to the .glb")
            return
        self._is_model_reported = False
        self._set_model(QUrl.fromLocalFile(str(self._converted.glb_path)).toString(), LoadPath.BLENDER)

    def _set_model(self, url: str, load_path: str) -> None:
        self._model_url = url
        self._load_path = load_path
        # A new revision makes the viewport reload even when the URL is unchanged (the export was rewritten).
        self._model_revision += 1
        self.modelChanged.emit()

    # ####################### #
    #   BOOKMARKS, REFERENCES #
    # ####################### #

    @Slot(str, float, float, float, float, float, float)
    def saveBookmark(
        self,
        name: str,
        target_x: float,
        target_y: float,
        target_z: float,
        yaw: float,
        pitch: float,
        distance: float,
    ) -> None:
        """Save the viewport's camera on the selected source under `name`."""
        source_path = self._selected_source
        bookmark_name = name.strip()
        if not (source_path and bookmark_name):
            logger.debug("[Studio] saveBookmark needs a selected source and a name")
            return
        bookmark = CameraBookmark(
            name=bookmark_name,
            target_cm=(target_x, target_y, target_z),
            yaw_degrees=yaw,
            pitch_degrees=pitch,
            distance_cm=distance,
        )
        self._run_catalog(
            lambda: self._backend.save_bookmark(source_path, bookmark), lambda _: self._refresh_bookmarks()
        )

    @Slot(str)
    def deleteBookmark(self, name: str) -> None:
        """Delete one of the selected source's bookmarks."""
        source_path = self._selected_source
        self._run_catalog(lambda: self._backend.delete_bookmark(source_path, name), lambda _: self._refresh_bookmarks())

    @Slot(list)
    def addReferences(self, urls: list[Any]) -> None:
        """Copy dropped image files into the project's reference drop folder."""
        # QML's `drop.urls` arrive as QUrl objects; `str()` of one is its repr, not the URL, so take them as they are.
        qt_urls = [url if isinstance(url, QUrl) else QUrl(str(url)) for url in urls]
        local_paths = [Path(url.toLocalFile()) for url in qt_urls if url.isLocalFile()]
        if not local_paths:
            self._show_message("Only local image files can be added as references")
            return

        def add_all() -> list[Reference]:
            for image_path in local_paths:
                self._backend.add_reference(image_path)
            return list(self._backend.rescan().references)

        self._run_catalog(add_all, self._on_references_added)

    def _on_references_added(self, references: list[Reference]) -> None:
        self._set_references(references)
        self._show_message("Reference added")

    def _refresh_bookmarks(self) -> None:
        source_path = self._selected_source
        if not source_path:
            self._bookmark_rows.set_rows([])
            return
        self._run_catalog(lambda: self._backend.list_bookmarks(source_path), self._set_bookmarks)

    def _set_bookmarks(self, bookmarks: list[CameraBookmark]) -> None:
        rows = []
        for bookmark in bookmarks:
            target_x, target_y, target_z = bookmark.target_cm
            rows.append(
                BookmarkRow(
                    name=bookmark.name,
                    target_x=target_x,
                    target_y=target_y,
                    target_z=target_z,
                    yaw=bookmark.yaw_degrees,
                    pitch=bookmark.pitch_degrees,
                    distance=bookmark.distance_cm,
                )
            )
        self._bookmark_rows.set_rows(rows)

    def _set_references(self, references: list[Reference]) -> None:
        rows = [
            ReferenceRow(
                path=reference.path,
                name=PurePosixPath(reference.path).name,
                tag=reference.tag,
                url=self._file_url(reference.path),
            )
            for reference in references
        ]
        self._reference_rows.set_rows(rows)

    # -----------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------

    def _find_source(self, source_path: str) -> Source | None:
        return next((source for source in self._sources if source.path == source_path), None)

    def _file_url(self, project_path: str) -> str:
        return QUrl.fromLocalFile(str(self._backend.resolve(project_path))).toString()

    def _show_message(self, text: str) -> None:
        self._message = text
        self.messageChanged.emit()

    def _run_catalog(self, work: Callable[[], Any], on_done: Callable[[Any], None]) -> None:
        """Run backend work off the UI thread, showing the window as busy until it is done."""
        self._busy_count += 1
        self.busyChanged.emit()

        def finish(result: Any) -> None:
            self._busy_count -= 1
            self.busyChanged.emit()
            on_done(result)

        def fail(error: Exception) -> None:
            self._busy_count -= 1
            self.busyChanged.emit()
            logger.error(f"[Studio] {error}")
            self._show_message(str(error))

        self._tasks.run_catalog(work, finish, fail)
