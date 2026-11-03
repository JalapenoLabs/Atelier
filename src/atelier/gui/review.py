"""The Review mode's view-model: the review queue as a grid of tiles, the selection, and bulk decisions.

`ReviewBoard` follows the daemon's review queue (`docs/review.md`): it reads the queue when a project opens and again
on every queue event, and badges each tile with its ticket's decision job as `DecisionJobEvent`s arrive. A ticket
leaves the grid when the daemon's `decided` event says its decision posted. Tiles are kept in place across those
updates (removed, inserted or replaced, never reset), so the grid keeps its scroll and the reviewer their place.

The selection is a set of tickets in the current tab, with a keyboard cursor and the anchor a Shift range runs from.
A decision covers the selection, or the tile under the cursor when nothing is selected; several tickets share one
note, which makes it a bulk decision. The note is a `DecisionNote`: the reviewer's takes, verbatim and in order, plus
what they are typing. Voice notes add their takes to the same note.

Each tile shows its ticket's hero image: the first image of the latest delivery or, with none, the newest standard
`hero` render of the source the ticket links to. Heroes are found and thumbnailed off the UI thread
(`thumbnails.py`), once per delivery, and fill their tiles as they arrive.

Opening a tile (Enter, a double click) asks for focus mode on its ticket through `focusRequested`; the window shows
the focused ticket on its own page (`focus.py`) until the reviewer goes back to the grid. Focus mode walks the current
tab's tickets in the grid's order and submits its decisions through the board, so the grid badges them the same way.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from PySide6.QtCore import Property, QObject, Qt, QUrl, Signal, Slot

from atelier.gui.backend import StudioBackend
from atelier.gui.chat import RunWork
from atelier.gui.list_model import RowListModel
from atelier.gui.tasks import TaskRunner
from atelier.gui.thumbnails import ThumbnailCache
from atelierd.config import ReviewVerdict
from atelierd.constants import REVIEW_DIR_NAME
from atelierd.review import (
    DecisionJob,
    DecisionJobEvent,
    DecisionJobState,
    DecisionRequest,
    ReviewAttachment,
    ReviewDelivery,
    ReviewEvent,
    ReviewEventKind,
    ReviewItem,
    ReviewQueue,
)
from atelierd.revisions import RenderState, SourceHistory

logger = logging.getLogger(__name__)

# Where the window keeps review thumbnails, under the open project's state folder.
THUMBNAIL_FOLDER = Path("cache") / "thumbnails"

# The standard render a linked source's tile shows.
_HERO_VIEW = "hero"

# A delivery's attachment is shown as an image when its type or its name says it is one.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"})

JOB_STATE_LABELS = {
    DecisionJobState.QUEUED: "Queued",
    DecisionJobState.WRITING: "Writing",
    DecisionJobState.POSTING: "Posting",
    DecisionJobState.POSTED: "Posted",
    DecisionJobState.FAILED: "Failed",
}
if set(JOB_STATE_LABELS) != set(DecisionJobState):
    raise RuntimeError("Every decision job state needs a label")

VERDICT_LABELS = {
    ReviewVerdict.APPROVE: "Approve",
    ReviewVerdict.CHANGES: "Changes",
}
if set(VERDICT_LABELS) != set(ReviewVerdict):
    raise RuntimeError("Every review verdict needs a label")

# `<state>/review/bulk-note/` holds what the window makes for the grid's bulk note, which belongs to no one ticket. No
# ticket is named that, and the daemon only ever prunes a ticket folder's `delivery/`.
BULK_NOTE_DIR_NAME = "bulk-note"

# A ticket with a job in one of these states already has a decision on its way; the daemon refuses another.
_BUSY_JOB_STATES = frozenset({DecisionJobState.QUEUED, DecisionJobState.WRITING, DecisionJobState.POSTING})


# ========================================================================================
# Rows of the lists QML shows
# ========================================================================================


class ReviewTabRow(BaseModel):
    """One configured queue, as a tab over the grid."""

    model_config = ConfigDict(frozen=True)

    name: str
    count: int


class ReviewTile(BaseModel):
    """One ticket in the grid, as its delegate reads it."""

    model_config = ConfigDict(frozen=True)

    ticket: str
    summary: str
    tags: list[str]
    url: str
    thumbnail_url: str
    hero_url: str
    hero_note: str
    job_id: int
    job_state: str
    job_label: str
    job_error: str
    is_selected: bool


class _Hero(BaseModel):
    """A ticket's hero image and its thumbnail, or why the tile has none."""

    model_config = ConfigDict(frozen=True)

    # What the hero was found from (the delivery, or the linked source), so a new delivery looks again.
    basis: str
    image_path: Path | None = None
    thumbnail_path: Path | None = None
    note: str = ""


