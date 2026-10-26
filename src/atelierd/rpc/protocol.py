"""The GUI's JSON-RPC 2.0 protocol: framing, every method with its typed params and result, events and errors.

The server (`server.py`) and the client (`client.py`) both read this module, so a method's shape is written once:
`RpcMethod(name, params model, result adapter)`. Messages are JSON objects, one per line, over the Unix socket.

Events are notifications (no `id`) with method `event`: `{"project": ..., "kind": ..., "event": {...}}`, where
`kind` names the event's model in `EVENT_MODEL_BY_KIND`. A client receives them after `events.subscribe`.

A failure is a JSON-RPC error. Errors the core raises on purpose (`KeyError` for a missing source, `SourceBusyError`
for a restore during a turn) carry their exception's name in `data.type`, so the client raises the same exception
and callers handle a daemon's failure exactly as they would the core's.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from atelierd.catalog import CameraBookmark, CatalogEvent, Reference, ScanResult, Source, SourceStatus
from atelierd.config import ConfigError
from atelierd.exports import ConvertedExport
from atelierd.exports.cache import ExportConversionError
from atelierd.registry import RegistryError
from atelierd.revisions import RestoreResult, RevisionEvent, SourceBusyError, SourceHistory
from atelierd.workers import (
    Approval,
    ApprovalEvent,
    ChatItem,
    Feedback,
    FeedbackRequest,
    WorkerBusyError,
    WorkerEvent,
    WorkerInfo,
    WorkerSnapshot,
)

# Results can be whole catalogs; a line may be this long.
MAX_LINE_BYTES = 64 * 1024 * 1024
EVENT_METHOD = "event"


class RpcMethod[ParamsType: BaseModel, ResultType]:
    """One method: its name, the model its params validate as, and how its result is (de)serialised."""

    def __init__(self, name: str, params: type[ParamsType], result: TypeAdapter[ResultType]) -> None:
        self.name = name
        self.params = params
        self.result = result


# ========================================================================================
# Params
# ========================================================================================


class _Params(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NoParams(_Params):
    """A method that takes nothing."""


class OpenProjectParams(_Params):
    """A project by name (from the registry) or by path."""

    reference: str


class ProjectParams(_Params):
    """A method on one open project."""

    project: str


class SubscribeParams(_Params):
    """Which project's events to receive; none means every project's."""

    project: str | None = None


class SourceParams(ProjectParams):
    """A method on one source of an open project."""

    source: str


class SetStatusParams(SourceParams):
    """A source's new review status."""

    status: SourceStatus


class SaveBookmarkParams(SourceParams):
    """A camera bookmark to save on a source."""

    bookmark: CameraBookmark


class DeleteBookmarkParams(SourceParams):
    """A bookmark to delete, by name."""

    name: str


class AddReferenceParams(ProjectParams):
    """An image file to copy into the project's reference drop folder."""

    image: Path


class ExportParams(ProjectParams):
    """One of the project's exports."""

    export: str


class RevisionExportParams(ProjectParams):
    """One export of one revision."""

    revision_id: int
    export: str


class RestoreParams(SourceParams):
    """The revision to restore a source to."""

    revision_id: int


class PromptParams(SourceParams):
    """A prompt for a source's worker, optionally carrying a feedback."""

    prompt: str
    feedback_id: int | None = None


class PinReferencesParams(SourceParams):
    """The references to pin to a source's next prompt (replacing those pinned)."""

    references: list[str]


class CreateFeedbackParams(ProjectParams):
    """A marked-up capture to store as feedback."""

    request: FeedbackRequest


class ApprovalsParams(_Params):
    """Which project's pending approvals; none means every project's."""

    project: str | None = None


class AnswerApprovalParams(_Params):
    """The reviewer's answer to an escalated tool call."""

    approval_id: str
    is_allowed: bool
    message: str = ""


# ========================================================================================
# Results
# ========================================================================================


class DaemonInfo(BaseModel):
    """What the daemon a client reached is, so the window can tell it is not the one it expected."""

    model_config = ConfigDict(frozen=True)

    pid: int
    socket_path: Path
    http_port: int
    state_root: Path | None = Field(default=None, description="Where project state lives, when not in each project.")


class OpenedProject(BaseModel):
    """A project the daemon has open, with where its files and state are, and a fresh scan."""

    model_config = ConfigDict(frozen=True)

    name: str
    root: Path
    state_dir: Path
    objects_dir: Path = Field(description="The object store, so the window can read snapshotted bytes by hash.")
    scan: ScanResult


# ========================================================================================
# Methods
# ========================================================================================

DAEMON_INFO = RpcMethod("daemon.info", NoParams, TypeAdapter(DaemonInfo))
SUBSCRIBE = RpcMethod("events.subscribe", SubscribeParams, TypeAdapter(type(None)))

