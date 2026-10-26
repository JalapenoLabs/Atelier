"""The window's view-model, driven through a fake backend: selection, the load path and background work."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl

from atelier.gui.studio import LoadPath, Studio
from atelier.gui.tasks import TaskRunner
from atelierd.catalog import (
    CameraBookmark,
    CatalogChange,
    CatalogEvent,
    Reference,
    ScanResult,
    Source,
    SourceStatus,
)
from atelierd.exports import ConvertedExport, ExportStats

WaitUntil = Callable[[Callable[[], bool]], None]

CARBINE = "SourceArt/Weapons/Carbine/Carbine.blend"
CARBINE_EXPORT = "SourceArt/Weapons/Carbine/Export/SK_Carbine.fbx"
STOCK_EXPORT = "SourceArt/Weapons/Carbine/Export/SM_Stock.fbx"
POSTER = "SourceArt/Posters/Poster.blend"


class FakeBackend:
    """A `StudioBackend` over fixed data. Conversions block until released, to control their timing."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.sources = [
            Source(
                path=CARBINE,
                exports=(CARBINE_EXPORT, STOCK_EXPORT),
                renders=(
                    "SourceArt/Weapons/Carbine/Review/Carbine_Side.png",
                    "SourceArt/Weapons/Carbine/Review/Carbine_Front.png",
                ),
                status=SourceStatus.IN_REVIEW,
            ),
            Source(path=POSTER, renders=("SourceArt/Posters/Poster_preview.png",)),
        ]
        self.references = [Reference(path="SourceArt/References/mood.png", tag="References")]
        self.bookmarks: dict[str, CameraBookmark] = {}
        self.added_images: list[Path] = []
        self.released_conversions: dict[str, threading.Event] = {}
        self.on_event: Callable[[CatalogEvent], None] | None = None

    @property
    def project_name(self) -> str:
        return "Fake"

    def list_projects(self) -> list[str]:
        return ["Fake"]

    def open_project(self, reference: str, on_event: Callable[[CatalogEvent], None]) -> ScanResult:
        self.on_event = on_event
        return ScanResult(sources=tuple(self.sources), references=tuple(self.references), change=CatalogChange())

    def close_project(self) -> None:
        self.on_event = None

    def resolve(self, project_path: str) -> Path:
        return self.root / project_path

    def rescan(self) -> ScanResult:
        return ScanResult(sources=tuple(self.sources), references=tuple(self.references), change=CatalogChange())

    def list_sources(self) -> list[Source]:
        return list(self.sources)

    def list_references(self) -> list[Reference]:
        return list(self.references)

    def set_status(self, source_path: str, status: SourceStatus) -> None:
        self.sources = [
            source.model_copy(update={"status": status}) if source.path == source_path else source
            for source in self.sources
        ]

    def list_bookmarks(self, source_path: str) -> list[CameraBookmark]:
        return sorted(self.bookmarks.values(), key=lambda bookmark: bookmark.name)

    def save_bookmark(self, source_path: str, bookmark: CameraBookmark) -> None:
        self.bookmarks[bookmark.name] = bookmark

    def delete_bookmark(self, source_path: str, name: str) -> None:
        del self.bookmarks[name]

    def add_reference(self, image: Path) -> str:
        self.added_images.append(image)
        relative_path = f"SourceArt/References/{image.name}"
        self.references.append(Reference(path=relative_path, tag="References"))
        return relative_path

    def convert_export(self, export_path: str) -> ConvertedExport:
        released = self.released_conversions.setdefault(export_path, threading.Event())
        if not released.wait(timeout=10):
            raise TimeoutError(f"The test never released the conversion of {export_path}")
        stats = ExportStats(
            converter_version=1,
            triangles=7896,
            materials=("Body", "Metal"),
            bounds_min_cm=(-3, -28, -11),
            bounds_max_cm=(3, 70, 16),
        )
        glb_path = self.root / "cache" / f"{Path(export_path).stem}.glb"
        return ConvertedExport(export=export_path, sha256="0" * 64, glb_path=glb_path, stats=stats)

    def release(self, export_path: str) -> None:
        self.released_conversions.setdefault(export_path, threading.Event()).set()


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[FakeBackend]:
    backend = FakeBackend(tmp_path)
    yield backend
    # Let any conversion a test left waiting finish, so no worker thread outlives the test.
    for released in backend.released_conversions.values():
        released.set()


@pytest.fixture
def studio(backend: FakeBackend, wait_until: WaitUntil) -> Studio:
    studio = Studio(backend, TaskRunner())
    studio.openProject("Fake")
    wait_until(lambda: studio.property("projectName") == "Fake")
    return studio


class TestProject:
    def test_opening_fills_the_tree_and_the_rail(self, studio: Studio) -> None:
        tree_rows = studio.property("sourceRows").rows
        assert [row.name for row in tree_rows] == ["Posters", "Poster", "Weapons", "Carbine"]
        assert [row.path for row in studio.property("referenceRows").rows] == ["SourceArt/References/mood.png"]

    def test_filtering_by_status(self, studio: Studio) -> None:
        studio.setProperty("statusFilter", "in-review")
        assert [row.name for row in studio.property("sourceRows").rows if not row.is_folder] == ["Carbine"]

    def test_setting_a_status_updates_the_tree(self, studio: Studio, wait_until: WaitUntil) -> None:
        studio.selectSource(POSTER)
        studio.setSourceStatus("approved")
        wait_until(lambda: studio.property("selectedStatus") == "approved")