# ========================================================================================
# The decision note
# ========================================================================================


class TakeState(StrEnum):
    """Where one take of a note is: words, or a voice note on its way to words."""

    WORDS = "words"
    RECORDING = "recording"
    TRANSCRIBING = "transcribing"
    # Transcribing failed; the recording is kept so the take can be transcribed again.
    FAILED = "failed"


class Take(BaseModel):
    """One take of the reviewer's words. A spoken take keeps its recording, so a retake can delete it."""

    model_config = ConfigDict(frozen=True)

    # Stays the take's own while takes before it come and go, so a late transcription finds it.
    take_id: int
    text: str = ""
    state: TakeState = TakeState.WORDS
    error: str = ""
    recording: Path | None = None


class DecisionNote(QObject):
    """The reviewer's words for the next decision: takes, verbatim and in order, and the text being typed.

    A typed note is one take, the last. Voice notes (`voice.py`) put a take in place the moment recording starts, so
    takes keep the order they were spoken in however long each takes to transcribe; it holds words once transcribed,
    or the error and its recording when transcribing failed. A **checkpoint** locks the takes so far: a retake
    (`remove_last_take`) only ever removes a take after it. Focus mode lets the reviewer correct a take in place
    (`setTake`) before moving on, locked or not.
    """

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._takes: list[Take] = []
        self._draft = ""
        self._locked_count = 0
        self._next_take_id = 1

    def _get_takes(self) -> list[dict[str, Any]]:
        return [
            {"text": take.text, "state": take.state.value, "error": take.error, "isLocked": index < self._locked_count}
            for index, take in enumerate(self._takes)
        ]

    def _get_draft(self) -> str:
        return self._draft

    def _set_draft(self, draft: str) -> None:
        if draft == self._draft:
            return
        self._draft = draft
        self.changed.emit()

    def _get_is_empty(self) -> bool:
        return not self.all_takes()

    def _get_locked_count(self) -> int:
        return self._locked_count

    # Each take as QML shows it: `text`, `state` (a `TakeState`), `error` and `isLocked`.
    takes = Property(list, _get_takes, notify=changed)
    draft = Property(str, _get_draft, _set_draft, notify=changed)
    isEmpty = Property(bool, _get_is_empty, notify=changed)
    lockedCount = Property(int, _get_locked_count, notify=changed)

    @Slot(str)
    def addTake(self, text: str) -> None:
        """Add one take of the reviewer's words, exactly as given."""
        if not text.strip():
            logger.debug("[DecisionNote] A take with no words was left out")
            return
        self._append(Take(take_id=self._new_take_id(), text=text))

    @Slot(int, str)
    def setTake(self, index: int, text: str) -> None:
        """Put the reviewer's corrected words in place of one take; with no words left, the take goes."""
        if not 0 <= index < len(self._takes):
            logger.debug(f"[DecisionNote] No take {index} of {len(self._takes)} to set")
            return
        take = self._takes[index]
        if take.state is not TakeState.WORDS:
            logger.debug(f"[DecisionNote] Take {index} is {take.state.value}, not words to correct")
            return
        if text == take.text:
            return
        if text.strip():
            self._takes[index] = take.model_copy(update={"text": text})
        else:
            del self._takes[index]
            if index < self._locked_count:
                self._locked_count -= 1
        self.changed.emit()

    @Slot()
    def removeLastTake(self) -> None:
        """Drop the newest take after the checkpoint."""
        self.remove_last_take()

    @Slot()
    def checkpoint(self) -> None:
        """Lock the takes so far: a retake can no longer remove them."""
        self._locked_count = len(self._takes)
        self.changed.emit()

    @Slot()
    def clear(self) -> None:
        """Start the next decision with no words."""
        self._takes.clear()
        self._draft = ""
        self._locked_count = 0
        self.changed.emit()

    @property
    def locked_count(self) -> int:
        """How many takes, from the first, the checkpoint locks."""
        return self._locked_count

    def all_takes(self) -> tuple[str, ...]:
        """Every take that is words, the typed text last, as a decision carries them."""
        words = tuple(take.text for take in self._takes if take.state is TakeState.WORDS)
        if self._draft.strip():
            return (*words, self._draft)
        return words

    def unsettled_takes(self) -> list[Take]:
        """The spoken takes not yet words: still recording or transcribing, or failed to transcribe."""
        return [take for take in self._takes if take.state is not TakeState.WORDS]

    def remove_last_take(self) -> Take | None:
        """Drop the newest take after the checkpoint and return it, or None when there is none to drop."""
        if len(self._takes) <= self._locked_count:
            logger.debug(f"[DecisionNote] No take after the checkpoint to remove ({self._locked_count} locked)")
            return None
        removed = self._takes.pop()
        self.changed.emit()
        return removed

    def start_spoken_take(self, recording: Path) -> int:
        """Put a take in place for a recording that has just started; returns its id."""
        take_id = self._new_take_id()
        self._append(Take(take_id=take_id, state=TakeState.RECORDING, recording=recording))
        return take_id

    def update_spoken_take(self, take_id: int, state: TakeState, text: str = "", error: str = "") -> bool:
        """Move a spoken take along: transcribing, its words, or the error it failed with.

        Returns False when the take is no longer in the note (retaken, removed, or the note sent with a decision).
        """
        index = self._take_index(take_id)
        if index < 0:
            logger.debug(f"[DecisionNote] Take {take_id} has left the note")
            return False
        self._takes[index] = self._takes[index].model_copy(update={"state": state, "text": text, "error": error})
        self.changed.emit()
        return True

    def remove_take(self, take_id: int) -> Take | None:
        """Drop one take, locked or not, and return it; None when it is not in the note."""
        index = self._take_index(take_id)
        if index < 0:
            logger.debug(f"[DecisionNote] No take {take_id} to remove")
            return None
        removed = self._takes.pop(index)
        if index < self._locked_count:
            self._locked_count -= 1
        self.changed.emit()
        return removed

    def take_at(self, index: int) -> Take | None:
        """The take at a place in the note, as QML counts them."""
        if not 0 <= index < len(self._takes):
            return None
        return self._takes[index]

    def _append(self, take: Take) -> None:
        self._takes.append(take)
        self.changed.emit()

    def _new_take_id(self) -> int:
        take_id = self._next_take_id
        self._next_take_id += 1
        return take_id

    def _take_index(self, take_id: int) -> int:
        return next((index for index, take in enumerate(self._takes) if take.take_id == take_id), -1)


