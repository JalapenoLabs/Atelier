"""The Chat tab's view-model: the selected source's worker, its current session's chat, and the composer's actions.

The chat follows the daemon. Selecting a source loads its worker's snapshot (its state and the whole session so far),
and from then on every `WorkerEvent` for that source adds or updates one row in place: a tool call gains its result,
an approval card its answer. Rows are kept newest first, by the transcript's index, so a late snapshot and the events
that crossed it agree; the chat's list draws them bottom to top, which keeps the newest exactly at the bottom.
"""

from __future__ import annotations

import bisect
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict
from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

from atelier.gui.backend import StudioBackend
from atelier.gui.desktop import show_in_blender
from atelier.gui.list_model import RowListModel
from atelierd.workers import ChatItem, WorkerEvent, WorkerEventKind, WorkerInfo, WorkerSnapshot, WorkerStatus

logger = logging.getLogger(__name__)


class RunWork(Protocol):
    """Runs backend work off the UI thread and calls back on it: the studio's runner, which also shows failures."""

    def __call__(
        self,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        """Run `work`, then `on_done` with its result, or `on_error` (after the failure is shown) if it raised."""
        ...


STATUS_LABELS = {
    WorkerStatus.STOPPED: "Not running",
    WorkerStatus.STARTING: "Starting Blender and Claude Code…",
    WorkerStatus.IDLE: "Idle",
    WorkerStatus.RUNNING: "Working…",
    WorkerStatus.FAILED: "Failed",
}
if set(STATUS_LABELS) != set(WorkerStatus):
    raise RuntimeError("Every worker status needs a label")


class ChatRow(BaseModel):
    """One entry in the chat, as its delegate reads it."""

    model_config = ConfigDict(frozen=True)

    index: int
    kind: str
    text: str
    detail: str
    tool_name: str
    tool_summary: str
    tool_output: str
    is_error: bool
    is_done: bool
    image_urls: list[str]
    approval_id: str
    approval_status: str
    revision_label: str
    time: str


def chat_row(item: ChatItem) -> ChatRow:
    """The row QML draws for one chat item."""
    created = datetime.fromisoformat(item.created_at).astimezone()
    return ChatRow(
        index=item.index,
        kind=item.kind.value,
        text=item.text,
        detail=item.detail,
        tool_name=item.tool_name,
        tool_summary=item.tool_summary,
        tool_output=item.tool_output,
        is_error=item.is_error,
        is_done=item.is_done,
        image_urls=[QUrl.fromLocalFile(str(image)).toString() for image in item.images],
        approval_id=item.approval_id,
        approval_status=item.approval_status.value if item.approval_status else "",
        revision_label=f"r{item.revision_number}" if item.revision_number else "",
        time=created.strftime("%H:%M"),
    )


class Chat(QObject):
    """The selected source's worker and chat."""

    changed = Signal()
    # A source's snapshot arrived: the screenshot path waits for this before grabbing the chat.
    loaded = Signal()

    def __init__(self, backend: StudioBackend, run: RunWork, show_message: Callable[[str], None]) -> None:
        super().__init__()
        self._backend = backend
        self._run = run
        self._show_message = show_message
        self._source = ""
        self._info: WorkerInfo | None = None
        self._session_count = 0
        self._rows = RowListModel(ChatRow)
        # The transcript index of every row, ascending; row 0 is the newest, so a row's position counts down.
        self._indices: list[int] = []

    # ####################### #
    #       PROPERTIES        #
    # ####################### #

    def _get_rows(self) -> QObject:
        return self._rows

    def _get_source(self) -> str:
        return self._source

    def _get_status(self) -> str:
        return self._info.status.value if self._info else ""

    def _get_status_label(self) -> str:
        if not self._info:
            return ""
        label = STATUS_LABELS[self._info.status]
        if self._info.status is WorkerStatus.FAILED and self._info.error:
            label += f": {self._info.error}"
        return label

    def _get_is_turn_running(self) -> bool:
        return bool(self._info and self._info.is_turn_running)

    def _get_session_label(self) -> str:
        if not (self._info and self._info.session_id):
            return "No session yet"
        label = f"Session {self._info.session_id[:8]}"
        if self._session_count > 1:
            label += f" (of {self._session_count})"
        return label

    def _get_pinned_references(self) -> list[str]:
        return list(self._info.pinned_references) if self._info else []

    def _get_blender_port(self) -> int:
        if not (self._info and self._info.blender_port):
            return 0
        return self._info.blender_port

    rows = Property(QObject, _get_rows, constant=True)
    source = Property(str, _get_source, notify=changed)
    status = Property(str, _get_status, notify=changed)
    statusLabel = Property(str, _get_status_label, notify=changed)
    isTurnRunning = Property(bool, _get_is_turn_running, notify=changed)
    sessionLabel = Property(str, _get_session_label, notify=changed)
    pinnedReferences = Property(list, _get_pinned_references, notify=changed)
    blenderPort = Property(int, _get_blender_port, notify=changed)

    @property
    def pinned_references(self) -> list[str]:
        """The references the selected source's next prompt carries."""
        return self._get_pinned_references()

    # ####################### #
    #     FOLLOWING A SOURCE  #
    # ####################### #

    def load(self, source_path: str) -> None:
        """Show a source's worker and chat (or nothing, for no source)."""
        self._source = source_path
        self._info = None
        self._set_rows([])
        self.changed.emit()
        if not source_path:
            return
        self._run(lambda: self._backend.get_worker(source_path), self._on_snapshot)

    def apply_event(self, event: WorkerEvent) -> None:
        """Follow one event of the selected source's worker."""
        if event.source != self._source:
            return
        self._info = event.info
        if event.kind is WorkerEventKind.RESET:
            self._set_rows([])
        if event.item:
            self._upsert(event.item)
        self.changed.emit()

    def _on_snapshot(self, snapshot: WorkerSnapshot) -> None:
        if snapshot.info.source != self._source:
            logger.debug(f"[Chat] Dropping the snapshot of {snapshot.info.source}, no longer selected")
            return
        self._info = snapshot.info
        self._session_count = len(snapshot.sessions)
        self._set_rows(list(snapshot.items))
        self.changed.emit()
        self.loaded.emit()

    def _set_rows(self, items: list[ChatItem]) -> None:
        ordered = sorted(items, key=lambda item: item.index)
        self._indices = [item.index for item in ordered]
        self._rows.set_rows([chat_row(item) for item in reversed(ordered)])

    def _upsert(self, item: ChatItem) -> None:
        slot = bisect.bisect_left(self._indices, item.index)
        is_known = slot < len(self._indices) and self._indices[slot] == item.index
        if is_known:
            self._rows.replace_row(len(self._indices) - 1 - slot, chat_row(item))
            return
        self._indices.insert(slot, item.index)
        self._rows.insert_row(len(self._indices) - 1 - slot, chat_row(item))

    # ####################### #
    #         ACTIONS         #
    # ####################### #

    @Slot(str)
    def send(self, text: str) -> None:
        """Send the composer's text as the next prompt."""
        source_path = self._source
        if not (source_path and text.strip()):
            logger.debug("[Chat] send called with no source or no text")
            return
        self._run(lambda: self._backend.send_prompt(source_path, text), lambda item: None)

    @Slot()
    def interrupt(self) -> None:
        """Stop the running turn."""
        source_path = self._source
        self._run(lambda: self._backend.interrupt(source_path), lambda _: self._show_message("Interrupting…"))

    @Slot()
    def newSession(self) -> None:
        """Start the next prompt in a fresh session."""
        source_path = self._source

        def start_over() -> WorkerSnapshot:
            self._backend.new_session(source_path)
            return self._backend.get_worker(source_path)

        self._run(start_over, self._on_snapshot)

    @Slot()
    def stopWorker(self) -> None:
        """Stop the source's Claude Code and Blender."""
        source_path = self._source
        self._run(lambda: self._backend.stop_worker(source_path), lambda _: self._show_message("Worker stopped"))

    @Slot()
    def showInBlender(self) -> None:
        """Open the source's .blend in a windowed Blender, out of sight until you go to it."""
        if not self._source:
            logger.debug("[Chat] showInBlender called with no source")
            return
        blend_file: Path = self._backend.resolve(self._source)
        name = PurePosixPath(self._source).name
        self._run(lambda: show_in_blender(blend_file), lambda where: self._show_message(f"Opened {name} {where}"))