class TestSelection:
    def test_selecting_a_source_shows_its_first_export_and_its_renders(self, studio: Studio) -> None:
        studio.selectSource(CARBINE)

        assert studio.property("selectedExport") == CARBINE_EXPORT
        assert studio.export_paths == [CARBINE_EXPORT, STOCK_EXPORT]
        # Grouped by stem, then in path order within the group.
        assert [path.rsplit("/", 1)[-1] for path in studio.render_paths] == ["Carbine_Front.png", "Carbine_Side.png"]
        assert studio.property("loadPath") == LoadPath.DIRECT
        assert studio.property("modelUrl").endswith(CARBINE_EXPORT)

    def test_a_source_without_exports_shows_its_first_render(self, studio: Studio) -> None:
        studio.selectSource(POSTER)
        assert studio.property("selectedExport") == ""
        assert studio.property("stageImage").endswith("Poster_preview.png")
        assert studio.property("isStageReady")


class TestLoadPath:
    def test_the_converted_glb_replaces_the_direct_load(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        studio.selectSource(CARBINE)
        studio.reportModelLoad(True, "")
        assert not studio.property("isStageReady")

        backend.release(CARBINE_EXPORT)
        wait_until(lambda: studio.property("loadPath") == LoadPath.BLENDER)

        assert studio.property("modelUrl").endswith("cache/SK_Carbine.glb")
        assert studio.property("stats")["triangles"] == 7896
        assert studio.property("stats")["materials"] == 2
        assert not studio.property("isStageReady")
        studio.reportModelLoad(True, "")
        assert studio.property("isStageReady")

    def test_preferring_the_direct_load_keeps_the_fbx(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        studio.setDirectPreferred(True)
        studio.selectSource(CARBINE)
        studio.reportModelLoad(True, "")
        backend.release(CARBINE_EXPORT)
        wait_until(lambda: studio.property("conversionState") == "done")

        assert studio.property("loadPath") == LoadPath.DIRECT
        assert studio.property("stats")["triangles"] == 7896

    def test_a_failed_direct_load_falls_back_to_the_glb(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        studio.setDirectPreferred(True)
        studio.selectSource(CARBINE)
        studio.reportModelLoad(False, "assimp could not read it")
        assert studio.property("loadPath") == LoadPath.DIRECT

        backend.release(CARBINE_EXPORT)
        wait_until(lambda: studio.property("loadPath") == LoadPath.BLENDER)

    def test_a_late_conversion_of_another_export_is_ignored(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        studio.selectSource(CARBINE)
        studio.selectExport(STOCK_EXPORT)
        backend.release(CARBINE_EXPORT)
        backend.release(STOCK_EXPORT)
        wait_until(lambda: studio.property("loadPath") == LoadPath.BLENDER)

        assert studio.property("modelUrl").endswith("cache/SM_Stock.glb")

    def test_a_change_on_disk_reloads_the_selected_export(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        backend.release(CARBINE_EXPORT)
        studio.selectSource(CARBINE)
        wait_until(lambda: studio.property("loadPath") == LoadPath.BLENDER)
        revision = studio.property("modelRevision")

        assert backend.on_event is not None
        backend.on_event(CatalogEvent(change=CatalogChange(), touched=(CARBINE,)))
        wait_until(lambda: studio.property("modelRevision") > revision + 1)
        assert studio.property("selectedExport") == CARBINE_EXPORT


class TestBookmarksAndReferences:
    def test_saving_and_deleting_a_bookmark(self, studio: Studio, wait_until: WaitUntil) -> None:
        studio.selectSource(CARBINE)
        studio.saveBookmark(" hero ", 1, 2, 3, 225, -22, 300)
        wait_until(lambda: len(studio.property("bookmarkRows").rows) == 1)

        row = studio.property("bookmarkRows").rows[0]
        assert (row.name, row.target_y, row.yaw, row.distance) == ("hero", 2, 225, 300)

        studio.deleteBookmark("hero")
        wait_until(lambda: len(studio.property("bookmarkRows").rows) == 0)

    def test_dropped_images_are_added_and_listed(
        self,
        studio: Studio,
        backend: FakeBackend,
        tmp_path: Path,
        wait_until: WaitUntil,
    ) -> None:
        image = tmp_path / "concept.png"
        image.write_bytes(b"png")
        studio.addReferences([image.as_uri()])
        wait_until(lambda: len(studio.property("referenceRows").rows) == 2)
        assert backend.added_images == [image]

    def test_images_dropped_from_qml_arrive_as_qurls(
        self,
        studio: Studio,
        backend: FakeBackend,
        tmp_path: Path,
        wait_until: WaitUntil,
    ) -> None:
        image = tmp_path / "dropped.png"
        image.write_bytes(b"png")
        studio.addReferences([QUrl.fromLocalFile(str(image))])
        wait_until(lambda: len(studio.property("referenceRows").rows) == 2)
        assert backend.added_images == [image]

    def test_a_remote_url_is_not_added(self, studio: Studio, backend: FakeBackend) -> None:
        studio.addReferences([QUrl("https://example.com/concept.png")])
        assert backend.added_images == []
        assert "local image" in studio.property("message")
