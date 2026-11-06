"""Review focus mode: one ticket at a time, large, decided from the keyboard as fast as the reviewer can think.

`ReviewFocus` follows the review board's focused ticket (`review.py`). For that ticket it shows a **stage**: the latest
delivery's art (its hero first), the 3D viewport of the source the ticket links to, its reference images, its sounds,
and any other files by name, with a filmstrip to switch between them, under captions rather than file names. Beside
the stage is the ticket's **note**, a `DecisionNote` of its own per ticket, so words written for one ticket stay with
it while the reviewer moves back and forth; its **drawings**, PNGs drawn on the stage's images or on a blank canvas
(`markup.py`'s `DecisionDrawing`); and its **thread**, every comment on the ticket, oldest first (`thread.py`).

Moving on never waits. Approving or finishing with changes submits the decision through the board (which sends it to
the daemon in the background and badges the ticket) and opens the next ticket without a decision at once. The images
of the ticket shown and of the next two are kept decoded ahead: QML holds each one in a fixed slot of invisible images
(`prefetchSlots`), and an image that stays wanted keeps its slot, so the stage finds it already decoded in Qt's image
cache.

A ticket's drawings and voice notes' recordings (`voice.py`), everything made for its next decision, live in its
draft folder,
`<state>/review/<ticket>/draft/<delivery comment>/`. The decision job copies the drawings into its own folder when it
is queued, so the draft folder is deleted then; a draft made for an earlier delivery is deleted when the ticket opens
on a newer one.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict
from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices

from atelier.gui.list_model import RowListModel
from atelier.gui.markup import DecisionDrawing
from atelier.gui.review import DecisionNote, ReviewBoard, delivery_images, unsettled_takes_message
from atelier.gui.thread import STAGE_LINK_SCHEME, date_labels, readable_markdown
from atelierd.config import ReviewVerdict
from atelierd.constants import REVIEW_DIR_NAME
from atelierd.review import DecisionJob, DecisionRequest, ReviewItem, ReviewLink

logger = logging.getLogger(__name__)

# `<state>/review/<ticket>/draft/` holds what the window makes for a ticket's next decision: its drawings and the
# recordings of its voice notes.
DRAFT_DIR_NAME = "draft"
# How many tickets after the one shown have their images decoded ahead.
PREFETCH_TICKETS = 2
# The invisible images that hold decoded images ahead: enough for three deliveries of eight images each. Images past
# that are decoded when shown.
PREFETCH_SLOT_COUNT = 24

# A delivery's file is played as a sound when its type or its name says it is one.
_AUDIO_SUFFIXES = frozenset({".mp3", ".wav", ".ogg", ".oga", ".flac", ".m4a", ".aac", ".opus"})
# Comment text is untrusted: its links open in the browser only as web pages.
_WEB_SCHEMES = frozenset({"http", "https"})
_DRAWING_PREFIX = "drawing-"
_NO_DELIVERY_ROUND = "no-delivery"
_MODEL_ENTRY_NAME = "3D view"


class StageKind(StrEnum):
    """What one entry of the stage shows."""

    IMAGE = "image"
    MODEL = "model"
    AUDIO = "audio"
    FILE = "file"


class StageRow(BaseModel):
    """One entry of the stage, as the filmstrip shows it."""

    model_config = ConfigDict(frozen=True)

    kind: str
    name: str
    file_name: str
    url: str
    is_current: bool


class ThreadRow(BaseModel):
    """One comment of the ticket's thread."""

    model_config = ConfigDict(frozen=True)

    author: str
    date: str
    full_date: str
    text: str
    is_reviewer: bool
    is_delivery: bool


class DrawingRow(BaseModel):
    """One drawing the ticket's next decision carries."""

    model_config = ConfigDict(frozen=True)

    name: str
    url: str


class _Entry(BaseModel):
    """One thing the stage can show for a ticket."""

    model_config = ConfigDict(frozen=True)

    kind: StageKind
    # What the reviewer sees it called.
    name: str
    # Its file name, which the tooltip gives and comments' image links go by.
    file_name: str = ""
    path: Path | None = None


