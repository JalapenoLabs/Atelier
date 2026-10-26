"""The window's view-model, driven through a fake backend: selection, the load path and background work."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl

from atelier.gui.backend import StudioEvent
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
from atelierd.revisions import (
    STANDARD_VIEWS,
    FileKind,
    RenderState,
    RestoreResult,
    Revision,
    RevisionEvent,
    RevisionEventKind,
    RevisionFile,
    RevisionTrigger,
    SourceHistory,
    StandardRenderSet,
)

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
        self.on_event: Callable[[StudioEvent], None] | None = None
        self.histories: dict[str, SourceHistory] = {}
        self.requested_renders: list[tuple[int, str]] = []
        self.restores: list[tuple[str, int]] = []

    @property
    def project_name(self) -> str:
        return "Fake"

    def list_projects(self) -> list[str]:
        return ["Fake"]

    def open_project(self, reference: str, on_event: Callable[[StudioEvent], None]) -> ScanResult:
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

    def get_history(self, source_path: str) -> SourceHistory:
        return self.histories.get(source_path, SourceHistory(source=source_path))

    def request_standard_renders(self, revision_id: int, export_path: str) -> None:
        self.requested_renders.append((revision_id, export_path))

    def convert_revision_export(self, revision_id: int, export_path: str) -> ConvertedExport:
        return self.convert_export(f"{revision_id}/{export_path}")

    def object_path(self, sha256: str) -> Path:
        return self.root / "objects" / sha256

    def restore_revision(self, source_path: str, revision_id: int) -> RestoreResult:
        self.restores.append((source_path, revision_id))
        history = self.get_history(source_path)
        target = next(revision for revision in history.revisions if revision.id == revision_id)
        return RestoreResult(target=target, pre_restore=history.revisions[0], restored=history.revisions[0])

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


def _carbine_history(root: Path, newest_state: RenderState = RenderState.DONE) -> SourceHistory:
    """Two revisions of the Carbine: r1 (baseline, never rendered) and r2 (its export changed, rendered)."""
    renders_folder = root / "renders"

    def files(export_sha256: str) -> tuple[RevisionFile, ...]:
        return (
            RevisionFile(path=CARBINE, kind=FileKind.BLEND, sha256="b" * 64, size=1),
            RevisionFile(path=CARBINE_EXPORT, kind=FileKind.EXPORT, sha256=export_sha256, size=1),
            RevisionFile(
                path="SourceArt/Weapons/Carbine/Review/Carbine_Front.png",
                kind=FileKind.RENDER,
                sha256="f" * 64,
                size=1,
            ),
        )

    first = Revision(
        id=11, source=CARBINE, number=1, created_at="2026-10-26T10:00:00+00:00", trigger=RevisionTrigger.BASELINE
    )
    first = first.model_copy(update={"files": files("1" * 64)})
    second = Revision(
        id=12, source=CARBINE, number=2, created_at="2026-10-26T11:00:00+00:00", trigger=RevisionTrigger.CHANGE
    )
    second = second.model_copy(update={"files": files("2" * 64)})
    images = {view: renders_folder / "12" / f"SK_Carbine-{view}.png" for view in STANDARD_VIEWS}
    return SourceHistory(
        source=CARBINE,
        revisions=(second, first),
        standard_renders=(
            StandardRenderSet(
                revision_id=12,
                export=CARBINE_EXPORT,
                export_sha256="2" * 64,
                state=newest_state,
                images=images if newest_state is RenderState.DONE else {},
            ),
            StandardRenderSet(
                revision_id=11,
                export=CARBINE_EXPORT,
                export_sha256="1" * 64,
                state=RenderState.MISSING,
            ),
        ),
    )


@pytest.fixture
def carbine_with_history(studio: Studio, backend: FakeBackend, wait_until: WaitUntil) -> Studio:
    backend.histories[CARBINE] = _carbine_history(backend.root)
    studio.selectSource(CARBINE)
    wait_until(lambda: studio.property("hasRevisions"))
    return studio


class TestRevisions:
    def test_the_filmstrip_lists_revisions_newest_first_and_follows_the_newest(
        self,
        carbine_with_history: Studio,
    ) -> None:
        rows = carbine_with_history.property("revisionRows").rows

        assert [(row.label, row.trigger) for row in rows] == [("r2", "Files changed"), ("r1", "First seen")]
        assert rows[0].thumbnail.endswith("renders/12/SK_Carbine-hero.png")
        assert rows[1].thumbnail == ""
        assert carbine_with_history.property("isFollowingNewest")
        assert carbine_with_history.property("stagedLabel") == "r2"

    def test_the_standard_renders_lead_the_gallery(self, carbine_with_history: Studio) -> None:
        paths = carbine_with_history.render_paths

        assert paths[: len(STANDARD_VIEWS)] == [f"Standard/{view}" for view in STANDARD_VIEWS]
        carbine_with_history.showRender("Standard/side")
        assert carbine_with_history.property("stageImage").endswith("renders/12/SK_Carbine-side.png")
        assert carbine_with_history.property("stageImageName") == "r2 side"

    def test_a_standard_set_still_rendering_says_so(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        backend.histories[CARBINE] = _carbine_history(backend.root, newest_state=RenderState.PENDING)
        studio.selectSource(CARBINE)
        wait_until(lambda: studio.property("hasRevisions"))

        first_row = studio.property("renderRows").rows[0]
        assert (first_row.stem, first_row.name, first_row.url) == ("Standard", "Rendering…", "")

    def test_an_older_revision_is_shown_as_snapshotted_and_its_renders_are_asked_for(
        self,
        carbine_with_history: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        carbine_with_history.selectRevision(11)

        assert not carbine_with_history.property("isFollowingNewest")
        # No file of the old export exists for Qt to load, so the stage waits for Blender's conversion.
        assert carbine_with_history.property("modelUrl") == ""
        project_render = next(row for row in carbine_with_history.property("renderRows").rows if row.stem != "Standard")
        assert project_render.url.endswith(f"objects/{'f' * 64}")
        wait_until(lambda: (11, CARBINE_EXPORT) in backend.requested_renders)

        backend.release(f"11/{CARBINE_EXPORT}")
        wait_until(lambda: carbine_with_history.property("loadPath") == LoadPath.BLENDER)
        assert carbine_with_history.property("modelUrl").endswith("cache/SK_Carbine.glb")

        carbine_with_history.followNewest()
        assert carbine_with_history.property("isFollowingNewest")
        assert carbine_with_history.property("modelUrl").endswith(CARBINE_EXPORT)

    def test_compare_puts_the_previous_revision_beside_the_staged_one(
        self,
        carbine_with_history: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        carbine_with_history.startCompare()

        left = carbine_with_history.property("compareLeft")
        right = carbine_with_history.property("compareRight")
        assert carbine_with_history.property("isComparing")
        assert (left.property("label"), left.property("imageNote")) == ("r1  First seen", "Not rendered")
        assert right.property("imageUrl").endswith("renders/12/SK_Carbine-hero.png")
        assert [row.is_base for row in carbine_with_history.property("revisionRows").rows] == [False, True]

        carbine_with_history.setCompareView("top")
        assert right.property("imageUrl").endswith("renders/12/SK_Carbine-top.png")

        carbine_with_history.setCompareMode("models")
        backend.release(f"11/{CARBINE_EXPORT}")
        backend.release(f"12/{CARBINE_EXPORT}")
        wait_until(lambda: bool(left.property("modelUrl")) and bool(right.property("modelUrl")))
        assert carbine_with_history.property("isStageReady")

    def test_the_first_revision_has_nothing_to_compare_with(self, carbine_with_history: Studio) -> None:
        carbine_with_history.selectRevision(11)
        carbine_with_history.startCompare()

        assert not carbine_with_history.property("isComparing")
        assert "first revision" in carbine_with_history.property("message")

    def test_restoring_returns_the_stage_to_the_newest(
        self,
        carbine_with_history: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        carbine_with_history.selectRevision(11)
        carbine_with_history.restoreRevision(11)

        wait_until(lambda: backend.restores == [(CARBINE, 11)])
        wait_until(lambda: carbine_with_history.property("isFollowingNewest"))
        assert "Restored to r1" in carbine_with_history.property("message")

    def test_a_new_revision_refreshes_the_filmstrip(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        studio.selectSource(CARBINE)
        assert not studio.property("hasRevisions")

        backend.histories[CARBINE] = _carbine_history(backend.root)
        assert backend.on_event is not None
        backend.on_event(RevisionEvent(kind=RevisionEventKind.RECORDED, source=CARBINE, revision_id=12))

        wait_until(lambda: len(studio.property("revisionRows").rows) == 2)
