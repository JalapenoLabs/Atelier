"""The window's view-model, driven through a fake backend: selection, the load path and background work."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QColor, QImage

from atelier.gui.backend import DaemonDisconnected, StudioEvent
from atelier.gui.markup import flatten_markup
from atelier.gui.studio import DaemonState, LoadPath, Studio
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
from atelierd.rpc.protocol import DaemonInfo
from atelierd.workers import (
    Approval,
    ApprovalEvent,
    ApprovalStatus,
    ChatItem,
    ChatItemKind,
    Feedback,
    FeedbackRequest,
    Markup,
    Pin,
    Stroke,
    StrokeTool,
    WorkerEvent,
    WorkerEventKind,
    WorkerInfo,
    WorkerSnapshot,
    WorkerStatus,
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
        self.workers: dict[str, WorkerSnapshot] = {}
        self.prompts: list[tuple[str, str, int | None]] = []
        self.pinned: dict[str, list[str]] = {}
        self.feedback_requests: list[FeedbackRequest] = []
        self.approvals: list[Approval] = []
        self.answers: list[tuple[str, bool, str]] = []
        self.is_daemon_running = True

    @property
    def project_name(self) -> str:
        return "Fake"

    def connect(self, on_event: Callable[[StudioEvent], None]) -> DaemonInfo:
        if not self.is_daemon_running:
            raise ConnectionError("No daemon")
        self.on_event = on_event
        return DaemonInfo(pid=1, socket_path=self.root / "atelier.sock", http_port=0)

    def start_daemon(self) -> str:
        self.is_daemon_running = True
        return "Started"

    def list_projects(self) -> list[str]:
        return ["Fake"]

    def open_project(self, reference: str) -> ScanResult:
        return ScanResult(sources=tuple(self.sources), references=tuple(self.references), change=CatalogChange())

    def close(self) -> None:
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

    def get_worker(self, source_path: str) -> WorkerSnapshot:
        pinned = tuple(self.pinned.get(source_path, []))
        snapshot = self.workers.get(source_path, WorkerSnapshot(info=WorkerInfo(source=source_path)))
        return snapshot.model_copy(update={"info": snapshot.info.model_copy(update={"pinned_references": pinned})})

    def send_prompt(self, source_path: str, prompt: str, feedback_id: int | None = None) -> ChatItem:
        self.prompts.append((source_path, prompt, feedback_id))
        return ChatItem(index=0, kind=ChatItemKind.USER, created_at="2026-10-26T12:00:00+00:00", text=prompt)

    def interrupt(self, source_path: str) -> None:
        pass

    def new_session(self, source_path: str) -> None:
        self.workers.pop(source_path, None)

    def stop_worker(self, source_path: str) -> None:
        pass

    def pin_references(self, source_path: str, reference_paths: list[str]) -> list[str]:
        self.pinned[source_path] = sorted(reference_paths)
        return self.pinned[source_path]

    def create_feedback(self, request: FeedbackRequest) -> Feedback:
        self.feedback_requests.append(request)
        folder = self.root / "feedback" / "1"
        return Feedback(
            id=1,
            source=request.source,
            created_at="2026-10-26T12:00:00+00:00",
            folder=folder,
            capture_path=folder / "capture.png",
            annotated_path=folder / "annotated.png",
            pins=request.markup.pins,
        )

    def list_approvals(self) -> list[Approval]:
        return list(self.approvals)

    def answer_approval(self, approval_id: str, is_allowed: bool, message: str) -> Approval:
        self.answers.append((approval_id, is_allowed, message))
        approval = next(approval for approval in self.approvals if approval.id == approval_id)
        status = ApprovalStatus.ALLOWED if is_allowed else ApprovalStatus.DENIED
        return approval.model_copy(update={"status": status})

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
def settings(tmp_path: Path) -> QSettings:
    """The window's settings in a file of the test's own, never the user's configuration."""
    return QSettings(str(tmp_path / "Atelier.conf"), QSettings.Format.IniFormat)


@pytest.fixture
def studio(backend: FakeBackend, settings: QSettings, wait_until: WaitUntil) -> Studio:
    studio = Studio(backend, TaskRunner(), settings)
    # Asked for before the daemon answers, as the window's remembered project is: it opens once connected.
    studio.openProject("Fake")
    studio.connectDaemon()
    wait_until(lambda: studio.property("projectName") == "Fake")
    return studio