def stage_entries(item: ReviewItem) -> list[_Entry]:
    """What the stage shows for a ticket: its art, 3D model, reference images, sounds and files.

    The delivery lists its files in display order (`atelierd.review.images`), so the first is the tile's hero too.
    """
    delivery = item.delivery
    art: list[_Entry] = []
    reference: list[_Entry] = []
    sounds: list[_Entry] = []
    files: list[_Entry] = []
    images = delivery_images(delivery) if delivery else []
    for attachment in delivery.attachments if delivery else ():
        name = attachment.caption or attachment.name
        if attachment in images:
            entry = _Entry(kind=StageKind.IMAGE, name=name, file_name=attachment.name, path=attachment.path)
            (reference if attachment.is_reference else art).append(entry)
        elif attachment.mime_type.startswith("audio/") or attachment.path.suffix.lower() in _AUDIO_SUFFIXES:
            sounds.append(_Entry(kind=StageKind.AUDIO, name=name, file_name=attachment.name, path=attachment.path))
        else:
            files.append(_Entry(kind=StageKind.FILE, name=name, file_name=attachment.name, path=attachment.path))
    model = [_Entry(kind=StageKind.MODEL, name=_MODEL_ENTRY_NAME)] if item.link is not None else []
    return art + model + reference + sounds + files


