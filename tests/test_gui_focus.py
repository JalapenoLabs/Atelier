"""Review focus mode against a fake backend: the stage, moving between tickets, deciding, drawing, and its page."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fake_backend import CARBINE, CARBINE_EXPORT, FakeBackend
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, QSettings, Qt, QUrl
from PySide6.QtGui import QColor, QGuiApplication, QImage
from PySide6.QtQml import QQmlApplicationEngine, QQmlError
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtTest import QTest
from test_gui_review import carbine_history, make_image, make_item, opened_urls  # noqa: F401

from atelier.gui.app import _MAIN_QML
from atelier.gui.focus import DRAFT_DIR_NAME, ReviewFocus, StageKind, ThreadRow, stage_entries
from atelier.gui.markup import BLANK_CANVAS_SIZE, DecisionDrawing, flatten_markup
from atelier.gui.studio import Studio
from atelier.gui.tasks import TaskRunner
from atelierd.config import ReviewImagesConfig, ReviewVerdict
from atelierd.review import (
    ReviewComment,
    ReviewEvent,
    ReviewEventKind,
    ReviewLink,
    ReviewQueue,
    ReviewQueueTab,
    arrange_attachments,
)
from atelierd.workers import Markup, Stroke, StrokeTool

WaitUntil = Callable[[Callable[[], bool]], None]

DELIVERED_AT = "2026-11-01T00:00:00+00:00"
LINK = ReviewLink(source=CARBINE, exports=(CARBINE_EXPORT,), ticket_file="SourceArt/x/reference.json")


def make_focus_queue(root: Path) -> ReviewQueue:
    """Four art tickets: a carbine with every kind of file and a linked source, two plain ones, and a sound."""
    delivery = root / "state" / "review"
    carbine_files = (
        make_image(delivery / "WAY-60" / "hero.png", 1600, 1200),
        make_image(delivery / "WAY-60" / "detail.png", 800, 600, "#7a3aa5"),
        make_image(delivery / "WAY-60" / "Carbine_Compare.png", 1600, 800, "#3aa57a"),
        delivery / "WAY-60" / "shot.mp3",
        delivery / "WAY-60" / "source.zip",
    )
    items = (
        make_item("WAY-60", "[Art] Carbine", carbine_files, link=LINK),
        make_item("WAY-61", "[Art] Wall", (make_image(delivery / "WAY-61" / "wall.png", 800, 400, "#a5603a"),)),
        make_item("WAY-62", "[Art] Crate", (make_image(delivery / "WAY-62" / "crate.png", 640, 640, "#a5a53a"),)),
        make_item("WAY-63", "[Audio] Door", (delivery / "WAY-63" / "door.mp3",), tags=("Audio",)),
    )
    # A zip's type says nothing, so it shows as a file by its name.
    carbine = items[0]
    assert carbine.delivery is not None
    attachments = arrange_attachments(
        "WAY-60",
        [
            attachment.model_copy(update={"mime_type": ""}) if attachment.name == "source.zip" else attachment
            for attachment in carbine.delivery.attachments
        ],
        ReviewImagesConfig(hero=("hero.*",), reference=("*_Compare.*",)),
    )
    comments = (
        ReviewComment(
            id="4-1", author="Claude", created_at="2026-10-29T04:53:03+00:00", text="**Round 1**", attachment_names=()
        ),
        ReviewComment(
            id="4-2",
            author="Alex Navarro",
            created_at="2026-10-30T10:00:00+00:00",
            text="Chunkier grip",
            is_reviewer=True,
        ),
        ReviewComment(
            id=carbine.delivery.comment_id,
            author="Claude",
            created_at=DELIVERED_AT,
            text="Round 2\n\n![](hero.png){width=100%}\n\n![](old-round.png)",
            attachment_names=("hero.png", "old-round.png"),
            captions={"hero.png": "Hero", "old-round.png": "Old round"},
        ),
        ReviewComment(id="4-9", author="Claude", created_at=DELIVERED_AT, text="A correction"),
    )
    arranged = carbine.delivery.model_copy(update={"attachments": attachments})
    carbine = carbine.model_copy(update={"delivery": arranged, "comments": comments})
    return ReviewQueue(
        is_configured=True,
        tabs=(ReviewQueueTab(name="Art", query="tag: {Art asset}", tickets=("WAY-60", "WAY-61", "WAY-62", "WAY-63")),),
        items=(carbine, *items[1:]),
        refreshed_at=DELIVERED_AT,
    )


@pytest.fixture
def backend(tmp_path: Path) -> Iterator[FakeBackend]:
    backend = FakeBackend(tmp_path)
    for reference in backend.references:
        make_image(tmp_path / reference.path, 64, 64)
    backend.review_queue = make_focus_queue(tmp_path)
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


def stage_names(focus: ReviewFocus) -> list[str]:
    return [row.name for row in focus.property("stageRows").rows]


def focused(studio: Studio) -> str:
    return str(studio.review.property("focusedTicket"))


def draw_line(drawing: DecisionDrawing, tool: str = "pen") -> None:
    drawing.setTool(tool)
    drawing.addStroke(tool, [[0.2, 0.5], [0.8, 0.5]])


class TestStage:
    def test_art_comes_first_in_the_deliverys_order_then_the_model_reference_sounds_and_files(
        self, studio: Studio
    ) -> None:
        item = studio.review.item("WAY-60")
        assert item is not None
        entries = stage_entries(item)
        assert [(entry.kind, entry.name) for entry in entries] == [
            (StageKind.IMAGE, "Hero"),
            (StageKind.IMAGE, "Detail"),
            (StageKind.MODEL, "3D view"),
            (StageKind.IMAGE, "Carbine compare"),
            (StageKind.AUDIO, "Shot"),
            (StageKind.FILE, "Source"),
        ]
        assert entries[3].file_name == "Carbine_Compare.png"

    def test_a_file_without_a_caption_shows_its_name(self, tmp_path: Path) -> None:
        item = make_item("WAY-61", "[Art] Wall", (make_image(tmp_path / "wall.png", 64, 64),))
        assert stage_entries(item)[0].name == "wall.png"

    def test_opening_a_ticket_shows_its_hero_and_the_filmstrip_switches(self, studio: Studio) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        assert focus.property("ticket") == "WAY-60"
        assert focus.property("positionLabel") == "1 of 4"
        assert focus.property("currentKind") == "image"
        assert focus.property("currentUrl").endswith("/WAY-60/hero.png")

        focus.stepEntry(-1)
        assert focus.property("currentName") == "Source"
        focus.stepEntry(1)
        assert focus.property("stageIndex") == 0
        assert [row.is_current for row in focus.property("stageRows").rows][:2] == [True, False]

    def test_the_model_entry_shows_the_linked_source_on_the_named_export(self, studio: Studio) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        focus.showEntry(2)
        assert focus.property("currentKind") == "model"
        assert studio.property("selectedSource") == CARBINE
        assert studio.property("selectedExport") == CARBINE_EXPORT

    def test_a_ticket_with_nothing_to_show_says_why(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        queue = backend.review_queue
        tab = queue.tabs[0]
        backend.review_queue = queue.model_copy(
            update={
                "items": (*queue.items, make_item("WAY-64", "[Art] Bare", has_delivery=False)),
                "tabs": (tab.model_copy(update={"tickets": (*tab.tickets, "WAY-64")}),),
            }
        )
        studio.review.apply_review_event(ReviewEvent(kind=ReviewEventKind.REFRESHED, added=("WAY-64",)))
        wait_until(lambda: studio.review.property("ticketCount") == 5)
        studio.review.openTicket(4)
        assert studio.focus.property("stageIndex") == -1
        assert studio.focus.property("emptyNote") == "No delivery yet"

    def test_the_shown_and_next_two_tickets_images_are_held_decoded_each_in_the_slot_it_had(
        self, studio: Studio
    ) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        slots = focus.property("prefetchSlots")
        wanted = [url for url in slots if url]
        names = [url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1] for url in wanted]
        assert names == ["WAY-60/hero.png", "WAY-60/detail.png", "WAY-60/Carbine_Compare.png", "WAY-61/wall.png"] + [
            "WAY-62/crate.png"
        ]

        focus.nextTicket()
        moved = focus.property("prefetchSlots")
        # The wall and the crate stay where they were; the carbine's slots are let go. WAY-63 is a sound.
        for url in wanted[3:]:
            assert moved.index(url) == slots.index(url)
        assert not any("/WAY-60/" in url for url in moved)


class TestMovingAndDeciding:
    def test_left_and_right_walk_the_tab_and_the_grid_cursor_follows(self, studio: Studio) -> None:
        focus = studio.focus
        studio.review.openTicket(1)
        focus.nextTicket()
        assert focused(studio) == "WAY-62"
        assert studio.review.property("cursorIndex") == 2
        focus.previousTicket()
        focus.previousTicket()
        assert focused(studio) == "WAY-60"
        focus.previousTicket()
        assert focused(studio) == "WAY-60"
        assert studio.property("message") == "That is the first ticket"

    def test_each_ticket_keeps_its_own_note_while_moving_back_and_forth(self, studio: Studio) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        focus.current_note.addTake("Grip too thin")
        focus.current_note.setProperty("draft", "and the stock")
        focus.nextTicket()
        assert focus.current_note.all_takes() == ()
        focus.previousTicket()
        assert focus.current_note.all_takes() == ("Grip too thin", "and the stock")

        focus.current_note.setTake(0, "Grip far too thin")
        focus.current_note.setTake(1, "  ")
        assert focus.current_note.all_takes() == ("Grip far too thin", "and the stock")

    def test_approving_opens_the_next_ticket_at_once_and_the_note_is_cleared_once_queued(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        focus.current_note.setProperty("draft", "Lovely. Overall, the metal could be warmer on all weapons")
        note = focus.current_note

        focus.approve()
        # Moving on does not wait for the daemon.
        assert focused(studio) == "WAY-61"
        wait_until(lambda: len(backend.decisions) == 1)
        request = backend.decisions[0]
        assert (request.tickets, request.verdict, request.drawings) == (("WAY-60",), ReviewVerdict.APPROVE, {})
        # General notes stay in the words, for the decision job to split out.
        assert request.takes == ("Lovely. Overall, the metal could be warmer on all weapons",)
        wait_until(lambda: note.all_takes() == ())

    def test_a_decided_ticket_is_skipped_and_cannot_be_decided_twice(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        focus.approve()
        focus.previousTicket()
        assert focused(studio) == "WAY-60"
        assert focus.property("isDeciding")
        focus.approve()
        assert studio.property("message") == "WAY-60 already has a decision on its way"

        # From the last ticket the next one without a decision is found from the start, past the decided one.
        studio.review.focus_ticket("WAY-63")
        focus.approve()
        assert focused(studio) == "WAY-61"
        wait_until(lambda: len(backend.decisions) == 2)

    def test_finishing_with_changes_needs_words_or_a_drawing(self, studio: Studio, backend: FakeBackend) -> None:
        focus = studio.focus
        wanted: list[bool] = []
        focus.wordsWanted.connect(lambda: wanted.append(True))
        studio.review.openTicket(0)
        focus.finishWithChanges()
        assert wanted == [True]
        assert focused(studio) == "WAY-60"
        assert backend.decisions == []

    def test_deciding_the_last_open_ticket_goes_back_to_the_grid(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        for _ in range(4):
            focus.approve()
        assert focused(studio) == ""
        wait_until(lambda: len(backend.decisions) == 4)


class TestDrawing:
    def test_a_drawing_is_kept_in_the_draft_folder_and_sent_with_changes_then_cleared(
        self, studio: Studio, backend: FakeBackend, tmp_path: Path, wait_until: WaitUntil
    ) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        focus.drawOnCurrent()
        assert focus.drawing.is_open
        draw_line(focus.drawing)
        focus.finishDrawing()
        focus.drawOnBlank()
        draw_line(focus.drawing)
        focus.finishDrawing()

        draft = tmp_path / "state" / "review" / "WAY-60" / DRAFT_DIR_NAME / "4-WAY-60"
        assert [path.name for path in focus.drawing_paths("WAY-60")] == ["drawing-1.png", "drawing-2.png"]
        assert [row.name for row in focus.property("drawingRows").rows] == ["drawing-1.png", "drawing-2.png"]
        assert QImage(str(draft / "drawing-1.png")).size().toTuple() == (1600, 1200)
        assert QImage(str(draft / "drawing-2.png")).size().toTuple() == BLANK_CANVAS_SIZE

        focus.removeDrawing("drawing-1.png")
        focus.finishWithChanges()
        wait_until(lambda: len(backend.decisions) == 1)
        assert backend.decisions[0].drawings == {"WAY-60": (draft / "drawing-2.png",)}
        assert backend.decisions[0].takes == ()
        wait_until(lambda: not draft.exists())

    def test_moving_on_with_a_drawing_open_keeps_it_and_discard_drops_it(self, studio: Studio) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        focus.drawOnCurrent()
        draw_line(focus.drawing)
        focus.nextTicket()
        assert not focus.drawing.is_open
        assert [path.name for path in focus.drawing_paths("WAY-60")] == ["drawing-1.png"]

        focus.drawOnCurrent()
        draw_line(focus.drawing)
        focus.discardDrawing()
        assert focus.drawing_paths("WAY-61") == []

    def test_only_images_are_drawn_on_directly(self, studio: Studio) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        focus.showEntry(4)
        focus.drawOnCurrent()
        assert not focus.drawing.is_open
        assert "B opens a blank canvas" in studio.property("message")

    def test_a_draft_for_an_earlier_delivery_is_dropped(self, studio: Studio, tmp_path: Path) -> None:
        stale = tmp_path / "state" / "review" / "WAY-61" / DRAFT_DIR_NAME / "3-old" / "drawing-1.png"
        make_image(stale, 10, 10)
        studio.review.openTicket(1)
        assert not stale.parent.exists()


class TestDrawingLayer:
    def test_undo_redo_and_clear_take_back_and_bring_back_strokes_and_pins(self, qt_application: object) -> None:
        drawing = DecisionDrawing()
        draw_line(drawing)
        drawing.addPin(0.5, 0.5)
        drawing.setPinNote(1, "here")
        drawing.undo()
        assert not drawing.markup.pins
        drawing.redo()
        assert [pin.note for pin in drawing.markup.pins] == ["here"]

        drawing.clear()
        assert not drawing.has_marks
        drawing.undo()
        assert len(drawing.markup.strokes) == 1
        assert drawing.property("canRedo")
        draw_line(drawing, "highlighter")
        assert not drawing.property("canRedo")

    def test_sizes_and_tools_set_the_stroke_width(self, qt_application: object) -> None:
        drawing = DecisionDrawing()
        drawing.setSize("small")
        draw_line(drawing, "pen")
        drawing.setSize("large")
        draw_line(drawing, "pen")
        draw_line(drawing, "highlighter")
        widths = [stroke.width for stroke in drawing.markup.strokes]
        assert widths[0] < widths[1] < widths[2]

    def test_markup_saved_without_a_width_reads_as_a_medium_pen(self) -> None:
        stroke = Stroke.model_validate({"tool": "pen", "color": "#ff4d4d", "points": [[0, 0], [1, 1]]})
        assert stroke.width == 0.005

    def test_the_eraser_rubs_out_marks_but_never_the_image_and_the_highlighter_is_translucent(
        self, qt_application: object
    ) -> None:
        capture = QImage(200, 100, QImage.Format.Format_RGB32)
        capture.fill(QColor("#808080"))
        pen = Stroke(tool=StrokeTool.PEN, color="#ff0000", points=((0.1, 0.3), (0.9, 0.3)), width=0.04)
        highlight = Stroke(tool=StrokeTool.HIGHLIGHTER, color="#ffff00", points=((0.1, 0.7), (0.9, 0.7)), width=0.08)
        eraser = Stroke(tool=StrokeTool.ERASER, color="#000000", points=((0.5, 0.0), (0.5, 1.0)), width=0.1)

        flattened = flatten_markup(capture, Markup(strokes=(pen, highlight, eraser)))
        assert flattened.pixelColor(40, 30).name() == "#ff0000"
        # Under the eraser's path the render shows again, through both strokes.
        assert flattened.pixelColor(100, 30).name() == "#808080"
        assert flattened.pixelColor(100, 70).name() == "#808080"
        # The highlighter tints the grey without hiding it.
        tinted = flattened.pixelColor(40, 70)
        assert 128 < tinted.red() < 255 and tinted.blue() < 128

    def test_a_blank_canvas_is_white_and_starts_with_the_pen_in_ink(self, qt_application: object) -> None:
        drawing = DecisionDrawing()
        drawing.setTool("eraser")
        drawing.open_blank()
        assert drawing.property("tool") == "pen"
        assert drawing.property("label") == "Blank canvas"
        assert drawing.property("color") == "#1b1b1b"
        blank = QImage(QUrl(drawing.property("imageUrl")).toLocalFile())
        assert blank.pixelColor(10, 10).name() == "#ffffff"


# ####################### #
#       THE QML PAGE      #
# ####################### #


@pytest.fixture
def focus_window(
    studio: Studio,
    qt_application: QGuiApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wait_until: WaitUntil,
) -> Iterator[tuple[QQuickWindow, list[QQmlError]]]:
    """The whole window in Review mode, focus mode open on the first ticket, shown on `offscreen`."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    qt_application.setOrganizationName("AtelierTests")
    qt_application.setApplicationName("AtelierTests")
    engine = QQmlApplicationEngine()
    warnings: list[QQmlError] = []
    engine.warnings.connect(warnings.extend)
    engine.rootContext().setContextProperty("studio", studio)
    engine.rootContext().setContextProperty("launchOptions", {"windowWidth": 1400, "windowHeight": 860})
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
    QTest.keyClick(window, Qt.Key.Key_Return)
    page = window.findChild(QQuickItem, "reviewFocusPage")
    assert page is not None
    wait_until(lambda: page.hasActiveFocus())
    yield window, warnings
    window.close()


