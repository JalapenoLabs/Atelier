"""A `StudioBackend` over fixed data, for the window's view-model tests: no daemon, no tracker, no Blender."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from atelier.gui.backend import StudioEvent
from atelierd.catalog import CameraBookmark, CatalogChange, Reference, ScanResult, Source, SourceStatus
from atelierd.exports import ConvertedExport, ExportStats
from atelierd.review import (
    Decision,
    DecisionJob,
    DecisionJobState,
    DecisionRequest,
    ReviewQueue,
)
from atelierd.revisions import RestoreResult, SourceHistory
from atelierd.rpc.protocol import DaemonInfo
from atelierd.workers import (
    Approval,
    ApprovalStatus,
    ChatItem,
    ChatItemKind,
    Feedback,
    FeedbackRequest,
    WorkerInfo,
    WorkerSnapshot,
)

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
        self.review_queue = ReviewQueue(is_configured=False)
        self.decision_jobs: list[DecisionJob] = []
        self.decisions: list[DecisionRequest] = []
        self.retried: list[int] = []
        self.refreshes = 0

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

    # ####################### #
    #         REVIEW          #
    # ####################### #

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    def get_review_queue(self) -> ReviewQueue:
        return self.review_queue

    def refresh_review_queue(self) -> ReviewQueue:
        self.refreshes += 1
        return self.review_queue

    def decide(self, request: DecisionRequest) -> list[DecisionJob]:
        self.decisions.append(request)
        bulk_tickets = request.tickets if len(request.tickets) > 1 else ()
        queued = [
            DecisionJob(
                id=len(self.decision_jobs) + position + 1,
                decision=Decision(
                    ticket=ticket,
                    verdict=request.verdict,
                    takes=request.takes,
                    bulk_tickets=bulk_tickets,
                ),
                state=DecisionJobState.QUEUED,
                created_at="2026-11-02T12:00:00+00:00",
                updated_at="2026-11-02T12:00:00+00:00",
            )
            for position, ticket in enumerate(request.tickets)
        ]
        self.decision_jobs = [*reversed(queued), *self.decision_jobs]
        return queued

    def list_decision_jobs(self) -> list[DecisionJob]:
        return list(self.decision_jobs)

    def retry_decision_job(self, job_id: int) -> DecisionJob:
        self.retried.append(job_id)
        job = next(job for job in self.decision_jobs if job.id == job_id)
        return job.model_copy(update={"state": DecisionJobState.QUEUED, "error": ""})

    def release(self, export_path: str) -> None:
        self.released_conversions.setdefault(export_path, threading.Event()).set()