def unsettled_takes_message(unsettled: list[Take]) -> str:
    """Why a decision waits for its spoken takes, and what to do about it."""
    if any(take.state is TakeState.FAILED for take in unsettled):
        return "A take failed to transcribe: retry it, or retake it, before deciding"
    return "A take is still being recorded or transcribed; decide once it is words"


# ========================================================================================
# The grid
# ========================================================================================


class ReviewBoard(QObject):
    """The review queue of the open project, as the Review mode's grid."""

    changed = Signal()
    selectionChanged = Signal()
    cursorChanged = Signal()
    focusChanged = Signal()
    # Focus mode opens on this ticket.
    focusRequested = Signal(str)

    def __init__(
        self,
        backend: StudioBackend,
        tasks: TaskRunner,
        run_work: RunWork,
        show_message: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._backend = backend
        self._tasks = tasks
        self._run_work = run_work
        self._show_message = show_message

        self._queue = ReviewQueue(is_configured=False)
        self._items: dict[str, ReviewItem] = {}
        # Each ticket's newest decision job.
        self._jobs: dict[str, DecisionJob] = {}
        self._heroes: dict[str, _Hero] = {}
        # Tickets whose hero is being found, by the basis it is found from.
        self._pending_heroes: dict[str, str] = {}
        self._thumbnails: ThumbnailCache | None = None
        # Bumped when a project opens, so heroes found for the last one are dropped.
        self._generation = 0

        self._current_tab = ""
        self._selected: set[str] = set()
        self._cursor = ""
        self._anchor = ""
        # The selection a drag box started from; None when no box is being dragged.
        self._box_base: set[str] | None = None
        self._focused = ""
        # Tickets whose decision has been submitted but not yet answered with its job.
        self._submitting: set[str] = set()
        self._is_refreshing = False

        self._tab_rows = RowListModel(ReviewTabRow)
        self._tile_rows = RowListModel(ReviewTile)
        self.note = DecisionNote()

    # ####################### #
    #       PROPERTIES        #
    # ####################### #

    def _get_is_configured(self) -> bool:
        return self._queue.is_configured

    def _get_error(self) -> str:
        return self._queue.error

    def _get_is_refreshing(self) -> bool:
        return self._is_refreshing

    def _get_has_refreshed(self) -> bool:
        return self._queue.refreshed_at is not None

    def _get_ticket_count(self) -> int:
        return len(self._items)

    def _get_tab_rows(self) -> QObject:
        return self._tab_rows

    def _get_current_tab(self) -> str:
        return self._current_tab

    def _get_tile_rows(self) -> QObject:
        return self._tile_rows

    def _get_selected_count(self) -> int:
        return len(self._selected)

    def _get_cursor_index(self) -> int:
        return self._tile_index(self._cursor)

    def _get_note(self) -> QObject:
        return self.note

    def _get_focused_ticket(self) -> str:
        return self._focused

    def _get_focused_tile(self) -> dict[str, Any]:
        position = self._tile_index(self._focused)
        if position < 0:
            return {}
        return self._tiles()[position].model_dump()

    isConfigured = Property(bool, _get_is_configured, notify=changed)
    error = Property(str, _get_error, notify=changed)
    isRefreshing = Property(bool, _get_is_refreshing, notify=changed)
    hasRefreshed = Property(bool, _get_has_refreshed, notify=changed)
    ticketCount = Property(int, _get_ticket_count, notify=changed)
    tabRows = Property(QObject, _get_tab_rows, constant=True)
    currentTab = Property(str, _get_current_tab, notify=changed)
    tileRows = Property(QObject, _get_tile_rows, constant=True)
    selectedCount = Property(int, _get_selected_count, notify=selectionChanged)
    cursorIndex = Property(int, _get_cursor_index, notify=cursorChanged)
    decisionNote = Property(QObject, _get_note, constant=True)
    focusedTicket = Property(str, _get_focused_ticket, notify=focusChanged)
    focusedTile = Property(dict, _get_focused_tile, notify=focusChanged)

    @property
    def selected_tickets(self) -> list[str]:
        """The selected tickets, in the grid's order."""
        return [tile.ticket for tile in self._tiles() if tile.ticket in self._selected]

    @property
    def focused_ticket(self) -> str:
        """The ticket open in focus mode; empty while the grid shows."""
        return self._focused

    @property
    def tab_tickets(self) -> list[str]:
        """The current tab's tickets, in the grid's order: the order focus mode walks them in."""
        return self._tab_tickets()

    @property
    def state_dir(self) -> Path:
        """The open project's state folder."""
        return self._backend.state_dir

    @property
    def bulk_draft_folder(self) -> Path:
        """The folder holding what is made for the grid's bulk note: its voice notes' recordings."""
        return self._backend.state_dir / REVIEW_DIR_NAME / BULK_NOTE_DIR_NAME

    def item(self, ticket: str) -> ReviewItem | None:
        """A ticket's queue item, or None when it is not in the queue."""
        return self._items.get(ticket)

    def tile(self, ticket: str) -> ReviewTile | None:
        """A ticket's tile in the current tab, or None when the tab does not show it."""
        position = self._tile_index(ticket)
        if position < 0:
            return None
        return self._tiles()[position]

    def is_busy(self, ticket: str) -> bool:
        """Whether a ticket has a decision on its way: submitted, queued, being written or being posted."""
        if ticket in self._submitting:
            return True
        job = self._jobs.get(ticket)
        return job is not None and job.state in _BUSY_JOB_STATES

    # ####################### #
    #   FOLLOWING THE QUEUE   #
    # ####################### #

    def open_project(self) -> None:
        """Start over on the project the backend just opened: its queue, its jobs and its thumbnail folder."""
        self._generation += 1
        self._thumbnails = ThumbnailCache(self._backend.state_dir / THUMBNAIL_FOLDER)
        self._queue = ReviewQueue(is_configured=False)
        self._items = {}
        self._jobs = {}
        self._heroes = {}
        self._pending_heroes = {}
        self._current_tab = ""
        self._selected = set()
        self._cursor = ""
        self._anchor = ""
        self._focused = ""
        self._submitting = set()
        self.note.clear()
        self._rebuild()
        self._run_work(
            lambda: (self._backend.get_review_queue(), self._backend.list_decision_jobs()),
            self._on_loaded,
        )

    def apply_review_event(self, event: ReviewEvent) -> None:
        """Follow a change to the queue by reading it again; the daemon answers from what it already holds."""
        if event.kind is ReviewEventKind.FAILED:
            logger.info(f"[Review] The queue could not be refreshed: {event.error}")
        self._run_work(self._backend.get_review_queue, self._set_queue)

    def apply_job_event(self, event: DecisionJobEvent) -> None:
        """Badge the job's tile with its new state, in place."""
        self._take_jobs([event.job])
        self._refresh_tiles()

    @Slot()
    def refresh(self) -> None:
        """Read the queue from the tracker again now."""
        if not self._queue.is_configured:
            logger.debug("[Review] The project has no review queue to refresh")
            return
        self._is_refreshing = True
        self.changed.emit()
        self._run_work(self._backend.refresh_review_queue, self._set_queue, lambda error: self._end_refreshing())

    def _on_loaded(self, loaded: tuple[ReviewQueue, list[DecisionJob]]) -> None:
        queue, jobs = loaded
        self._take_jobs(jobs)
        self._set_queue(queue)

    def _end_refreshing(self) -> None:
        self._is_refreshing = False
        self.changed.emit()

    def _set_queue(self, queue: ReviewQueue) -> None:
        self._queue = queue
        self._is_refreshing = False
        self._items = {item.ticket: item for item in queue.items}
        tab_names = [tab.name for tab in queue.tabs]
        if self._current_tab not in tab_names:
            self._current_tab = tab_names[0] if tab_names else ""
        self._rebuild()
        self._find_heroes()

    def _take_jobs(self, jobs: list[DecisionJob]) -> None:
        """Keep each job that is its ticket's newest so far (jobs are numbered in the order they were made)."""
        for job in jobs:
            known = self._jobs.get(job.ticket)
            if known is None or job.id >= known.id:
                self._jobs[job.ticket] = job

    # ####################### #
    #        SELECTION        #
    # ####################### #

    @Slot(str)
    def selectTab(self, name: str) -> None:
        """Show one queue's tickets; the selection stays with the tab it was made in."""
        if name == self._current_tab:
            return
        if name not in [tab.name for tab in self._queue.tabs]:
            logger.debug(f"[Review] No queue named {name}")
            return
        self._current_tab = name
        self._selected = set()
        self._cursor = ""
        self._anchor = ""
        self._rebuild()

    @Slot(int, int)
    def clickTile(self, index: int, modifiers: int) -> None:
        """Select as a click does: alone, Ctrl toggles one, Shift selects a range from the last tile clicked."""
        ticket = self._ticket_at(index)
        if not ticket:
            return
        held = Qt.KeyboardModifier(modifiers)
        is_adding = bool(held & Qt.KeyboardModifier.ControlModifier)
        if held & Qt.KeyboardModifier.ShiftModifier and self._anchor:
            range_tickets = self._range(self._anchor, ticket)
            self._set_selection((self._selected | range_tickets) if is_adding else range_tickets)
            self._set_cursor(ticket)
            return
        if is_adding:
            self._set_selection(self._selected ^ {ticket})
        else:
            self._set_selection({ticket})
        self._anchor = ticket
        self._set_cursor(ticket)

    @Slot(int, bool)
    def moveCursor(self, step: int, is_extending: bool) -> None:
        """Move the keyboard cursor by `step` tiles; extending selects the range from the anchor to it."""
        tiles = self._tiles()
        if not tiles:
            return
        position = self._tile_index(self._cursor)
        if position < 0:
            target = 0
        else:
            target = min(max(position + step, 0), len(tiles) - 1)
        ticket = tiles[target].ticket
        if is_extending:
            self._anchor = self._anchor or self._cursor or ticket
            self._set_selection(self._range(self._anchor, ticket))
        self._set_cursor(ticket)

    @Slot()
    def toggleAtCursor(self) -> None:
        """Select or unselect the tile under the cursor (Space)."""
        if not self._cursor:
            self.moveCursor(0, False)
            return
        self._set_selection(self._selected ^ {self._cursor})
        self._anchor = self._cursor

    @Slot()
    def selectAll(self) -> None:
        """Select every tile of the current tab."""
        self._set_selection({tile.ticket for tile in self._tiles()})

    @Slot()
    def clearSelection(self) -> None:
        """Select nothing."""
        self._set_selection(set())

    @Slot(bool)
    def beginBoxSelection(self, is_adding: bool) -> None:
        """Start a drag box: it selects what it covers, added to the selection when `is_adding` (Ctrl)."""
        self._box_base = set(self._selected) if is_adding else set()

    @Slot(list)
    def updateBoxSelection(self, indices: list[Any]) -> None:
        """Select the tiles the drag box covers now, by index."""
        if self._box_base is None:
            logger.debug("[Review] A drag box was updated without being begun")
            return
        covered = {ticket for ticket in (self._ticket_at(int(index)) for index in indices) if ticket}
        self._set_selection(self._box_base | covered)

    @Slot()
    def endBoxSelection(self) -> None:
        """Finish the drag box."""
        self._box_base = None

    # ####################### #
    #   DECISIONS AND FOCUS   #
    # ####################### #

    @Slot(result=int)
    def decisionCount(self) -> int:
        """How many tickets a decision now would cover: the selection, or the tile under the cursor."""
        return len(self._decision_tickets())

    @Slot(str, result=bool)
    def submitDecision(self, verdict: str) -> bool:
        """Queue a decision on the selection (or the tile under the cursor) with the note's words.

        Tickets with a decision already on its way are left out. Returns whether a decision was sent, so the note
        stays open when it was not.
        """
        review_verdict = ReviewVerdict(verdict)
        tickets = self._decision_tickets()
        busy = [ticket for ticket in tickets if self.is_busy(ticket)]
        ready = [ticket for ticket in tickets if ticket not in busy]
        takes = self.note.all_takes()
        if not ready:
            self._show_message("Nothing to decide: select tickets that have no decision on its way")
            return False
        unsettled = self.note.unsettled_takes()
        if unsettled:
            self._show_message(unsettled_takes_message(unsettled))
            return False
        if review_verdict is ReviewVerdict.CHANGES and not takes:
            self._show_message("Say what to change before requesting changes")
            return False

        request = DecisionRequest(tickets=tuple(ready), verdict=review_verdict, takes=takes)
        skipped = f"; {len(busy)} already deciding, left out" if busy else ""
        count = f"{len(ready)} tickets" if len(ready) > 1 else ready[0]
        self.submit(
            request,
            lambda jobs: self._on_decided(jobs, f"{VERDICT_LABELS[review_verdict]}: {count} queued{skipped}"),
        )
        return True

    def submit(
        self,
        request: DecisionRequest,
        on_queued: Callable[[list[DecisionJob]], None],
        on_failed: Callable[[Exception], None] | None = None,
    ) -> None:
        """Send a decision to the daemon in the background and badge its tickets once their jobs are queued.

        The tickets count as busy from now on, so nothing decides them twice while the daemon answers. A refusal is
        shown as the status message, and `on_failed` hears it.
        """
        self._submitting |= set(request.tickets)

        def queued(jobs: list[DecisionJob]) -> None:
            self._submitting -= set(request.tickets)
            self._take_jobs(jobs)
            self._refresh_tiles()
            on_queued(jobs)

        def failed(error: Exception) -> None:
            self._submitting -= set(request.tickets)
            if on_failed is not None:
                on_failed(error)

        self._run_work(lambda: self._backend.decide(request), queued, failed)

    @Slot(str)
    def retryJob(self, ticket: str) -> None:
        """Queue a ticket's failed decision job again."""
        job = self._jobs.get(ticket)
        if job is None or job.state is not DecisionJobState.FAILED:
            logger.debug(f"[Review] {ticket} has no failed decision job to retry")
            return

        def on_retried(retried: DecisionJob) -> None:
            # The note and the selection belong to the next decision, so a retry leaves them alone.
            self._take_jobs([retried])
            self._refresh_tiles()
            self._show_message(f"{ticket}: retrying its decision")

        self._run_work(lambda: self._backend.retry_decision_job(job.id), on_retried)

    @Slot(int)
    def openTicket(self, index: int) -> None:
        """Open one ticket in focus mode."""
        ticket = self._ticket_at(index)
        if not ticket:
            return
        self._set_cursor(ticket)
        self._focused = ticket
        self.focusChanged.emit()
        self.focusRequested.emit(ticket)

    @Slot()
    def openAtCursor(self) -> None:
        """Open the tile under the cursor in focus mode (Enter)."""
        self.openTicket(self._tile_index(self._cursor))

    def focus_ticket(self, ticket: str) -> None:
        """Show another ticket of the current tab in focus mode, keeping the grid's cursor on it."""
        if ticket not in self._tab_tickets():
            logger.debug(f"[Review] {ticket} is not in the {self._current_tab} tab to focus on")
            return
        self._set_cursor(ticket)
        if ticket == self._focused:
            return
        self._focused = ticket
        self.focusChanged.emit()

    @Slot()
    def closeFocus(self) -> None:
        """Go back to the grid."""
        self._focused = ""
        self.focusChanged.emit()

    def _on_decided(self, jobs: list[DecisionJob], message: str) -> None:
        self.note.clear()
        # The note's recordings went with it as words; the next bulk note starts with none.
        shutil.rmtree(self.bulk_draft_folder, ignore_errors=True)
        self._selected -= {job.ticket for job in jobs}
        self._refresh_tiles()
        self.selectionChanged.emit()
        self._show_message(message)

    # -----------------------------------------------------------------------------
    # Tiles
    # -----------------------------------------------------------------------------

    def _tiles(self) -> list[ReviewTile]:
        return [row for row in self._tile_rows.rows if isinstance(row, ReviewTile)]

    def _tab_tickets(self) -> list[str]:
        tab = next((tab for tab in self._queue.tabs if tab.name == self._current_tab), None)
        if tab is None:
            return []
        return [ticket for ticket in tab.tickets if ticket in self._items]

    def _rebuild(self) -> None:
        """Bring the tabs and tiles in line with the queue, keeping the selection and cursor on tickets still there."""
        self._tab_rows.set_rows(
            [
                ReviewTabRow(name=tab.name, count=sum(ticket in self._items for ticket in tab.tickets))
                for tab in self._queue.tabs
            ]
        )
        tickets = self._tab_tickets()
        cursor_position = self._tile_index(self._cursor)
        self._selected &= set(tickets)
        if self._anchor not in tickets:
            self._anchor = ""
        self._tile_rows.sync_rows([self._tile(ticket) for ticket in tickets], key=lambda tile: tile.ticket)
        if self._cursor not in tickets:
            # The cursor's ticket left: stay at the same place in the grid. A new grid starts on its first tile, so
            # the keyboard works at once.
            self._cursor = tickets[min(max(cursor_position, 0), len(tickets) - 1)] if tickets else ""
        if self._focused and self._focused not in self._items:
            self._focused = ""
        self.changed.emit()
        self.selectionChanged.emit()
        self.cursorChanged.emit()
        self.focusChanged.emit()

    def _refresh_tiles(self) -> None:
        """Update every tile whose badge, hero or selection changed, in place."""
        changed = {
            position: fresh for position, tile in enumerate(self._tiles()) if (fresh := self._tile(tile.ticket)) != tile
        }
        self._tile_rows.update_rows(changed)
        if self._focused in {tile.ticket for tile in changed.values()}:
            self.focusChanged.emit()

    def _tile(self, ticket: str) -> ReviewTile:
        item = self._items[ticket]
        hero = self._heroes.get(ticket)
        is_hero_current = hero is not None and hero.basis == _hero_basis(item)
        job = self._jobs.get(ticket)
        is_job_current = job is not None and _is_job_for_delivery(job, item)
        return ReviewTile(
            ticket=ticket,
            summary=item.summary,
            tags=list(item.tags),
            url=item.url,
            thumbnail_url=_file_url(hero.thumbnail_path) if hero and is_hero_current else "",
            hero_url=_file_url(hero.image_path) if hero and is_hero_current else "",
            hero_note=hero.note if hero and is_hero_current else "Loading…",
            job_id=job.id if job and is_job_current else 0,
            job_state=job.state.value if job and is_job_current else "",
            job_label=_job_label(job) if job and is_job_current else "",
            job_error=job.error if job and is_job_current else "",
            is_selected=ticket in self._selected,
        )

    def _set_selection(self, selected: set[str]) -> None:
        if selected == self._selected:
            return
        self._selected = selected
        self._refresh_tiles()
        self.selectionChanged.emit()

    def _set_cursor(self, ticket: str) -> None:
        if ticket == self._cursor:
            return
        self._cursor = ticket
        self.cursorChanged.emit()

    def _ticket_at(self, index: int) -> str:
        tiles = self._tiles()
        if not 0 <= index < len(tiles):
            logger.debug(f"[Review] No tile at {index} of {len(tiles)}")
            return ""
        return tiles[index].ticket

    def _tile_index(self, ticket: str) -> int:
        if not ticket:
            return -1
        return next((position for position, tile in enumerate(self._tiles()) if tile.ticket == ticket), -1)

    def _range(self, first: str, last: str) -> set[str]:
        tickets = [tile.ticket for tile in self._tiles()]
        start, end = sorted((tickets.index(first), tickets.index(last)))
        return set(tickets[start : end + 1])

    def _decision_tickets(self) -> list[str]:
        if self._selected:
            return self.selected_tickets
        if self._cursor:
            return [self._cursor]
        return []

    # -----------------------------------------------------------------------------
    # Heroes
    # -----------------------------------------------------------------------------

    def _find_heroes(self) -> None:
        """Find each ticket's hero and its thumbnail in the background, unless known for its current delivery."""
        thumbnails = self._thumbnails
        if thumbnails is None:
            logger.debug("[Review] No project is open to keep thumbnails for")
            return
        generation = self._generation
        for item in self._queue.items:
            basis = _hero_basis(item)
            known = self._heroes.get(item.ticket)
            if (known and known.basis == basis) or self._pending_heroes.get(item.ticket) == basis:
                continue
            self._pending_heroes[item.ticket] = basis
            self._queue_hero(item, basis, thumbnails, generation)

    def _queue_hero(self, item: ReviewItem, basis: str, thumbnails: ThumbnailCache, generation: int) -> None:
        def on_failed(error: Exception) -> None:
            logger.warning(f"[Review] Could not find the hero image of {item.ticket}: {error}")
            self._on_hero(generation, item.ticket, _Hero(basis=basis, note="Could not read its images"))

        self._tasks.run_thumbnail(
            lambda: self._find_hero(item, basis, thumbnails),
            lambda hero: self._on_hero(generation, item.ticket, hero),
            on_failed,
        )

    def _find_hero(self, item: ReviewItem, basis: str, thumbnails: ThumbnailCache) -> _Hero:
        """Blocking: the hero image of a ticket and its thumbnail."""
        delivery = item.delivery
        if delivery is not None:
            images = delivery_images(delivery)
            if images:
                return _thumbnailed(basis, images[0].path, thumbnails)
        if item.link is not None:
            hero_path = _standard_hero(self._backend.get_history(item.link.source), item.link.exports)
            if hero_path is not None:
                return _thumbnailed(basis, hero_path, thumbnails)
            if delivery is None:
                return _Hero(basis=basis, note="No delivery, and no standard render yet")
        if delivery is None:
            return _Hero(basis=basis, note="No delivery yet")
        file_count = len(delivery.attachments)
        files = "1 file" if file_count == 1 else f"{file_count} files"
        return _Hero(basis=basis, note=f"{files}, no image")

    def _on_hero(self, generation: int, ticket: str, hero: _Hero) -> None:
        if generation != self._generation:
            logger.debug(f"[Review] Dropped the hero of {ticket} found for a project no longer open")
            return
        if self._pending_heroes.get(ticket) == hero.basis:
            del self._pending_heroes[ticket]
        self._heroes[ticket] = hero
        self._refresh_tiles()


# -----------------------------------------------------------------------------
# Small pure helpers
# -----------------------------------------------------------------------------


def delivery_images(delivery: ReviewDelivery) -> list[ReviewAttachment]:
    """A delivery's images in the order it lists them, the first being its hero: by type, or by name without one."""
    return [
        attachment
        for attachment in delivery.attachments
        if attachment.mime_type.startswith("image/") or attachment.path.suffix.lower() in _IMAGE_SUFFIXES
    ]


def _hero_basis(item: ReviewItem) -> str:
    """What a ticket's hero depends on: its delivery, or the source it links to when it has none."""
    delivery = item.delivery.comment_id if item.delivery else ""
    source = item.link.source if item.link else ""
    return f"{delivery}|{source}"


def _thumbnailed(basis: str, image_path: Path, thumbnails: ThumbnailCache) -> _Hero:
    thumbnail_path = thumbnails.thumbnail(image_path)
    if thumbnail_path is None:
        return _Hero(basis=basis, image_path=image_path, note=f"Could not read {image_path.name}")
    return _Hero(basis=basis, image_path=image_path, thumbnail_path=thumbnail_path)


def _standard_hero(history: SourceHistory, exports: tuple[str, ...]) -> Path | None:
    """The newest revision's standard hero render, of an export the ticket names when it names any."""
    if not history.revisions:
        return None
    newest_id = history.revisions[0].id
    for render_set in history.standard_renders:
        is_named_export = not exports or render_set.export in exports
        if render_set.revision_id == newest_id and is_named_export and render_set.state is RenderState.DONE:
            hero_path = render_set.images.get(_HERO_VIEW)
            if hero_path is not None:
                return hero_path
    return None


def _is_job_for_delivery(job: DecisionJob, item: ReviewItem) -> bool:
    """Whether a job decided the delivery the ticket has now, rather than one from an earlier round."""
    if item.delivery is None:
        return True
    try:
        return datetime.fromisoformat(job.created_at) >= datetime.fromisoformat(item.delivery.created_at)
    except ValueError, TypeError:
        # Unparseable, or one with a time zone and one without: show the job rather than hide a decision.
        logger.debug(f"[Review] Could not compare when job {job.id} and the delivery on {item.ticket} were made")
        return True


def _job_label(job: DecisionJob) -> str:
    return f"{VERDICT_LABELS[job.decision.verdict]} · {JOB_STATE_LABELS[job.state]}"


def _file_url(path: Path | None) -> str:
    if path is None:
        return ""
    return QUrl.fromLocalFile(str(path)).toString()