def stage_point(window: QQuickWindow, x: float, y: float) -> QPoint:
    """A point on the focus page's stage, as fractions of its size."""
    stage = window.findChild(QQuickItem, "focusStage")
    assert stage is not None
    return stage.mapToScene(stage.boundingRect().topLeft()).toPoint() + QPoint(
        round(stage.width() * x), round(stage.height() * y)
    )


class TestThread:
    @staticmethod
    def rows(focus: ReviewFocus) -> list[ThreadRow]:
        return [row for row in focus.property("threadRows").rows if isinstance(row, ThreadRow)]

    def test_it_is_the_whole_conversation_oldest_first(self, studio: Studio) -> None:
        studio.review.openTicket(0)
        rows = self.rows(studio.focus)
        assert [row.author for row in rows] == ["Claude", "Alex Navarro", "Claude", "Claude"]
        assert [row.is_reviewer for row in rows] == [False, True, False, False]
        assert [row.is_delivery for row in rows] == [False, False, True, False]
        assert rows[0].text == "**Round 1**"
        assert rows[0].date == "October 28th 2026" or rows[0].date == "October 29th 2026"
        assert " at " in rows[0].full_date

    def test_its_images_link_to_the_stage_or_the_ticket(self, studio: Studio) -> None:
        studio.review.openTicket(0)
        text = self.rows(studio.focus)[2].text
        assert "[Hero](atelier-stage:hero.png)" in text
        assert "[Old round](https://tracker.example/issue/WAY-60)" in text

    def test_a_link_to_the_stage_shows_that_file(self, studio: Studio) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        focus.showEntry(1)
        focus.followLink("atelier-stage:hero.png")
        assert focus.property("currentName") == "Hero"

    def test_only_web_links_leave_the_window(self, studio: Studio, opened_urls: list[str]) -> None:  # noqa: F811
        focus = studio.focus
        studio.review.openTicket(0)
        focus.followLink("https://tracker.example/issue/WAY-1")
        focus.followLink("file:///etc/passwd")
        assert opened_urls == ["https://tracker.example/issue/WAY-1"]

    def test_a_new_comment_joins_a_ticket_already_open(
        self, studio: Studio, backend: FakeBackend, wait_until: WaitUntil
    ) -> None:
        focus = studio.focus
        studio.review.openTicket(0)
        carbine = backend.review_queue.items[0]
        added = ReviewComment(id="4-10", author="Claude", created_at=DELIVERED_AT, text="One more thing")
        fresh = carbine.model_copy(update={"comments": (*carbine.comments, added)})
        backend.review_queue = backend.review_queue.model_copy(
            update={"items": (fresh, *backend.review_queue.items[1:])}
        )
        studio.review.refresh()
        wait_until(lambda: len(self.rows(focus)) == 5)
        assert focus.property("ticket") == "WAY-60"