class TestProject:
    def test_opening_fills_the_tree_and_the_rail(self, studio: Studio) -> None:
        tree_rows = studio.property("sourceRows").rows
        assert [row.name for row in tree_rows] == ["Posters", "Poster", "Weapons", "Carbine"]
        assert [row.path for row in studio.property("referenceRows").rows] == ["SourceArt/References/mood.png"]

    def test_filtering_by_status(self, studio: Studio) -> None:
        studio.setProperty("statusFilter", "in-review")
        assert [row.name for row in studio.property("sourceRows").rows if row.kind == "source"] == ["Carbine"]

    def test_setting_a_status_updates_the_tree(self, studio: Studio, wait_until: WaitUntil) -> None:
        studio.selectSource(POSTER)
        studio.setSourceStatus("approved")
        wait_until(lambda: studio.property("selectedStatus") == "approved")


class _CountingTaskRunner(TaskRunner):
    """A `TaskRunner` that counts the conversions it is asked for, as they are submitted."""

    def __init__(self) -> None:
        super().__init__()
        self.conversion_count = 0

    def run_conversion(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[Exception], None],
    ) -> None:
        self.conversion_count += 1
        super().run_conversion(work, on_done, on_error)


class _OtherProjectBackend(FakeBackend):
    @property
    def project_name(self) -> str:
        return "Other"


def _tree_names(studio: Studio) -> list[str]:
    return [row.name for row in studio.property("sourceRows").rows]


def _cursor_name(studio: Studio) -> str:
    return str(studio.property("sourceRows").rows[studio.property("treeCursorIndex")].name)


class TestTree:
    def test_clicking_an_export_selects_its_source_and_loads_only_that_export(
        self,
        backend: FakeBackend,
        settings: QSettings,
        wait_until: WaitUntil,
    ) -> None:
        tasks = _CountingTaskRunner()
        studio = Studio(backend, tasks, settings)
        studio.connectDaemon()
        studio.openProject("Fake")
        wait_until(lambda: studio.property("projectName") == "Fake")

        studio.toggleTreeRow(CARBINE)
        studio.activateTreeRow(STOCK_EXPORT)

        assert studio.property("selectedSource") == CARBINE
        assert studio.property("selectedExport") == STOCK_EXPORT
        assert studio.property("modelUrl").endswith(STOCK_EXPORT)
        assert tasks.conversion_count == 1

    def test_clicking_a_source_shows_its_first_export_even_after_another_was_picked(self, studio: Studio) -> None:
        studio.toggleTreeRow(CARBINE)
        studio.activateTreeRow(STOCK_EXPORT)
        studio.activateTreeRow(CARBINE)
        assert studio.property("selectedExport") == CARBINE_EXPORT

    def test_clicking_a_folder_opens_and_closes_it(self, studio: Studio) -> None:
        studio.activateTreeRow("SourceArt/Weapons")
        assert _tree_names(studio) == ["Posters", "Poster", "Weapons"]
        studio.activateTreeRow("SourceArt/Weapons")
        assert _tree_names(studio) == ["Posters", "Poster", "Weapons", "Carbine"]

    def test_filtering_by_an_export_name_opens_its_source(self, studio: Studio) -> None:
        studio.setProperty("filterText", "stock")
        assert _tree_names(studio) == ["Weapons", "Carbine", "SM_Stock"]

    def test_the_expansion_is_remembered_per_project(
        self,
        studio: Studio,
        backend: FakeBackend,
        settings: QSettings,
        tmp_path: Path,
        wait_until: WaitUntil,
    ) -> None:
        studio.toggleTreeRow(CARBINE)
        studio.toggleTreeRow("SourceArt/Posters")
        studio.close()

        reopened = Studio(backend, TaskRunner(), settings)
        reopened.connectDaemon()
        reopened.openProject("Fake")
        wait_until(lambda: reopened.property("projectName") == "Fake")
        assert _tree_names(reopened) == ["Posters", "Weapons", "Carbine", "SK_Carbine", "SM_Stock"]
        reopened.close()

        other = Studio(_OtherProjectBackend(tmp_path), TaskRunner(), settings)
        other.connectDaemon()
        other.openProject("Other")
        wait_until(lambda: other.property("projectName") == "Other")
        assert _tree_names(other) == ["Posters", "Poster", "Weapons", "Carbine"]
        other.close()

    def test_the_keyboard_moves_opens_closes_and_picks(self, studio: Studio) -> None:
        studio.moveTreeCursor(1)
        assert _cursor_name(studio) == "Posters"
        studio.moveTreeCursor(3)
        assert _cursor_name(studio) == "Carbine"
        # Past the end stays on the last row.
        studio.moveTreeCursor(5)
        assert _cursor_name(studio) == "Carbine"

        studio.expandAtTreeCursor()
        assert _tree_names(studio)[-2:] == ["SK_Carbine", "SM_Stock"]
        assert _cursor_name(studio) == "Carbine"
        studio.expandAtTreeCursor()
        assert _cursor_name(studio) == "SK_Carbine"
        studio.moveTreeCursor(1)
        studio.activateTreeCursor()
        assert studio.property("selectedExport") == STOCK_EXPORT

        studio.collapseAtTreeCursor()
        assert _cursor_name(studio) == "Carbine"
        studio.collapseAtTreeCursor()
        assert "SM_Stock" not in _tree_names(studio)
        studio.collapseAtTreeCursor()
        assert _cursor_name(studio) == "Weapons"

    def test_the_cursor_stays_on_its_row_while_the_rows_around_it_change(self, studio: Studio) -> None:
        studio.activateTreeRow(CARBINE)
        assert studio.property("treeCursorIndex") == 3
        studio.setProperty("filterText", "carb")
        assert studio.property("treeCursorIndex") == 1
        studio.setProperty("filterText", "poster")
        assert studio.property("treeCursorIndex") == -1


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