LIST_PROJECTS = RpcMethod("projects.list", NoParams, TypeAdapter(list[str]))
OPEN_PROJECT = RpcMethod("projects.open", OpenProjectParams, TypeAdapter(OpenedProject))

RESCAN = RpcMethod("catalog.rescan", ProjectParams, TypeAdapter(ScanResult))
LIST_SOURCES = RpcMethod("catalog.list_sources", ProjectParams, TypeAdapter(list[Source]))
LIST_REFERENCES = RpcMethod("catalog.list_references", ProjectParams, TypeAdapter(list[Reference]))
SET_STATUS = RpcMethod("catalog.set_status", SetStatusParams, TypeAdapter(type(None)))
LIST_BOOKMARKS = RpcMethod("catalog.list_bookmarks", SourceParams, TypeAdapter(list[CameraBookmark]))
SAVE_BOOKMARK = RpcMethod("catalog.save_bookmark", SaveBookmarkParams, TypeAdapter(type(None)))
DELETE_BOOKMARK = RpcMethod("catalog.delete_bookmark", DeleteBookmarkParams, TypeAdapter(type(None)))
ADD_REFERENCE = RpcMethod("catalog.add_reference", AddReferenceParams, TypeAdapter(str))
CONVERT_EXPORT = RpcMethod("exports.convert", ExportParams, TypeAdapter(ConvertedExport))

GET_HISTORY = RpcMethod("revisions.history", SourceParams, TypeAdapter(SourceHistory))
REQUEST_RENDERS = RpcMethod("revisions.request_renders", RevisionExportParams, TypeAdapter(type(None)))
CONVERT_REVISION_EXPORT = RpcMethod("revisions.convert_export", RevisionExportParams, TypeAdapter(ConvertedExport))
RESTORE = RpcMethod("revisions.restore", RestoreParams, TypeAdapter(RestoreResult))

GET_WORKER = RpcMethod("workers.get", SourceParams, TypeAdapter(WorkerSnapshot))
LIST_WORKERS = RpcMethod("workers.list", ProjectParams, TypeAdapter(list[WorkerInfo]))
SEND_PROMPT = RpcMethod("workers.prompt", PromptParams, TypeAdapter(ChatItem))
INTERRUPT = RpcMethod("workers.interrupt", SourceParams, TypeAdapter(type(None)))
NEW_SESSION = RpcMethod("workers.new_session", SourceParams, TypeAdapter(type(None)))
STOP_WORKER = RpcMethod("workers.stop", SourceParams, TypeAdapter(type(None)))
PIN_REFERENCES = RpcMethod("workers.pin_references", PinReferencesParams, TypeAdapter(list[str]))

CREATE_FEEDBACK = RpcMethod("feedback.create", CreateFeedbackParams, TypeAdapter(Feedback))
LIST_FEEDBACK = RpcMethod("feedback.list", SourceParams, TypeAdapter(list[Feedback]))

LIST_APPROVALS = RpcMethod("approvals.list", ApprovalsParams, TypeAdapter(list[Approval]))
ANSWER_APPROVAL = RpcMethod("approvals.answer", AnswerApprovalParams, TypeAdapter(Approval))


# ========================================================================================
# Events
# ========================================================================================


class EventKind(StrEnum):
    """Which model an event notification carries."""

    CATALOG = "catalog"
    REVISION = "revision"
    WORKER = "worker"
    APPROVAL = "approval"


EVENT_MODEL_BY_KIND: dict[EventKind, type[CatalogEvent | RevisionEvent | WorkerEvent | ApprovalEvent]] = {
    EventKind.CATALOG: CatalogEvent,
    EventKind.REVISION: RevisionEvent,
    EventKind.WORKER: WorkerEvent,
    EventKind.APPROVAL: ApprovalEvent,
}
EVENT_KIND_BY_MODEL = {model: kind for kind, model in EVENT_MODEL_BY_KIND.items()}
if set(EVENT_MODEL_BY_KIND) != set(EventKind):
    raise RuntimeError("Every event kind needs its model in EVENT_MODEL_BY_KIND")


class EventNotification(BaseModel):
    """The params of an `event` notification."""

    model_config = ConfigDict(frozen=True)

    project: str
    kind: EventKind
    event: dict[str, Any]


# ========================================================================================
# Errors
# ========================================================================================

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# A failure the core raised on purpose; `data.type` names it.
APPLICATION_ERROR = -32000

# Exceptions that cross the socket as themselves. Anything else arrives as an `RpcError`.
ERROR_TYPE_BY_NAME: dict[str, type[Exception]] = {
    error_type.__name__: error_type
    for error_type in (
        ConfigError,
        ExportConversionError,
        FileNotFoundError,
        KeyError,
        NotADirectoryError,
        RegistryError,
        SourceBusyError,
        ValueError,
        WorkerBusyError,
    )
}


class RpcError(Exception):
    """A JSON-RPC error that is not one of the core's own exceptions."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