class TestFocusPage:
    def test_the_thread_opens_on_the_latest_delivery_under_the_note(
        self,
        studio: Studio,
        focus_window: tuple[QQuickWindow, list[QQmlError]],
        wait_until: WaitUntil,
    ) -> None:
        window, warnings = focus_window
        thread = window.findChild(QQuickItem, "focusThread")
        notes = window.findChild(QQuickItem, "focusDraftField")
        assert thread is not None
        assert notes is not None
        wait_until(lambda: thread.property("count") == 4)
        assert notes.mapToScene(notes.boundingRect().topLeft()).y() < thread.mapToScene(QPointF(0, 0)).y()
        assert [warning.toString() for warning in warnings] == []

    def test_o_and_the_button_open_the_ticket_in_the_tracker(
        self,
        opened_urls: list[str],  # noqa: F811
        focus_window: tuple[QQuickWindow, list[QQmlError]],
    ) -> None:
        window, warnings = focus_window
        QTest.keyClick(window, Qt.Key.Key_O)
        button = window.findChild(QQuickItem, "focusOpenInTracker")
        assert button is not None
        centre = button.mapToScene(button.boundingRect().center()).toPoint()
        QTest.mouseClick(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, centre)
        assert opened_urls == ["https://tracker.example/issue/WAY-60"] * 2
        assert [warning.toString() for warning in warnings] == []

    def test_an_animation_plays_on_the_stage(
        self,
        studio: Studio,
        backend: FakeBackend,
        tmp_path: Path,
        focus_window: tuple[QQuickWindow, list[QQmlError]],
        wait_until: WaitUntil,
    ) -> None:
        window, warnings = focus_window
        animation = window.findChild(QQuickItem, "focusStageAnimation")
        assert animation is not None
        assert animation.property("source").toString() == ""

        gif = tmp_path / "state" / "walk.gif"
        Image.new("P", (8, 8)).save(gif)
        walk = make_item("WAY-61", "[Art] Wall", (gif,))
        assert walk.delivery is not None
        attachment = walk.delivery.attachments[0].model_copy(update={"mime_type": "image/gif"})
        walk = walk.model_copy(update={"delivery": walk.delivery.model_copy(update={"attachments": (attachment,)})})
        queue = backend.review_queue
        backend.review_queue = queue.model_copy(update={"items": (queue.items[0], walk, *queue.items[2:])})
        studio.review.refresh()
        wait_until(lambda: not studio.review.property("isRefreshing"))

        QTest.keyClick(window, Qt.Key.Key_Right)
        assert studio.focus.property("ticket") == "WAY-61"
        assert animation.property("source").toString().endswith("/walk.gif")
        assert animation.isVisible()
        assert [warning.toString() for warning in warnings] == []

    def test_the_keyboard_draws_writes_and_finishes_with_changes(
        self,
        studio: Studio,
        backend: FakeBackend,
        focus_window: tuple[QQuickWindow, list[QQmlError]],
        wait_until: WaitUntil,
    ) -> None:
        window, warnings = focus_window
        focus = studio.focus
        assert focus.property("ticket") == "WAY-60"

        QTest.keyClick(window, Qt.Key.Key_Down)
        assert focus.property("currentName") == "Detail"
        QTest.keyClick(window, Qt.Key.Key_Up)

        # D draws on the hero, with a drag of the highlighter.
        QTest.keyClick(window, Qt.Key.Key_D)
        assert focus.drawing.is_open
        QTest.keyClick(window, Qt.Key.Key_H)
        surface = window.findChild(QQuickItem, "focusDrawingSurface")
        assert surface is not None
        wait_until(lambda: surface.property("isImageReady"))
        start, end = stage_point(window, 0.3, 0.5), stage_point(window, 0.7, 0.5)
        QTest.mousePress(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
        QTest.mouseMove(window, stage_point(window, 0.5, 0.52))
        QTest.mouseMove(window, end)
        QTest.mouseRelease(window, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, end)
        assert [stroke.tool for stroke in focus.drawing.markup.strokes] == [StrokeTool.HIGHLIGHTER]
        QTest.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
        assert focus.drawing.markup.strokes == ()
        QTest.keyClick(window, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier)
        assert len(focus.drawing.markup.strokes) == 1
        # Esc keeps it too: the most reflexive key never throws a drawing away.
        QTest.keyClick(window, Qt.Key.Key_Escape)
        assert not focus.drawing.is_open
        assert len(focus.drawing_paths("WAY-60")) == 1

        # N writes in the note; Esc gives the keys back, and C sends words and drawing and moves on.
        QTest.keyClick(window, Qt.Key.Key_N)
        for character in "Chunkier grip":
            QTest.keyClick(window, character)
        QTest.keyClick(window, Qt.Key.Key_Escape)
        assert focused(studio) == "WAY-60"
        QTest.keyClick(window, Qt.Key.Key_C)
        wait_until(lambda: len(backend.decisions) == 1)
        request = backend.decisions[0]
        assert (request.verdict, request.takes) == (ReviewVerdict.CHANGES, ("Chunkier grip",))
        assert [path.name for path in request.drawings["WAY-60"]] == ["drawing-1.png"]
        assert focused(studio) == "WAY-61"

        QTest.keyClick(window, Qt.Key.Key_Right)
        assert focused(studio) == "WAY-62"
        QTest.keyClick(window, Qt.Key.Key_Escape)
        wait_until(lambda: focused(studio) == "")
        assert [warning.toString() for warning in warnings] == []
