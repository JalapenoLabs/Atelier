"""Domain models of workers: their state, their chat transcripts, approvals, feedback and the events they raise.

Paths of a source's files are project-relative, like the catalog's. Paths into the state folder (transcript images,
feedback images) are absolute, like the export cache's: the window reads those files straight from disk.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from atelierd.catalog.models import CameraBookmark


class WorkerStatus(StrEnum):
    """Where one source's worker stands."""

    # No processes: never started, stopped, or the daemon restarted. The next prompt starts it.
    STOPPED = "stopped"
    # Its Blender and Claude Code are starting for the turn just sent.
    STARTING = "starting"
    # Running, between turns.
    IDLE = "idle"
    # A turn is running.
    RUNNING = "running"
    # Its processes died or would not start; the next prompt starts them again.
    FAILED = "failed"


class WorkerInfo(BaseModel):
    """One source's worker, as the window's chat header and a manager see it."""

    model_config = ConfigDict(frozen=True)

    source: str
    status: WorkerStatus = WorkerStatus.STOPPED
    session_id: str | None = Field(default=None, description="The session the composer continues, if any.")
    blender_port: int | None = Field(default=None, description="The port of its background Blender, while running.")
    pending_approvals: int = 0
    error: str = Field(default="", description="Why it failed, when it did.")

    @property
    def is_turn_running(self) -> bool:
        """Whether a turn is starting or running, which is when restores are refused."""
        return self.status in (WorkerStatus.STARTING, WorkerStatus.RUNNING)


class ChatItemKind(StrEnum):
    """What one entry in a worker's chat is."""

    # A prompt sent to the worker, by the reviewer or a manager.
    USER = "user"
    # Text the worker wrote.
    ASSISTANT = "assistant"
    # A tool call, with its result once it arrives.
    TOOL = "tool"
    # A tool call the scope policy escalated, with how it was answered.
    APPROVAL = "approval"
    # The end of a turn: how it ended and the revision recorded after it.
    RESULT = "result"
    # Something Atelier itself reports: a session started, a worker failed.
    NOTICE = "notice"


class ApprovalStatus(StrEnum):
    """Where an escalated tool call stands."""

    PENDING = "pending"
    ALLOWED = "allowed"
    DENIED = "denied"
    # The turn ended (interrupted, the worker stopped) before anyone answered.
    CANCELLED = "cancelled"


class ChatItem(BaseModel):
    """One entry in a worker's chat, as the chat panel draws it."""

    model_config = ConfigDict(frozen=True)

    index: int = Field(
        description="Position in the session's transcript; a later item with the same index replaces it."
    )
    kind: ChatItemKind
    created_at: str = Field(description="ISO 8601, UTC.")
    text: str = Field(default="", description="The prompt, the worker's text, a result summary or a notice.")
    detail: str = Field(default="", description="For a prompt, the brief Atelier sent with it.")
    tool_name: str = ""
    tool_use_id: str = ""
    tool_summary: str = Field(default="", description="One line saying what the tool call does.")
    tool_output: str = Field(default="", description="The start of the tool's result text.")
    is_error: bool = False
    is_done: bool = Field(default=False, description="For a tool call, whether its result has arrived.")
    images: tuple[Path, ...] = Field(default=(), description="Images the tool returned, saved under the state folder.")
    approval_id: str = ""
    approval_status: ApprovalStatus | None = None
    revision_number: int | None = Field(default=None, description="For a result, the revision recorded after it.")
    feedback_id: int | None = Field(default=None, description="For a prompt, the feedback it sent.")


class SessionInfo(BaseModel):
    """One Claude Code session a worker ran on a source."""

    model_config = ConfigDict(frozen=True)

    session_id: str
    source: str
    started_at: str
    last_turn_at: str | None = None


class WorkerSnapshot(BaseModel):
    """Everything the chat panel shows for one source: its worker and the current session's transcript."""

    model_config = ConfigDict(frozen=True)

    info: WorkerInfo
    items: tuple[ChatItem, ...] = ()
    sessions: tuple[SessionInfo, ...] = Field(default=(), description="Newest first.")
    pinned_references: tuple[str, ...] = ()


class Approval(BaseModel):
    """A tool call the scope policy escalated to the reviewer."""

    model_config = ConfigDict(frozen=True)

    id: str
    project: str
    source: str
    tool_name: str
    tool_summary: str
    tool_input: dict[str, object] = Field(default_factory=dict)
    reason: str = Field(description="Why the scope policy escalated it.")
    created_at: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    message: str = Field(default="", description="The reviewer's note with a denial, passed to the worker.")


# ========================================================================================
# Feedback
# ========================================================================================


class StrokeTool(StrEnum):
    """A markup stroke's shape."""

    PEN = "pen"
    ARROW = "arrow"
    BOX = "box"


class Stroke(BaseModel):
    """One markup stroke, in coordinates from 0 to 1 across the image's width and height."""

    model_config = ConfigDict(frozen=True)

    tool: StrokeTool
    color: str = Field(description="A `#rrggbb` colour.")
    points: tuple[tuple[float, float], ...] = Field(
        description="A pen's path, or an arrow's or box's two corners: from where the drag started to where it ended.",
    )


class Pin(BaseModel):
    """A numbered marker on the image with its note, at coordinates from 0 to 1."""

    model_config = ConfigDict(frozen=True)

    number: int
    x: float
    y: float
    note: str = ""


class Markup(BaseModel):
    """What was drawn on a capture: `markup.json` in its feedback folder."""

    model_config = ConfigDict(frozen=True)

    strokes: tuple[Stroke, ...] = ()
    pins: tuple[Pin, ...] = ()


class FeedbackRequest(BaseModel):
    """A capture the window marked up, about to become feedback. The images are files the window wrote."""

    model_config = ConfigDict(frozen=True)

    source: str
    capture_path: Path = Field(description="The clean render or viewport frame.")
    annotated_path: Path = Field(description="The capture with the markup flattened onto it.")
    markup: Markup = Field(default_factory=Markup)
    revision_id: int | None = Field(default=None, description="The revision the capture shows.")
    export: str | None = Field(default=None, description="The export the capture shows, when it shows one.")
    camera: CameraBookmark | None = Field(default=None, description="The viewport camera, for a frozen frame.")


class Feedback(BaseModel):
    """Markup stored in `<state>/feedback/<source>/<id>/`, as the worker's prompt and a manager see it."""

    model_config = ConfigDict(frozen=True)

    id: int
    source: str
    created_at: str
    folder: Path
    capture_path: Path
    annotated_path: Path
    pins: tuple[Pin, ...] = ()
    revision_id: int | None = None
    revision_number: int | None = None
    export: str | None = None
    camera: CameraBookmark | None = None


# ========================================================================================
# Events
# ========================================================================================


class WorkerEventKind(StrEnum):
    """What a `WorkerEvent` reports."""

    # The worker's status, session or pending approvals changed.
    STATE = "state"
    # A chat item was added, or an earlier one updated (a tool's result arrived, an approval was answered).
    ITEM = "item"
    # The chat started over: a new session.
    RESET = "reset"


class WorkerEvent(BaseModel):
    """Something one source's worker did."""

    model_config = ConfigDict(frozen=True)

    kind: WorkerEventKind
    source: str
    info: WorkerInfo
    item: ChatItem | None = None


class ApprovalEvent(BaseModel):
    """An escalated tool call that was raised, or answered (or cancelled)."""

    model_config = ConfigDict(frozen=True)

    approval: Approval
