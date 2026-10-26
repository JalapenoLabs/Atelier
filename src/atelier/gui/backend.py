"""The GUI's one door into the core: everything the window reads or changes goes through a `StudioBackend`.

`RpcBackend` is the implementation: a client of the daemon (`atelierd`) over its JSON-RPC socket. The core runs
there, not here, so workers keep running when the window closes. The protocol speaks only in core models and
project-relative paths (plus absolute paths into the state folder), and nothing above it imports the daemon's
services, so the window never depends on where the core runs.

Every method may block (a round trip to the daemon, which may scan or call Blender), so the window calls them off
the UI thread.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from atelierd.catalog import CameraBookmark, Reference, ScanResult, Source, SourceStatus
from atelierd.daemon.core import CoreEvent
from atelierd.daemon.system import start_in_background
from atelierd.exports import ConvertedExport
from atelierd.revisions import RestoreResult, SourceHistory
from atelierd.revisions.objects import ObjectStore
from atelierd.rpc import protocol
from atelierd.rpc.client import DaemonUnavailableError, RpcClient
from atelierd.rpc.protocol import DaemonInfo, OpenedProject
from atelierd.workers import Approval, ApprovalEvent, ChatItem, Feedback, FeedbackRequest, WorkerSnapshot

logger = logging.getLogger(__name__)


class DaemonDisconnected(BaseModel):
    """The connection to the daemon was lost."""

    model_config = ConfigDict(frozen=True)

    socket_path: Path


# Everything the window hears as it happens: the open project's changes, revisions and worker output, every
# project's approvals, and losing the daemon.
type StudioEvent = CoreEvent | DaemonDisconnected


class StudioBackend(Protocol):
    """What the window needs from the core, for one open project at a time."""

    # ####################### #
    #        THE DAEMON       #
    # ####################### #

    def connect(self, on_event: Callable[[StudioEvent], None]) -> DaemonInfo:
        """Reach the daemon and report every later event to `on_event` (on any thread).

        Raises:
            DaemonUnavailableError: no daemon is running.
        """
        ...

    def start_daemon(self) -> str:
        """Start a daemon in the background that outlives the window, and say how it was started."""
        ...

    # ####################### #
    #   PROJECTS AND CATALOG  #
    # ####################### #

    def list_projects(self) -> list[str]:
        """Return the names of the projects this machine knows, sorted."""
        ...

    def open_project(self, reference: str) -> ScanResult:
        """Open a project by name or path and scan it; its later events reach `on_event`."""
        ...

    def close(self) -> None:
        """Stop following the open project and let go of the daemon. Its workers keep running there."""
        ...

    @property
    def project_name(self) -> str:
        """The open project's name, or an empty string."""
        ...

    def resolve(self, project_path: str) -> Path:
        """Return the absolute path of a project-relative path in the open project."""
        ...

    def rescan(self) -> ScanResult:
        """Scan the open project again now, rather than when the watcher next notices a change."""
        ...

    def list_sources(self) -> list[Source]:
        """Return the open project's sources."""
        ...

    def list_references(self) -> list[Reference]:
        """Return the open project's reference images."""
        ...

    def set_status(self, source_path: str, status: SourceStatus) -> None:
        """Set a source's review status."""
        ...

    def list_bookmarks(self, source_path: str) -> list[CameraBookmark]:
        """Return a source's camera bookmarks."""
        ...

    def save_bookmark(self, source_path: str, bookmark: CameraBookmark) -> None:
        """Save a camera bookmark on a source."""
        ...

    def delete_bookmark(self, source_path: str, name: str) -> None:
        """Delete a source's camera bookmark."""
        ...

    def add_reference(self, image: Path) -> str:
        """Copy an image into the reference drop folder and return its project-relative path."""
        ...

    def convert_export(self, export_path: str) -> ConvertedExport:
        """Return an export converted to `.glb` with its stats, converting it on a cache miss."""
        ...

    # ####################### #
    #        REVISIONS        #
    # ####################### #

    def get_history(self, source_path: str) -> SourceHistory:
        """Return a source's revisions, newest first, with their standard renders and its restore mark."""
        ...

    def request_standard_renders(self, revision_id: int, export_path: str) -> None:
        """Render one export of a revision in the background, unless its renders exist or are on their way."""
        ...

    def convert_revision_export(self, revision_id: int, export_path: str) -> ConvertedExport:
        """Return one of a revision's exports, as snapshotted, converted to `.glb` with its stats."""
        ...

    def object_path(self, sha256: str) -> Path:
        """Return where a snapshotted file's bytes are on disk."""
        ...

    def restore_revision(self, source_path: str, revision_id: int) -> RestoreResult:
        """Write a whole earlier revision of a source back into the project, snapshotting the current state first."""
        ...

    # ####################### #
    #   WORKERS AND FEEDBACK  #
    # ####################### #

    def get_worker(self, source_path: str) -> WorkerSnapshot:
        """Return a source's worker state and its current session's chat."""
        ...

    def send_prompt(self, source_path: str, prompt: str, feedback_id: int | None = None) -> ChatItem:
        """Start a worker turn on a source, optionally carrying a feedback."""
        ...

    def interrupt(self, source_path: str) -> None:
        """Stop the running turn on a source."""
        ...

    def new_session(self, source_path: str) -> None:
        """End a source's session; its next prompt opens a fresh one."""
        ...

    def stop_worker(self, source_path: str) -> None:
        """Stop a source's Claude Code and Blender; the session resumes on the next prompt."""
        ...

    def pin_references(self, source_path: str, reference_paths: list[str]) -> list[str]:
        """Pin references to a source's next prompt, replacing those pinned."""
        ...

    def create_feedback(self, request: FeedbackRequest) -> Feedback:
        """Store a marked-up capture as feedback on its source."""
        ...

    def list_approvals(self) -> list[Approval]:
        """Return every project's pending approvals, oldest first."""
        ...

    def answer_approval(self, approval_id: str, is_allowed: bool, message: str) -> Approval:
        """Allow or deny an escalated tool call."""
        ...


