"""The Review mode's grid, driven through a fake backend: tiles, heroes, selection, bulk decisions and job badges."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fake_backend import CARBINE, CARBINE_EXPORT, FakeBackend
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication, QImage, QImageReader
from PySide6.QtQml import QQmlApplicationEngine, QQmlError
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest

from atelier.gui.app import _MAIN_QML
from atelier.gui.review import THUMBNAIL_FOLDER, DecisionNote, ReviewBoard, ReviewTile
from atelier.gui.studio import Studio
from atelier.gui.tasks import TaskRunner
from atelier.gui.thumbnails import THUMBNAIL_SIZE, ThumbnailCache
from atelierd.config import ReviewVerdict
from atelierd.review import (
    Decision,
    DecisionJob,
    DecisionJobEvent,
    DecisionJobState,
    ReviewAttachment,
    ReviewDelivery,
    ReviewEvent,
    ReviewEventKind,
    ReviewItem,
    ReviewLink,
    ReviewQueue,
    ReviewQueueTab,
)
from atelierd.revisions import RenderState, Revision, RevisionTrigger, SourceHistory, StandardRenderSet

WaitUntil = Callable[[Callable[[], bool]], None]

CONTROL = Qt.KeyboardModifier.ControlModifier.value
SHIFT = Qt.KeyboardModifier.ShiftModifier.value
DELIVERED_AT = "2026-11-01T00:00:00+00:00"


def make_image(path: Path, width: int, height: int, color: str = "#3a6ea5") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    image.save(str(path))
    return path


def make_item(
    ticket: str,
    summary: str,
    files: tuple[Path, ...] = (),
    tags: tuple[str, ...] = ("Art asset",),
    has_delivery: bool = True,
    link: ReviewLink | None = None,
) -> ReviewItem:
    attachments = tuple(
        ReviewAttachment(
            name=file_path.name,
            path=file_path,
            mime_type="image/png" if file_path.suffix == ".png" else "audio/mpeg",
        )
        for file_path in files
    )
    delivery = ReviewDelivery(
        comment_id=f"4-{ticket}",
        author="claude",
        created_at=DELIVERED_AT,
        text="Delivered",
        attachments=attachments,
    )
    return ReviewItem(
        ticket=ticket,
        summary=summary,
        stage="Review",
        tags=tags,
        url=f"https://tracker.example/issue/{ticket}",
        updated_at=DELIVERED_AT,
        queues=("Art",),
        delivery=delivery if has_delivery else None,
        link=link,
    )


def make_queue(root: Path) -> ReviewQueue:
    """Four art tickets (one with only a linked source's render, one a sound) and a UI design."""
    delivery = root / "state" / "review"
    items = (
        make_item("WAY-60", "[Art] Carbine", (make_image(delivery / "WAY-60" / "hero.png", 1600, 1200),)),
        make_item("WAY-61", "[Art] Wall", (make_image(delivery / "WAY-61" / "wall.png", 800, 400, "#a5603a"),)),
        make_item(
            "WAY-62",
            "[Art] Stock",
            has_delivery=False,
            link=ReviewLink(source=CARBINE, exports=(CARBINE_EXPORT,), ticket_file="SourceArt/x/reference.json"),
        ),
        make_item("WAY-63", "[Audio] Door", (delivery / "WAY-63" / "door.mp3",), tags=("Audio",)),
        make_item(
            "WAY-70",
            "[UI design] Build menu",
            (make_image(delivery / "WAY-70" / "menu.png", 640, 480, "#5cc285"),),
            tags=("UI design",),
        ),
    )
    return ReviewQueue(
        is_configured=True,
        tabs=(
            ReviewQueueTab(name="Art", query="tag: {Art asset}", tickets=("WAY-60", "WAY-61", "WAY-62", "WAY-63")),
            ReviewQueueTab(name="UI", query="tag: {UI design}", tickets=("WAY-70",)),
        ),
        items=items,
        refreshed_at=DELIVERED_AT,
    )


def carbine_history(root: Path) -> SourceHistory:
    revision = Revision(id=7, source=CARBINE, number=2, created_at=DELIVERED_AT, trigger=RevisionTrigger.CHANGE)
    hero = make_image(root / "state" / "renders" / "7" / "hero.png", 512, 512, "#7f8da8")
    return SourceHistory(
        source=CARBINE,
        revisions=(revision,),
        standard_renders=(
            StandardRenderSet(
                revision_id=7,
                export=CARBINE_EXPORT,
                export_sha256="0" * 64,
                state=RenderState.DONE,
                images={"hero": hero},
            ),
        ),
    )


def make_job(
    job_id: int, ticket: str, state: DecisionJobState, created_at: str = "2026-11-02T00:00:00+00:00"
) -> DecisionJob:
    return DecisionJob(
        id=job_id,
        decision=Decision(ticket=ticket, verdict=ReviewVerdict.CHANGES, takes=("Chunkier grip",)),
        state=state,
        created_at=created_at,
        updated_at=created_at,
        error="claude -p timed out" if state is DecisionJobState.FAILED else "",
    )


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[FakeBackend]:
    backend = FakeBackend(tmp_path)
    for reference in backend.references:
        make_image(tmp_path / reference.path, 64, 64)
    backend.review_queue = make_queue(tmp_path)
    backend.histories[CARBINE] = carbine_history(tmp_path)
    yield backend
    for released in backend.released_conversions.values():
        released.set()


@pytest.fixture
def studio(backend: FakeBackend, tmp_path: Path, wait_until: WaitUntil) -> Iterator[Studio]:
    studio = Studio(backend, TaskRunner(), QSettings(str(tmp_path / "Atelier.conf"), QSettings.Format.IniFormat))
    studio.connectDaemon()
    studio.openProject("Fake")
    wait_until(lambda: studio.review.property("hasRefreshed"))
    yield studio
    studio.close()


def tiles(board: ReviewBoard) -> list[ReviewTile]:
    return [row for row in board.property("tileRows").rows if isinstance(row, ReviewTile)]


def tile(board: ReviewBoard, ticket: str) -> ReviewTile:
    return next(found for found in tiles(board) if found.ticket == ticket)


def heroes_settled(board: ReviewBoard) -> bool:
    return all(found.hero_note != "Loading…" for found in tiles(board))


class TestTiles:
    def test_the_first_tab_shows_its_tickets_in_queue_order(self, studio: Studio) -> None:
        board = studio.review
        assert board.property("isConfigured")
        assert [(row.name, row.count) for row in board.property("tabRows").rows] == [("Art", 4), ("UI", 1)]
        assert board.property("currentTab") == "Art"
        assert [found.ticket for found in tiles(board)] == ["WAY-60", "WAY-61", "WAY-62", "WAY-63"]
        assert tile(board, "WAY-60").tags == ["Art asset"]
        assert board.property("ticketCount") == 5

        board.selectTab("UI")
        assert [found.ticket for found in tiles(board)] == ["WAY-70"]

    def test_a_project_without_a_queue_says_so(
        self, backend: FakeBackend, tmp_path: Path, wait_until: WaitUntil
    ) -> None:
        backend.review_queue = ReviewQueue(is_configured=False, refreshed_at=DELIVERED_AT)
        studio = Studio(backend, TaskRunner(), QSettings(str(tmp_path / "Other.conf"), QSettings.Format.IniFormat))
        studio.connectDaemon()
        studio.openProject("Fake")
        wait_until(lambda: studio.review.property("hasRefreshed"))
        assert not studio.review.property("isConfigured")
        assert tiles(studio.review) == []
        studio.close()

    def test_each_tile_shows_its_hero_as_a_cached_thumbnail(
        self, studio: Studio, tmp_path: Path, wait_until: WaitUntil
    ) -> None:
        board = studio.review
        wait_until(lambda: heroes_settled(board))

        carbine = tile(board, "WAY-60")
        thumbnail_path = Path(QUrl(carbine.thumbnail_url).toLocalFile())
        assert thumbnail_path.parent == tmp_path / "state" / THUMBNAIL_FOLDER
        size = QImageReader(str(thumbnail_path)).size()
        # Decoded to thumbnail size, keeping its 4:3 shape.
        assert (size.width(), size.height()) == (THUMBNAIL_SIZE, THUMBNAIL_SIZE * 3 // 4)
        assert QUrl(carbine.hero_url).toLocalFile() == str(tmp_path / "state" / "review" / "WAY-60" / "hero.png")

        # No delivery: the linked source's newest standard hero render.
        stock = tile(board, "WAY-62")
        assert QUrl(stock.hero_url).toLocalFile() == str(tmp_path / "state" / "renders" / "7" / "hero.png")
        assert stock.thumbnail_url

        # A sound: no image to show, and the tile says what it has.
        door = tile(board, "WAY-63")
        assert (door.thumbnail_url, door.hero_note) == ("", "1 file, no image")

    def test_a_thumbnail_is_decoded_once(self, tmp_path: Path, qt_application: object) -> None:
        image = make_image(tmp_path / "big.png", 2000, 1000)
        cache = ThumbnailCache(tmp_path / "thumbnails")

        first = cache.thumbnail(image)
        assert first is not None
        made_at = first.stat().st_mtime_ns

        assert cache.thumbnail(image) == first
        assert first.stat().st_mtime_ns == made_at

    def test_an_image_that_cannot_be_decoded_has_no_thumbnail(self, tmp_path: Path, qt_application: object) -> None:
        broken = tmp_path / "broken.png"
        broken.write_bytes(b"not a png")

        assert ThumbnailCache(tmp_path / "thumbnails").thumbnail(broken) is None


class TestSelection:
    def test_click_ctrl_click_and_shift_click(self, studio: Studio) -> None:
        board = studio.review
        board.clickTile(0, 0)
        assert board.selected_tickets == ["WAY-60"]

        board.clickTile(2, CONTROL)
        assert board.selected_tickets == ["WAY-60", "WAY-62"]

        board.clickTile(0, CONTROL)
        assert board.selected_tickets == ["WAY-62"]

        # A range from the last tile clicked, replacing the selection; with Ctrl, added to it.
        board.clickTile(1, 0)
        board.clickTile(3, SHIFT)
        assert board.selected_tickets == ["WAY-61", "WAY-62", "WAY-63"]
        board.clickTile(0, 0)
        board.clickTile(1, SHIFT)
        assert [found.is_selected for found in tiles(board)] == [True, True, False, False]

    def test_keyboard_moves_selects_and_extends(self, studio: Studio) -> None:
        board = studio.review
        # The grid starts with the cursor on its first tile.
        assert board.property("cursorIndex") == 0
        board.moveCursor(1, False)
        board.toggleAtCursor()
        assert board.selected_tickets == ["WAY-61"]

        board.moveCursor(2, True)
        assert board.selected_tickets == ["WAY-61", "WAY-62", "WAY-63"]
        assert board.property("cursorIndex") == 3

        # The cursor stops at the ends.
        board.moveCursor(10, False)
        assert board.property("cursorIndex") == 3

        board.selectAll()
        assert board.property("selectedCount") == 4
        board.clearSelection()
        assert board.property("selectedCount") == 0

    def test_a_drag_box_selects_what_it_covers_and_ctrl_adds(self, studio: Studio) -> None:
        board = studio.review
        board.clickTile(3, 0)

        board.beginBoxSelection(False)
        board.updateBoxSelection([0, 1])
        assert board.selected_tickets == ["WAY-60", "WAY-61"]
        board.updateBoxSelection([1])
        assert board.selected_tickets == ["WAY-61"]
        board.endBoxSelection()

        board.beginBoxSelection(True)
        board.updateBoxSelection([3])
        board.endBoxSelection()
        assert board.selected_tickets == ["WAY-61", "WAY-63"]

    def test_switching_tabs_starts_a_fresh_selection(self, studio: Studio) -> None:
        board = studio.review
        board.selectAll()
        board.selectTab("UI")
        assert board.property("selectedCount") == 0


class TestDecisions:
    def test_bulk_approve_queues_one_decision_for_every_selected_ticket(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        board = studio.review
        board.clickTile(0, 0)
        board.clickTile(2, SHIFT)

        assert board.decisionCount() == 3
        assert board.submitDecision("approve")
        wait_until(lambda: tile(board, "WAY-60").job_state == "queued")

        assert [(request.tickets, request.verdict, request.takes) for request in backend.decisions] == [
            (("WAY-60", "WAY-61", "WAY-62"), ReviewVerdict.APPROVE, ())
        ]
        assert tile(board, "WAY-61").job_label == "Approve · Queued"
        assert board.property("selectedCount") == 0

    def test_bulk_changes_needs_words_and_sends_every_take_in_order(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        board = studio.review
        board.selectAll()
        note = board.note

        assert not board.submitDecision("changes")
        assert backend.decisions == []

        # Voice notes arrive as takes; the typed text is the last one.
        note.addTake("The grips are too thin.")
        note.addTake("Overall, warmer metal.")
        note.removeLastTake()
        note.addTake("For all weapons, warmer metal.")
        note.setProperty("draft", "And lose the decals.")
        assert board.submitDecision("changes")
        wait_until(lambda: len(backend.decisions) == 1)

        request = backend.decisions[0]
        assert request.tickets == ("WAY-60", "WAY-61", "WAY-62", "WAY-63")
        assert request.takes == ("The grips are too thin.", "For all weapons, warmer metal.", "And lose the decals.")
        wait_until(lambda: note.property("isEmpty"))

    def test_with_nothing_selected_a_decision_covers_the_tile_under_the_cursor(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        board = studio.review
        board.moveCursor(2, False)
        assert board.submitDecision("approve")
        wait_until(lambda: len(backend.decisions) == 1)
        assert backend.decisions[0].tickets == ("WAY-62",)

    def test_tickets_already_deciding_are_left_out(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        board = studio.review
        board.apply_job_event(DecisionJobEvent(job=make_job(1, "WAY-61", DecisionJobState.WRITING)))
        board.selectAll()

        assert board.submitDecision("approve")
        wait_until(lambda: len(backend.decisions) == 1)
        assert backend.decisions[0].tickets == ("WAY-60", "WAY-62", "WAY-63")

    def test_job_events_badge_tiles_in_place_and_a_failed_job_can_be_retried(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        board = studio.review
        resets: list[None] = []
        board.property("tileRows").modelReset.connect(lambda: resets.append(None))

        failed = make_job(4, "WAY-60", DecisionJobState.FAILED)
        backend.decision_jobs = [failed]
        board.apply_job_event(DecisionJobEvent(job=failed))
        carbine = tile(board, "WAY-60")
        assert (carbine.job_state, carbine.job_label, carbine.job_error) == (
            "failed",
            "Changes · Failed",
            "claude -p timed out",
        )

        board.retryJob("WAY-60")
        wait_until(lambda: tile(board, "WAY-60").job_state == "queued")
        assert backend.retried == [4]
        assert resets == []

    def test_a_job_from_an_earlier_round_does_not_badge_the_new_delivery(self, studio: Studio) -> None:
        board = studio.review
        board.apply_job_event(
            DecisionJobEvent(job=make_job(2, "WAY-60", DecisionJobState.FAILED, created_at="2026-10-01T00:00:00+00:00"))
        )
        assert tile(board, "WAY-60").job_state == ""

    def test_a_decided_ticket_leaves_without_resetting_the_grid(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        board = studio.review
        resets: list[None] = []
        board.property("tileRows").modelReset.connect(lambda: resets.append(None))
        board.moveCursor(2, False)
        board.clickTile(1, 0)
        board.clickTile(2, CONTROL)

        queue = backend.review_queue
        art, ui = queue.tabs
        backend.review_queue = queue.model_copy(
            update={
                "items": tuple(item for item in queue.items if item.ticket != "WAY-61"),
                "tabs": (art.model_copy(update={"tickets": ("WAY-60", "WAY-62", "WAY-63")}), ui),
            }
        )
        board.apply_review_event(ReviewEvent(kind=ReviewEventKind.DECIDED, removed=("WAY-61",)))
        wait_until(lambda: len(tiles(board)) == 3)

        assert resets == []
        assert board.selected_tickets == ["WAY-62"]
        assert board.property("cursorIndex") == 1
        assert [(row.name, row.count) for row in board.property("tabRows").rows] == [("Art", 3), ("UI", 1)]

    def test_refresh_reads_the_tracker_again(self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil) -> None:
        studio.review.refresh()
        wait_until(lambda: backend.refreshes == 1 and not studio.review.property("isRefreshing"))


class TestFocus:
    def test_opening_a_tile_asks_for_focus_mode_on_its_ticket(self, studio: Studio) -> None:
        board = studio.review
        requested: list[str] = []
        board.focusRequested.connect(requested.append)

        board.openTicket(1)
        assert requested == ["WAY-61"]
        assert board.property("focusedTicket") == "WAY-61"
        assert board.property("focusedTile")["summary"] == "[Art] Wall"

        board.closeFocus()
        assert board.property("focusedTicket") == ""


class TestDecisionNote:
    def test_a_take_with_no_words_is_left_out(self) -> None:
        note = DecisionNote()
        note.addTake("  ")
        note.setProperty("draft", " ")
        assert note.all_takes() == ()
        assert note.property("isEmpty")


# ####################### #
#       THE QML PAGE      #
# ####################### #


@pytest.fixture
def review_window(
    studio: Studio,
    qt_application: QGuiApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_until: WaitUntil,
) -> Iterator[tuple[QQuickWindow, list[QQmlError]]]:
    """The whole window in Review mode, shown on `offscreen` (nothing appears on screen) and holding the keyboard."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    qt_application.setOrganizationName("AtelierTests")
    qt_application.setApplicationName("AtelierTests")
    engine = QQmlApplicationEngine()
    warnings: list[QQmlError] = []
    engine.warnings.connect(warnings.extend)
    engine.rootContext().setContextProperty("studio", studio)
    engine.rootContext().setContextProperty("launchOptions", {"windowWidth": 1200, "windowHeight": 800})
    engine.load(QUrl.fromLocalFile(str(_MAIN_QML)))
    window = engine.rootObjects()[0]
    assert isinstance(window, QQuickWindow), [warning.toString() for warning in warnings]
    window.setProperty("mode", "review")
    window.show()
    window.requestActivate()
    assert QTest.qWaitForWindowActive(window)
    grid = window.findChild(QQuickItem, "reviewGrid")
    assert grid is not None
    wait_until(lambda: grid.hasActiveFocus() and grid.property("count") == 4)
    yield window, warnings
    window.close()


def tile_point(window: QQuickWindow, index: int) -> QPoint:
    """The centre of a tile in window coordinates."""
    grid = window.findChild(QQuickItem, "reviewGrid")
    assert grid is not None
    columns = grid.property("columns")
    cell_width = grid.property("cellWidth")
    cell_height = grid.property("cellHeight")
    local = QPointF((index % columns + 0.5) * cell_width, (index // columns + 0.5) * cell_height)
    return grid.mapToScene(local).toPoint()


class TestReviewPage:
    def test_the_keyboard_selects_and_approves_through_the_note(
        self,
        studio: Studio,
        backend: FakeBackend,
        review_window: tuple[QQuickWindow, list[QQmlError]],
        wait_until: WaitUntil,
    ) -> None:
        window, warnings = review_window
        board = studio.review

        QTest.keyClick(window, Qt.Key.Key_Right)
        QTest.keyClick(window, Qt.Key.Key_Space)
        assert board.selected_tickets == ["WAY-61"]
        QTest.keyClick(window, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        assert board.property("selectedCount") == 4

        # A opens the note; the typed words go to every ticket and Enter sends them.
        QTest.keyClick(window, Qt.Key.Key_A)
        for key in (Qt.Key.Key_N, Qt.Key.Key_I, Qt.Key.Key_C, Qt.Key.Key_E):
            QTest.keyClick(window, key)
        QTest.keyClick(window, Qt.Key.Key_Return)
        wait_until(lambda: len(backend.decisions) == 1)

        request = backend.decisions[0]
        assert (request.tickets, request.verdict, request.takes) == (
            ("WAY-60", "WAY-61", "WAY-62", "WAY-63"),
            ReviewVerdict.APPROVE,
            ("nice",),
        )
        assert [warning.toString() for warning in warnings] == []

    def test_clicks_and_a_drag_box_select_and_a_double_click_opens_focus_mode(
        self,
        studio: Studio,
        review_window: tuple[QQuickWindow, list[QQmlError]],
        wait_until: WaitUntil,
    ) -> None:
        window, warnings = review_window
        board = studio.review

        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, tile_point(window, 2))
        assert board.selected_tickets == ["WAY-62"]
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier, tile_point(window, 0))
        assert board.selected_tickets == ["WAY-60", "WAY-62"]

        # A box from the first tile's centre to the second's covers both, and only them.
        start, end = tile_point(window, 0), tile_point(window, 1)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(window, (start + end) / 2)
        QTest.mouseMove(window, end)
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        assert board.selected_tickets == ["WAY-60", "WAY-61"]

        QTest.mouseDClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, tile_point(window, 3))
        wait_until(lambda: board.property("focusedTicket") == "WAY-63")
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait_until(lambda: board.property("focusedTicket") == "")
        assert [warning.toString() for warning in warnings] == []
