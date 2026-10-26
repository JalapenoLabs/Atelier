"""Workers: Claude Code sessions on one source each, with their own Blender, scope policy, approvals and feedback."""

from atelierd.workers.approvals import ApprovalBroker
from atelierd.workers.feedback import FeedbackService
from atelierd.workers.models import (
    Approval,
    ApprovalEvent,
    ApprovalStatus,
    ChatItem,
    ChatItemKind,
    Feedback,
    FeedbackRequest,
    Markup,
    Pin,
    SessionInfo,
    Stroke,
    StrokeTool,
    WorkerEvent,
    WorkerEventKind,
    WorkerInfo,
    WorkerSnapshot,
    WorkerStatus,
)
from atelierd.workers.policy import PolicyDecision, ScopePolicy, build_scope_policy, evaluate
from atelierd.workers.pool import WorkerPool
from atelierd.workers.process import BlenderLauncher, launch_background_blender
from atelierd.workers.worker import Worker, WorkerBusyError, WorkerSettings

__all__ = [
    "Approval",
    "ApprovalBroker",
    "ApprovalEvent",
    "ApprovalStatus",
    "BlenderLauncher",
    "ChatItem",
    "ChatItemKind",
    "Feedback",
    "FeedbackRequest",
    "FeedbackService",
    "Markup",
    "Pin",
    "PolicyDecision",
    "ScopePolicy",
    "SessionInfo",
    "Stroke",
    "StrokeTool",
    "Worker",
    "WorkerBusyError",
    "WorkerEvent",
    "WorkerEventKind",
    "WorkerInfo",
    "WorkerPool",
    "WorkerSettings",
    "WorkerSnapshot",
    "WorkerStatus",
    "build_scope_policy",
    "evaluate",
    "launch_background_blender",
]