class RpcBackend:
    """A `StudioBackend` over the daemon's JSON-RPC socket."""

    def __init__(self, socket_path: Path, state_root: Path | None = None) -> None:
        """Create the backend; `connect` reaches the daemon.

        Args:
            socket_path: the daemon's socket.
            state_root: the state root to start a daemon with, when the window starts one.
        """
        self._socket_path = socket_path
        self._state_root = state_root
        self._client: RpcClient | None = None
        self._on_event: Callable[[StudioEvent], None] | None = None
        self._opened: OpenedProject | None = None

    # ####################### #
    #        THE DAEMON       #
    # ####################### #

    def connect(self, on_event: Callable[[StudioEvent], None]) -> DaemonInfo:
        """Reach the daemon and report every later event to `on_event` (on the client's reader thread).

        Raises:
            DaemonUnavailableError: no daemon is running.
        """
        if self._client:
            self._client.close()
        self._on_event = on_event
        client = RpcClient(self._socket_path, on_event=self._deliver, on_disconnect=self._on_disconnect)
        client.connect()
        self._client = client
        # Every project's events: approvals from any of them light the badge; the rest are filtered in `_deliver`.
        client.call(protocol.SUBSCRIBE, protocol.SubscribeParams())
        info = client.call(protocol.DAEMON_INFO, protocol.NoParams())
        if info.state_root != self._state_root:
            logger.warning(
                f"[Backend] The daemon on {self._socket_path} keeps state in {info.state_root or 'each project'}, "
                f"not {self._state_root or 'each project'} as this window was started with"
            )
        return info

    def start_daemon(self) -> str:
        """Start a daemon in the background that outlives the window, and say how it was started."""
        return start_in_background(self._socket_path, self._state_root)

    # ####################### #
    #   PROJECTS AND CATALOG  #
    # ####################### #

    def list_projects(self) -> list[str]:
        """Return the names of the projects this machine knows, sorted."""
        return self._call(protocol.LIST_PROJECTS, protocol.NoParams())

    def open_project(self, reference: str) -> ScanResult:
        """Open a project by name or path and scan it; its later events reach `on_event`."""
        opened = self._call(protocol.OPEN_PROJECT, protocol.OpenProjectParams(reference=reference))
        self._opened = opened
        return opened.scan

    def close(self) -> None:
        """Stop following the open project and let go of the daemon. Its workers keep running there."""
        self._opened = None
        if self._client:
            self._client.close()
            self._client = None

    @property
    def project_name(self) -> str:
        """The open project's name, or an empty string."""
        return self._opened.name if self._opened else ""

    def resolve(self, project_path: str) -> Path:
        """Return the absolute path of a project-relative path in the open project."""
        return self._open().root / project_path

    def rescan(self) -> ScanResult:
        """Scan the open project again now, rather than when the watcher next notices a change."""
        return self._call(protocol.RESCAN, self._project_params())

    def list_sources(self) -> list[Source]:
        """Return the open project's sources."""
        return self._call(protocol.LIST_SOURCES, self._project_params())

    def list_references(self) -> list[Reference]:
        """Return the open project's reference images."""
        return self._call(protocol.LIST_REFERENCES, self._project_params())

    def set_status(self, source_path: str, status: SourceStatus) -> None:
        """Set a source's review status."""
        params = protocol.SetStatusParams(project=self._open().name, source=source_path, status=status)
        self._call(protocol.SET_STATUS, params)

    def list_bookmarks(self, source_path: str) -> list[CameraBookmark]:
        """Return a source's camera bookmarks."""
        return self._call(protocol.LIST_BOOKMARKS, self._source_params(source_path))

    def save_bookmark(self, source_path: str, bookmark: CameraBookmark) -> None:
        """Save a camera bookmark on a source."""
        params = protocol.SaveBookmarkParams(project=self._open().name, source=source_path, bookmark=bookmark)
        self._call(protocol.SAVE_BOOKMARK, params)

    def delete_bookmark(self, source_path: str, name: str) -> None:
        """Delete a source's camera bookmark."""
        params = protocol.DeleteBookmarkParams(project=self._open().name, source=source_path, name=name)
        self._call(protocol.DELETE_BOOKMARK, params)

    def add_reference(self, image: Path) -> str:
        """Copy an image into the reference drop folder and return its project-relative path."""
        params = protocol.AddReferenceParams(project=self._open().name, image=image)
        return self._call(protocol.ADD_REFERENCE, params)

    def convert_export(self, export_path: str) -> ConvertedExport:
        """Return an export converted to `.glb` with its stats, converting it on a cache miss."""
        params = protocol.ExportParams(project=self._open().name, export=export_path)
        return self._call(protocol.CONVERT_EXPORT, params)

    # ####################### #
    #        REVISIONS        #
    # ####################### #

    def get_history(self, source_path: str) -> SourceHistory:
        """Return a source's revisions, newest first, with their standard renders and its restore mark."""
        return self._call(protocol.GET_HISTORY, self._source_params(source_path))

    def request_standard_renders(self, revision_id: int, export_path: str) -> None:
        """Render one export of a revision in the background, unless its renders exist or are on their way."""
        params = protocol.RevisionExportParams(project=self._open().name, revision_id=revision_id, export=export_path)
        self._call(protocol.REQUEST_RENDERS, params)

    def convert_revision_export(self, revision_id: int, export_path: str) -> ConvertedExport:
        """Return one of a revision's exports, as snapshotted, converted to `.glb` with its stats."""
        params = protocol.RevisionExportParams(project=self._open().name, revision_id=revision_id, export=export_path)
        return self._call(protocol.CONVERT_REVISION_EXPORT, params)

    def object_path(self, sha256: str) -> Path:
        """Return where a snapshotted file's bytes are on disk; the window reads them straight from there."""
        return ObjectStore(self._open().objects_dir).path_of(sha256)

    def restore_revision(self, source_path: str, revision_id: int) -> RestoreResult:
        """Write a whole earlier revision of a source back into the project, snapshotting the current state first."""
        params = protocol.RestoreParams(project=self._open().name, source=source_path, revision_id=revision_id)
        return self._call(protocol.RESTORE, params)

    # ####################### #
    #   WORKERS AND FEEDBACK  #
    # ####################### #

    def get_worker(self, source_path: str) -> WorkerSnapshot:
        """Return a source's worker state and its current session's chat."""
        return self._call(protocol.GET_WORKER, self._source_params(source_path))

    def send_prompt(self, source_path: str, prompt: str, feedback_id: int | None = None) -> ChatItem:
        """Start a worker turn on a source, optionally carrying a feedback."""
        params = protocol.PromptParams(
            project=self._open().name,
            source=source_path,
            prompt=prompt,
            feedback_id=feedback_id,
        )
        return self._call(protocol.SEND_PROMPT, params)

    def interrupt(self, source_path: str) -> None:
        """Stop the running turn on a source."""
        self._call(protocol.INTERRUPT, self._source_params(source_path))

    def new_session(self, source_path: str) -> None:
        """End a source's session; its next prompt opens a fresh one."""
        self._call(protocol.NEW_SESSION, self._source_params(source_path))

    def stop_worker(self, source_path: str) -> None:
        """Stop a source's Claude Code and Blender; the session resumes on the next prompt."""
        self._call(protocol.STOP_WORKER, self._source_params(source_path))

    def pin_references(self, source_path: str, reference_paths: list[str]) -> list[str]:
        """Pin references to a source's next prompt, replacing those pinned."""
        params = protocol.PinReferencesParams(
            project=self._open().name,
            source=source_path,
            references=reference_paths,
        )
        return self._call(protocol.PIN_REFERENCES, params)

    def create_feedback(self, request: FeedbackRequest) -> Feedback:
        """Store a marked-up capture as feedback on its source."""
        params = protocol.CreateFeedbackParams(project=self._open().name, request=request)
        return self._call(protocol.CREATE_FEEDBACK, params)

    def list_approvals(self) -> list[Approval]:
        """Return every project's pending approvals, oldest first."""
        return self._call(protocol.LIST_APPROVALS, protocol.ApprovalsParams())

    def answer_approval(self, approval_id: str, is_allowed: bool, message: str) -> Approval:
        """Allow or deny an escalated tool call."""
        params = protocol.AnswerApprovalParams(approval_id=approval_id, is_allowed=is_allowed, message=message)
        return self._call(protocol.ANSWER_APPROVAL, params)

    # -----------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------

    def _call[ParamsType: BaseModel, ResultType](
        self,
        method: protocol.RpcMethod[ParamsType, ResultType],
        params: ParamsType,
    ) -> ResultType:
        if self._client is None:
            raise DaemonUnavailableError("Not connected to the Atelier daemon")
        return self._client.call(method, params)

    def _open(self) -> OpenedProject:
        if self._opened is None:
            raise RuntimeError("No project is open")
        return self._opened

    def _project_params(self) -> protocol.ProjectParams:
        return protocol.ProjectParams(project=self._open().name)

    def _source_params(self, source_path: str) -> protocol.SourceParams:
        return protocol.SourceParams(project=self._open().name, source=source_path)

    def _deliver(self, project_name: str, event: CoreEvent) -> None:
        """Pass on the open project's events, and every project's approvals."""
        is_open_project = self._opened is not None and project_name == self._opened.name
        if self._on_event and (is_open_project or isinstance(event, ApprovalEvent)):
            self._on_event(event)

    def _on_disconnect(self) -> None:
        if self._on_event:
            self._on_event(DaemonDisconnected(socket_path=self._socket_path))
