"""What the manager MCP's tools return: compact views of the core's models, shaped for a model to read.

The core's own models carry what the window needs (absolute paths into the state folder, approval ids, chat
details). A manager needs less, and every field it reads costs its context, so these keep only what a manager acts
on. Revisions are named by number (the N of rN), as the window shows them, never by their database id.

Approvals appear as `PendingApproval`, which has no id on purpose: nothing a manager receives could answer one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from atelierd.catalog.models import CameraBookmark, Source, SourceStatus
from atelierd.exports import ExportStats
from atelierd.revisions import RenderState, RevisionTrigger
from atelierd.workers.models import Pin, WorkerInfo, WorkerStatus


class _ManagerModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class ProjectSummary(_ManagerModel):
    """A project this machine knows."""

    name: str
    is_open: bool = Field(description="Whether the daemon has it open (its watcher and workers running).")


class SourceSummary(_ManagerModel):
    """One source in a listing."""

    path: str
    status: SourceStatus
    exports: int = Field(description="How many FBX exports it has.")
    worker: WorkerStatus | None = Field(default=None, description="Its worker's state, when one has run.")


class ExportDetail(_ManagerModel):
    """One export with Blender's measurements of it."""

    path: str
    stats: ExportStats | None = None
    stats_error: str = Field(default="", description="Why it could not be measured, when it could not.")


class RevisionSummary(_ManagerModel):
    """One revision of a source, with where each export's standard renders stand."""

    number: int
    trigger: RevisionTrigger
    created_at: str
    restored_from: int | None = Field(
        default=None, description="For a restore, the number of the revision it wrote back."
    )
    renders: dict[str, RenderState] = Field(default_factory=dict, description="Each export's standard renders.")


class SourceDetail(_ManagerModel):
    """Everything about one source a manager reviews from."""

    source: Source
    exports: tuple[ExportDetail, ...] = ()
    revisions: tuple[RevisionSummary, ...] = Field(default=(), description="Newest first.")
    restored_to: int | None = Field(
        default=None,
        description="The revision it was restored to, until a worker turn has been told about the restore.",
    )
    worker: WorkerInfo
    feedback: int = Field(default=0, description="How many marked-up feedbacks it has (list_feedback reads them).")


class PendingApproval(_ManagerModel):
    """A worker's tool call waiting on the human reviewer. A manager can see it but never answer it."""

    source: str
    tool_name: str
    tool_summary: str
    reason: str = Field(description="Why the scope policy escalated it.")
    created_at: str


class WorkerState(_ManagerModel):
    """One source's worker, with what is waiting on the reviewer and how its last turn ended."""

    info: WorkerInfo
    pending_approvals: tuple[PendingApproval, ...] = ()
    last_result: str | None = Field(default=None, description="How the last turn of this session ended.")
    last_revision: int | None = Field(default=None, description="The revision the last turn recorded.")
    note: str = ""


class ProjectWorkers(_ManagerModel):
    """Every worker of a project and every approval waiting on the reviewer."""

    workers: tuple[WorkerInfo, ...] = ()
    pending_approvals: tuple[PendingApproval, ...] = ()


class FeedbackSummary(_ManagerModel):
    """One marked-up feedback: what it critiques and its numbered pins."""

    id: int
    created_at: str
    revision: int | None = Field(default=None, description="The revision number the marked-up capture shows.")
    export: str | None = None
    pins: tuple[Pin, ...] = ()
    camera: CameraBookmark | None = Field(default=None, description="The viewport camera, for a frozen 3D frame.")


class RestoreSummary(_ManagerModel):
    """What a restore did."""

    restored_to: int = Field(description="The revision whose files were written back.")
    pre_restore: int = Field(description="The state before the restore; restoring it undoes the restore.")
    recorded: int = Field(description="The new revision recording the restored state.")
    written: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    skipped: tuple[str, ...] = Field(default=(), description="Shared files left alone because other sources use them.")