class ReviewFocus(QObject):
    """Focus mode on the board's focused ticket: its stage, note and drawings, and deciding it."""

    # The ticket shown, or its badge, changed.
    changed = Signal()
    # The stage's entries or the one shown changed.
    stageChanged = Signal()
    # The ticket's drawings changed.
    drawingsChanged = Signal()
    # Another ticket is shown: the thread goes to this comment, its latest delivery.
    threadShown = Signal(int)
    # Finishing with changes needs words: the page gives the note's field the keyboard.
    wordsWanted = Signal()

    def __init__(
        self,
        board: ReviewBoard,
        show_message: Callable[[str], None],
        show_model: Callable[[ReviewLink], None],
    ) -> None:
        """Create focus mode over a board.

        Args:
            board: the review grid, whose focused ticket this shows and through which it decides.
            show_message: shows a status message.
            show_model: puts a linked source's export on the window's 3D viewport.
        """
        super().__init__()
        self._board = board
        self._show_message = show_message
        self._show_model = show_model
        self.drawing = DecisionDrawing()

        self._ticket = ""
        # What the stage was built from, so a badge change keeps the stage as it is and a new delivery rebuilds it.
        self._basis = ""
        self._entries: list[_Entry] = []
        self._stage_index = 0
        self._notes: dict[str, DecisionNote] = {}
        self._stage_rows = RowListModel(StageRow)
        self._drawing_rows = RowListModel(DrawingRow)
        self._thread_rows = RowListModel(ThreadRow)
        self._prefetch_slots = [""] * PREFETCH_SLOT_COUNT

        board.focusChanged.connect(self._follow_board)

    # ####################### #
    #       PROPERTIES        #
    # ####################### #

    def _get_is_open(self) -> bool:
        return bool(self._ticket)

    def _get_ticket(self) -> str:
        return self._ticket

    def _get_tile(self) -> dict[str, Any]:
        tile = self._board.tile(self._ticket)
        return tile.model_dump() if tile else {}

    def _get_position_label(self) -> str:
        tickets = self._board.tab_tickets
        if self._ticket not in tickets:
            return ""
        return f"{tickets.index(self._ticket) + 1} of {len(tickets)}"

    def _get_is_deciding(self) -> bool:
        return bool(self._ticket) and self._board.is_busy(self._ticket)

    def _get_stage_rows(self) -> QObject:
        return self._stage_rows

    def _get_stage_index(self) -> int:
        return self._stage_index if self._entries else -1

    def _get_current_kind(self) -> str:
        entry = self._current_entry()
        return entry.kind.value if entry else ""

    def _get_current_name(self) -> str:
        entry = self._current_entry()
        return entry.name if entry else ""

    def _get_current_url(self) -> str:
        entry = self._current_entry()
        return _file_url(entry.path) if entry else ""

    def _get_empty_note(self) -> str:
        item = self._board.item(self._ticket)
        if item is None or item.delivery is None:
            return "No delivery yet"
        if item.delivery.error:
            return f"Some files could not be downloaded: {item.delivery.error}"
        return "The delivery has no files"

    def _get_note(self) -> QObject:
        return self.current_note

    def _get_drawing_rows(self) -> QObject:
        return self._drawing_rows

    def _get_thread_rows(self) -> QObject:
        return self._thread_rows

    def _get_drawing(self) -> QObject:
        return self.drawing

    def _get_prefetch_slots(self) -> list[str]:
        return list(self._prefetch_slots)

    def _get_prefetch_slot_count(self) -> int:
        return PREFETCH_SLOT_COUNT

    isOpen = Property(bool, _get_is_open, notify=changed)
    ticket = Property(str, _get_ticket, notify=changed)
    # The ticket's grid tile: summary, tags, link, and its decision job's badge.
    tile = Property(dict, _get_tile, notify=changed)
    positionLabel = Property(str, _get_position_label, notify=changed)
    isDeciding = Property(bool, _get_is_deciding, notify=changed)
    stageRows = Property(QObject, _get_stage_rows, constant=True)
    stageIndex = Property(int, _get_stage_index, notify=stageChanged)
    currentKind = Property(str, _get_current_kind, notify=stageChanged)
    currentName = Property(str, _get_current_name, notify=stageChanged)
    currentUrl = Property(str, _get_current_url, notify=stageChanged)
    # What the stage says when it has nothing to show.
    emptyNote = Property(str, _get_empty_note, notify=stageChanged)
    note = Property(QObject, _get_note, notify=changed)
    drawingRows = Property(QObject, _get_drawing_rows, constant=True)
    threadRows = Property(QObject, _get_thread_rows, constant=True)
    decisionDrawing = Property(QObject, _get_drawing, constant=True)
    # Image URLs to keep decoded ahead, one per slot; a URL that stays wanted keeps its slot.
    prefetchSlots = Property(list, _get_prefetch_slots, notify=stageChanged)
    prefetchSlotCount = Property(int, _get_prefetch_slot_count, constant=True)

    @property
    def shown_ticket(self) -> str:
        """The ticket focus mode shows; empty while the grid shows."""
        return self._ticket

    @property
    def current_note(self) -> DecisionNote:
        """The shown ticket's note: where its typed words are, and where its spoken takes go."""
        return self.note_for(self._ticket)

    def draft_folder(self, ticket: str) -> Path:
        """The folder holding what is made for a ticket's next decision, for its current delivery."""
        item = self._board.item(ticket)
        delivery_round = item.delivery.comment_id if item and item.delivery else _NO_DELIVERY_ROUND
        return self._board.state_dir / REVIEW_DIR_NAME / ticket / DRAFT_DIR_NAME / delivery_round

    def drawing_paths(self, ticket: str) -> list[Path]:
        """The drawings a ticket's next decision carries, in the order they were drawn."""
        folder = self.draft_folder(ticket)
        if not folder.is_dir():
            return []
        drawings = [path for path in folder.glob(f"{_DRAWING_PREFIX}*.png") if _drawing_number(path) > 0]
        return sorted(drawings, key=_drawing_number)

    # ####################### #
    #   MOVING BETWEEN THEM   #
    # ####################### #

    @Slot()
    def nextTicket(self) -> None:
        """Show the next ticket of the tab, keeping a drawing in progress."""
        self._step_ticket(1)

    @Slot()
    def previousTicket(self) -> None:
        """Show the previous ticket of the tab, keeping a drawing in progress."""
        self._step_ticket(-1)

    @Slot()
    def close(self) -> None:
        """Go back to the grid, keeping a drawing in progress."""
        if self._keep_drawing():
            self._board.closeFocus()

    @Slot(int)
    def showEntry(self, index: int) -> None:
        """Show one entry of the stage."""
        if not 0 <= index < len(self._entries):
            logger.debug(f"[Focus] No stage entry {index} of {len(self._entries)}")
            return
        if index == self._stage_index or not self._keep_drawing():
            return
        self._stage_index = index
        self._on_entry_shown()

    @Slot(int)
    def stepEntry(self, step: int) -> None:
        """Show the entry `step` places along the filmstrip, wrapping around."""
        if not self._entries:
            return
        self.showEntry((self._stage_index + step) % len(self._entries))

    @Slot()
    def openInTracker(self) -> None:
        """Open the ticket shown in the tracker, in the default browser."""
        self._board.openInTracker(self._ticket)

    @Slot(str)
    def followLink(self, link: str) -> None:
        """Follow a link in the thread: to the stage for a file of the delivery, or to the browser for a web page."""
        url = QUrl(link)
        if url.scheme() == STAGE_LINK_SCHEME:
            file_name = unquote(url.path())
            index = next(
                (position for position, entry in enumerate(self._entries) if entry.file_name == file_name),
                -1,
            )
            self.showEntry(index)
        elif url.scheme() in _WEB_SCHEMES:
            QDesktopServices.openUrl(url)
        else:
            logger.debug(f"[Focus] Left a {url.scheme()!r} link in a comment unfollowed")

    def _step_ticket(self, step: int) -> None:
        tickets = self._board.tab_tickets
        if self._ticket not in tickets:
            logger.debug(f"[Focus] {self._ticket} is not in the tab to step from")
            return
        target = tickets.index(self._ticket) + step
        if not 0 <= target < len(tickets):
            self._show_message("That is the last ticket" if step > 0 else "That is the first ticket")
            return
        if self._keep_drawing():
            self._board.focus_ticket(tickets[target])

    # ####################### #
    #        DECIDING         #
    # ####################### #

    @Slot()
    def approve(self) -> None:
        """Approve the ticket shown, with its note and drawings if it has any, and open the next one."""
        self._decide(ReviewVerdict.APPROVE)

    @Slot()
    def finishWithChanges(self) -> None:
        """Request changes on the ticket shown with its note and drawings, and open the next one."""
        self._decide(ReviewVerdict.CHANGES)

    def _decide(self, verdict: ReviewVerdict) -> None:
        ticket = self._ticket
        if not ticket:
            logger.debug("[Focus] No ticket is shown to decide")
            return
        if not self._keep_drawing():
            return
        if self._board.is_busy(ticket):
            self._show_message(f"{ticket} already has a decision on its way")
            return
        note = self.note_for(ticket)
        unsettled = note.unsettled_takes()
        if unsettled:
            self._show_message(f"{ticket}: {unsettled_takes_message(unsettled)}")
            return
        takes = note.all_takes()
        drawings = tuple(self.drawing_paths(ticket))
        if verdict is ReviewVerdict.CHANGES and not (takes or drawings):
            self._show_message("Say or draw what should change, then finish with changes")
            self.wordsWanted.emit()
            return

        request = DecisionRequest(
            tickets=(ticket,),
            verdict=verdict,
            takes=takes,
            drawings={ticket: drawings} if drawings else {},
        )
        verdict_words = "approved" if verdict is ReviewVerdict.APPROVE else "changes requested"
        self._board.submit(request, lambda jobs: self._on_queued(ticket, verdict_words, jobs))
        self._move_on(ticket)

    def _on_queued(self, ticket: str, verdict_words: str, jobs: list[DecisionJob]) -> None:
        """The daemon has the decision and its own copies of the drawings: start the ticket's next decision afresh."""
        self.note_for(ticket).clear()
        shutil.rmtree(self.draft_folder(ticket), ignore_errors=True)
        if ticket == self._ticket:
            self._refresh_drawings()
        self._show_message(f"{ticket}: {verdict_words}, decision {jobs[0].id} queued" if jobs else f"{ticket} queued")

    def _move_on(self, decided: str) -> None:
        """Open the next ticket with no decision on its way, after the decided one and around; the grid if none."""
        tickets = self._board.tab_tickets
        start = tickets.index(decided) + 1 if decided in tickets else 0
        following = tickets[start:] + tickets[:start]
        upcoming = next((ticket for ticket in following if not self._board.is_busy(ticket)), "")
        if not upcoming:
            self._show_message("Every ticket in this tab has a decision on its way")
            self._board.closeFocus()
            return
        self._board.focus_ticket(upcoming)

    # ####################### #
    #         DRAWING         #
    # ####################### #

    @Slot()
    def drawOnCurrent(self) -> None:
        """Draw on the image shown. The 3D view is drawn on through `drawOnFrame`, once QML has frozen a frame."""
        entry = self._current_entry()
        if entry is None or entry.kind is not StageKind.IMAGE or entry.path is None:
            self._show_message("Only an image can be drawn on here; B opens a blank canvas")
            return
        if not self.drawing.open_image(entry.path, entry.name):
            self._show_message(f"{entry.name} is not on disk to draw on")

    @Slot(str)
    def drawOnFrame(self, frame_path: str) -> None:
        """Draw on a frame frozen from the 3D view."""
        if not self.drawing.open_image(Path(frame_path), f"{self._get_current_name()} frame"):
            self._show_message("The 3D view could not be frozen to draw on")

    @Slot()
    def drawOnBlank(self) -> None:
        """Sketch on a blank canvas."""
        if self._keep_drawing():
            self.drawing.open_blank()

    @Slot()
    def finishDrawing(self) -> None:
        """Keep the drawing with the ticket's next decision and put it away."""
        if not self.drawing.is_open:
            logger.debug("[Focus] No drawing is open to keep")
            return
        self._keep_drawing()

    @Slot()
    def discardDrawing(self) -> None:
        """Put the drawing away without keeping it."""
        self.drawing.close()

    @Slot(str)
    def removeDrawing(self, name: str) -> None:
        """Take a drawing back out of the ticket's next decision."""
        path = next((path for path in self.drawing_paths(self._ticket) if path.name == name), None)
        if path is None:
            logger.debug(f"[Focus] {self._ticket} has no drawing {name} to remove")
            return
        path.unlink(missing_ok=True)
        self._refresh_drawings()

    def _keep_drawing(self) -> bool:
        """Save the open drawing, if anything is drawn, into the shown ticket's draft folder, and put it away.

        Returns False when it could not be saved: it stays open, and the caller stays where it is, so nothing drawn is
        lost.
        """
        if not self.drawing.is_open:
            return True
        if self.drawing.has_marks and self._ticket:
            existing = self.drawing_paths(self._ticket)
            number = max((_drawing_number(path) for path in existing), default=0) + 1
            drawing_path = self.draft_folder(self._ticket) / f"{_DRAWING_PREFIX}{number}.png"
            if not self.drawing.save(drawing_path):
                self._show_message("The drawing could not be saved; it is still open")
                return False
            self._refresh_drawings()
        self.drawing.close()
        return True

    # ####################### #
    #   FOLLOWING THE BOARD   #
    # ####################### #

    @Slot()
    def _follow_board(self) -> None:
        """Show the board's focused ticket, rebuilding the stage only for another ticket or a new delivery."""
        ticket = self._board.focused_ticket
        item = self._board.item(ticket) if ticket else None
        if item is None:
            if self._ticket:
                self._show_ticket("", None)
            return
        if ticket == self._ticket and _basis(item) == self._basis:
            self.changed.emit()
            self._refresh_thread(item)
            return
        self._show_ticket(ticket, item)

    def _show_ticket(self, ticket: str, item: ReviewItem | None) -> None:
        if ticket != self._ticket and self.drawing.is_open:
            # Moving on through the focus page keeps the drawing first; reaching here with one open means the ticket
            # left the queue under it.
            logger.info(f"[Focus] Put away a drawing on {self._ticket}, which left focus mode")
            self.drawing.close()
        self._ticket = ticket
        self._basis = _basis(item) if item else ""
        self._entries = stage_entries(item) if item else []
        self._stage_index = 0
        if item is not None:
            self._drop_stale_drafts(ticket)
        self._refresh_drawings()
        self.changed.emit()
        self._on_entry_shown()
        self._refresh_thread(item)
        rows = self._thread_rows.rows
        delivery_row = next((row for row in reversed(rows) if isinstance(row, ThreadRow) and row.is_delivery), None)
        self.threadShown.emit(rows.index(delivery_row) if delivery_row else len(rows) - 1)

    def _on_entry_shown(self) -> None:
        entry = self._current_entry()
        item = self._board.item(self._ticket)
        if entry is not None and entry.kind is StageKind.MODEL and item is not None and item.link is not None:
            self._show_model(item.link)
        self._stage_rows.set_rows(
            [
                StageRow(
                    kind=entry.kind.value,
                    name=entry.name,
                    file_name=entry.file_name,
                    url=_file_url(entry.path),
                    is_current=index == self._stage_index,
                )
                for index, entry in enumerate(self._entries)
            ]
        )
        self._assign_prefetch()
        self.stageChanged.emit()

    def _assign_prefetch(self) -> None:
        """Keep the images of the shown ticket and the next ones decoded, each URL in the slot it already has."""
        wanted: list[str] = []
        tickets = self._board.tab_tickets
        if self._ticket in tickets:
            position = tickets.index(self._ticket)
            for ticket in tickets[position : position + 1 + PREFETCH_TICKETS]:
                item = self._board.item(ticket)
                if item is not None and item.delivery is not None:
                    wanted += [_file_url(image.path) for image in delivery_images(item.delivery)]
        if len(wanted) > PREFETCH_SLOT_COUNT:
            logger.debug(f"[Focus] {len(wanted) - PREFETCH_SLOT_COUNT} images are past the slots and load when shown")
            wanted = wanted[:PREFETCH_SLOT_COUNT]

        wanted = list(dict.fromkeys(wanted))
        kept = [url if url in wanted else "" for url in self._prefetch_slots]
        incoming = iter(url for url in wanted if url not in kept)
        self._prefetch_slots = [url or next(incoming, "") for url in kept]

    def _refresh_thread(self, item: ReviewItem | None) -> None:
        """Bring the thread in line with the ticket's comments; unchanged, it stays as it is, scroll included."""
        stage_names = frozenset(entry.file_name for entry in self._entries if entry.file_name)
        rows = _thread_rows(item, stage_names) if item else []
        if rows != self._thread_rows.rows:
            self._thread_rows.set_rows(rows)

    def _refresh_drawings(self) -> None:
        rows = [DrawingRow(name=path.name, url=_file_url(path)) for path in self.drawing_paths(self._ticket)]
        self._drawing_rows.set_rows(rows)
        self.drawingsChanged.emit()

    def _drop_stale_drafts(self, ticket: str) -> None:
        """Delete drafts made for a ticket's earlier deliveries: their words and drawings answered another round."""
        current = self.draft_folder(ticket)
        for folder in current.parent.glob("*"):
            if folder != current and folder.is_dir():
                logger.info(f"[Focus] Deleting {ticket}'s draft for an earlier delivery: {folder}")
                shutil.rmtree(folder, ignore_errors=True)

    def _current_entry(self) -> _Entry | None:
        if not 0 <= self._stage_index < len(self._entries):
            return None
        return self._entries[self._stage_index]

    def note_for(self, ticket: str) -> DecisionNote:
        """A ticket's own note, kept while the reviewer moves back and forth until its decision is queued."""
        if ticket not in self._notes:
            self._notes[ticket] = DecisionNote()
        return self._notes[ticket]