def _chat_item(index: int, kind: ChatItemKind, **fields: object) -> ChatItem:
    return ChatItem(index=index, kind=kind, created_at="2026-10-26T12:00:00+00:00", **fields)


class TestDaemon:
    def test_with_no_daemon_the_window_offers_to_start_one(
        self,
        backend: FakeBackend,
        settings: QSettings,
        wait_until: WaitUntil,
    ) -> None:
        backend.is_daemon_running = False
        studio = Studio(backend, TaskRunner(), settings)
        studio.openProject("Fake")
        studio.connectDaemon()
        wait_until(lambda: studio.property("daemonState") == DaemonState.MISSING)
        assert studio.property("projectName") == ""

        studio.startDaemon()
        wait_until(lambda: studio.property("projectName") == "Fake")
        assert studio.property("daemonState") == DaemonState.CONNECTED

    def test_losing_the_daemon_says_so(self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil) -> None:
        assert backend.on_event is not None
        backend.on_event(DaemonDisconnected(socket_path=backend.root / "atelier.sock"))
        wait_until(lambda: studio.property("daemonState") == DaemonState.MISSING)


class TestChat:
    def test_selecting_a_source_loads_its_chat_and_events_stream_into_it(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        info = WorkerInfo(source=CARBINE, status=WorkerStatus.IDLE, session_id="abcdef1234")
        backend.workers[CARBINE] = WorkerSnapshot(
            info=info,
            items=(_chat_item(0, ChatItemKind.USER, text="Longer magazine"),),
        )
        studio.selectSource(CARBINE)
        chat = studio.chat
        wait_until(lambda: len(chat.property("rows").rows) == 1)
        assert chat.property("statusLabel") == "Idle"
        assert chat.property("sessionLabel") == "Session abcdef12"

        assert backend.on_event is not None
        running = info.model_copy(update={"status": WorkerStatus.RUNNING})
        tool = _chat_item(1, ChatItemKind.TOOL, tool_name="Bash", tool_summary="blender -b ...")
        backend.on_event(WorkerEvent(kind=WorkerEventKind.ITEM, source=CARBINE, info=running, item=tool))
        wait_until(lambda: len(chat.property("rows").rows) == 2)
        assert chat.property("isTurnRunning")

        done = tool.model_copy(update={"is_done": True, "tool_output": "Saved"})
        backend.on_event(WorkerEvent(kind=WorkerEventKind.ITEM, source=CARBINE, info=running, item=done))
        # Newest first: the list draws them bottom to top.
        wait_until(lambda: chat.property("rows").rows[0].is_done)
        assert [row.index for row in chat.property("rows").rows] == [1, 0]

        # Another source's worker does not touch this chat.
        other = WorkerInfo(source=POSTER, status=WorkerStatus.RUNNING)
        backend.on_event(WorkerEvent(kind=WorkerEventKind.ITEM, source=POSTER, info=other, item=tool))
        studio.chat.send("SAY hello")
        wait_until(lambda: backend.prompts == [(CARBINE, "SAY hello", None)])
        assert len(chat.property("rows").rows) == 2

    def test_pinning_a_reference_marks_it_in_the_rail(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        studio.selectSource(CARBINE)
        wait_until(lambda: studio.chat.property("source") == CARBINE)
        studio.togglePinnedReference("SourceArt/References/mood.png")
        wait_until(lambda: backend.pinned.get(CARBINE) == ["SourceArt/References/mood.png"])

        assert backend.on_event is not None
        info = WorkerInfo(source=CARBINE, pinned_references=("SourceArt/References/mood.png",))
        backend.on_event(WorkerEvent(kind=WorkerEventKind.STATE, source=CARBINE, info=info))
        wait_until(lambda: studio.property("referenceRows").rows[0].is_pinned)


class TestApprovals:
    def _approval(self) -> Approval:
        return Approval(
            id="a1",
            project="Fake",
            source=CARBINE,
            tool_name="Write",
            tool_summary="SourceArt/_lib/run_asset.py",
            reason="Writes a shared path other sources build from: SourceArt/_lib/run_asset.py",
            created_at="2026-10-26T12:00:00+00:00",
        )

    def test_a_raised_approval_lights_the_badge_until_it_is_answered(
        self,
        studio: Studio,
        backend: FakeBackend,
        wait_until: WaitUntil,
    ) -> None:
        approval = self._approval()
        backend.approvals.append(approval)
        assert backend.on_event is not None
        backend.on_event(ApprovalEvent(approval=approval))
        wait_until(lambda: studio.property("approvalCount") == 1)
        row = studio.property("approvalRows").rows[0]
        assert (row.source_name, row.is_in_open_project) == ("Carbine", True)

        studio.answerApproval("a1", True, " fine ")
        wait_until(lambda: studio.property("approvalCount") == 0)
        assert backend.answers == [("a1", True, "fine")]


class TestMarkup:
    def _render(self, tmp_path: Path) -> Path:
        image = QImage(1000, 500, QImage.Format.Format_RGB32)
        image.fill(QColor("#808080"))
        path = tmp_path / "render.png"
        assert image.save(str(path))
        return path

    def test_markup_on_a_render_is_sent_as_feedback_with_its_pins(
        self,
        studio: Studio,
        backend: FakeBackend,
        tmp_path: Path,
        wait_until: WaitUntil,
    ) -> None:
        studio.selectSource(CARBINE)
        markup = studio.markup
        markup.openImage(QUrl.fromLocalFile(str(self._render(tmp_path))).toString(), "Carbine_Side.png")
        assert markup.is_open
        assert studio.property("isStageReady")

        markup.setTool("arrow")
        markup.addStroke("arrow", [[0.1, 0.1], [0.5, 0.5]])
        markup.addPin(0.75, 0.5)
        markup.setPinNote(1, "Magazine 2 cm longer")
        markup.addPin(0.2, 0.8)
        markup.undo()
        markup.send("Fix the marked spots")

        wait_until(lambda: backend.prompts == [(CARBINE, "Fix the marked spots", 1)])
        [request] = backend.feedback_requests
        assert [stroke.tool for stroke in request.markup.strokes] == [StrokeTool.ARROW]
        assert [(pin.number, pin.note) for pin in request.markup.pins] == [(1, "Magazine 2 cm longer")]
        assert request.export == CARBINE_EXPORT
        wait_until(lambda: not markup.is_open)

    def test_flattening_draws_the_marks_at_full_resolution(self, qt_application: object, tmp_path: Path) -> None:
        capture = QImage(str(self._render(tmp_path)))
        drawn = Markup(
            strokes=(Stroke(tool=StrokeTool.BOX, color="#ff4d4d", points=((0.1, 0.1), (0.4, 0.9))),),
            pins=(Pin(number=1, x=0.8, y=0.5, note="here"),),
        )
        flattened = flatten_markup(capture, drawn)

        assert (flattened.width(), flattened.height()) == (1000, 500)
        # The box's left edge at x = 0.1, the untouched middle, and the pin's red disc at (0.8, 0.5), off its number.
        box_edge = flattened.pixelColor(100, 250)
        assert box_edge.red() > 230 and box_edge.green() < 110
        assert flattened.pixelColor(600, 250).name() == "#808080"
        pin_disc = flattened.pixelColor(800, 250 - 10)
        assert pin_disc.red() > 200 and pin_disc.green() < 150