def _thread_rows(item: ReviewItem, stage_names: frozenset[str]) -> list[ThreadRow]:
    delivery_comment = item.delivery.comment_id if item.delivery else ""
    rows: list[ThreadRow] = []
    for comment in item.comments:
        date, full_date = date_labels(comment.created_at)
        rows.append(
            ThreadRow(
                author=comment.author,
                date=date,
                full_date=full_date,
                text=readable_markdown(comment.text, comment.captions, stage_names, item.url),
                is_reviewer=comment.is_reviewer,
                is_delivery=comment.id == delivery_comment,
            )
        )
    return rows


def _basis(item: ReviewItem) -> str:
    """What a ticket's stage is built from: its delivery, in the order it shows, and the source it links to."""
    delivery = item.delivery.comment_id if item.delivery else ""
    files = ",".join(attachment.name for attachment in item.delivery.attachments) if item.delivery else ""
    source = item.link.source if item.link else ""
    return f"{item.ticket}|{delivery}|{files}|{source}"


def _drawing_number(path: Path) -> int:
    """A drawing's number from its name (`drawing-3.png`); 0 for a file not named as a drawing."""
    number = path.stem.removeprefix(_DRAWING_PREFIX)
    return int(number) if number.isdigit() else 0


def _file_url(path: Path | None) -> str:
    if path is None:
        return ""
    return QUrl.fromLocalFile(str(path)).toString()
