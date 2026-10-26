"""One source's worker: its Claude Code process, its background Blender, its turns, its approvals and its chat.

A turn goes like this:

1. `send_prompt` refuses while a turn runs (interrupt it first). Otherwise it builds the turn's brief, opens a session
   if the source has none, adds the prompt to the chat and returns; the turn runs on a thread of its own.
2. That thread starts whatever is not running: the background Blender (waiting until it serves MCP) and Claude Code,
   resuming the session with `--resume` or opening it with `--session-id`. It then writes the prompt to stdin.
3. Claude Code's events arrive on its reader thread and become chat items. A tool call the scope policy escalates
   arrives through the worker-side MCP as `request_approval`, and blocks there until the reviewer answers.
4. The turn's `result` event ends it: Atelier records a `worker-turn` revision of the source (renders follow in the
   background), adds the result to the chat, and only then marks the worker idle, so a restore can never interleave
   with a turn's last writes.

Claude Code stays running between turns (a long-lived process per active source); New session ends it, and the next
prompt starts a fresh one. If it dies, the turn ends the same way, with a revision of whatever it had written.
"""

from __future__ import annotations

import json
import logging
import shlex
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path, PurePosixPath
from subprocess import Popen
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from atelierd.blender import BlenderError
from atelierd.catalog.models import Source
from atelierd.catalog.store import CatalogStore
from atelierd.constants import (
    TRANSCRIPTS_DIR_NAME,
    WORKER_APPROVAL_TOOL,
    WORKER_BLENDER_SERVER_NAME,
    WORKER_BLENDER_START_TIMEOUT_S,
    WORKER_MCP_SERVER_NAME,
    WORKER_STOP_TIMEOUT_S,
    WORKER_TOKEN_HEADER,
    WORKERS_DIR_NAME,
)
from atelierd.project import Project
from atelierd.revisions import RevisionTracker, RevisionTrigger
from atelierd.workers.approvals import ApprovalBroker
from atelierd.workers.brief import build_brief
from atelierd.workers.models import (
    Approval,
    ApprovalStatus,
    ChatItem,
    ChatItemKind,
    Feedback,
    WorkerEvent,
    WorkerEventKind,
    WorkerInfo,
    WorkerSnapshot,
    WorkerStatus,
)
from atelierd.workers.policy import PolicyDecision, build_scope_policy, evaluate
from atelierd.workers.process import BlenderLauncher, ClaudeProcess, free_port, stop_process, wait_for_port
from atelierd.workers.store import WorkerStore
from atelierd.workers.transcript import Transcript

logger = logging.getLogger(__name__)


class WorkerBusyError(Exception):
    """A prompt or a new session was asked for while a turn is running."""


class WorkerSettings(BaseModel):
    """How the daemon runs workers: the commands, and where its worker-side MCP listens."""

    model_config = ConfigDict(frozen=True)

    claude_command: str = Field(description="The Claude Code CLI, by path.")
    blender_mcp_command: str = Field(description="The Blender MCP client each worker's `blender` server runs.")
    worker_mcp_url: str = Field(description="The worker-side MCP endpoint on the daemon's HTTP front door.")
    hook_python: Path = Field(description="The Python that runs the scope policy's hook: this daemon's own.")


class Worker:
    """The worker on one source of one project. Every public method is safe to call from any thread."""

    def __init__(
        self,
        project: Project,
        source_path: str,
        tracker: RevisionTracker,
        broker: ApprovalBroker,
        settings: WorkerSettings,
        launch_blender: BlenderLauncher,
        emit: Callable[[WorkerEvent], None],
    ) -> None:
        self.project = project
        self.source_path = source_path
        # Sent with every call the worker makes to the worker-side MCP, which is how that MCP knows who is asking.
        self.token = uuid.uuid4().hex
        self._tracker = tracker
        self._broker = broker
        self._settings = settings
        self._launch_blender = launch_blender
        self._emit = emit
        self._lock = threading.RLock()

        self._status = WorkerStatus.STOPPED
        self._error = ""
        self._claude: ClaudeProcess | None = None
        self._blender: Popen[bytes] | None = None
        self._blender_port: int | None = None
        self._is_interrupt_requested = False
        self._is_finishing = False

        with WorkerStore(project.database_path) as store:
            sessions = store.list_sessions(source_path)
            self._pinned_references = tuple(store.pinned_references(source_path))
        self._session_id: str | None = sessions[0].session_id if sessions else None
        # Whether Claude Code has stored the session, so the next process resumes it rather than opening it.
        self._is_session_stored = bool(sessions and sessions[0].last_turn_at)
        self._transcript = self._open_transcript(self._session_id) if self._session_id else None

        self._folder = project.state_dir / WORKERS_DIR_NAME / PurePosixPath(source_path).with_suffix("")

    # ####################### #
    #          READS          #
    # ####################### #

    def info(self) -> WorkerInfo:
        """Return the worker's state."""
        with self._lock:
            return WorkerInfo(
                source=self.source_path,
                status=self._status,
                session_id=self._session_id,
                blender_port=self._blender_port if self._blender else None,
                pending_approvals=len(self._broker_pending()),
                pinned_references=self._pinned_references,
                error=self._error,
            )

    def snapshot(self) -> WorkerSnapshot:
        """Return the worker's state with the current session's chat and its past sessions."""
        with WorkerStore(self.project.database_path) as store:
            sessions = store.list_sessions(self.source_path)
        with self._lock:
            items = self._transcript.items if self._transcript else ()
        return WorkerSnapshot(info=self.info(), items=items, sessions=tuple(sessions))

    @property
    def is_turn_running(self) -> bool:
        """Whether a turn is starting or running, which is when the source's files may be changing."""
        with self._lock:
            return self._status in (WorkerStatus.STARTING, WorkerStatus.RUNNING)

    # ####################### #
    #         ACTIONS         #
    # ####################### #

    def send_prompt(self, prompt: str, feedback: Feedback | None = None) -> ChatItem:
        """Start a turn with the reviewer's (or a manager's) prompt, and return the prompt's chat item.

        Raises:
            WorkerBusyError: a turn is already running.
            KeyError: the catalog no longer has the source.
            ValueError: the prompt is empty and carries no feedback.
        """
        text = prompt.strip()
        if not text and feedback is None:
            raise ValueError("A prompt needs some text or a feedback to send")
        text = text or "Address the feedback above."

        with self._lock:
            if self.is_turn_running:
                raise WorkerBusyError(f"{self.source_path} has a turn running; interrupt it or wait for it to end")
            source = self._present_source()
            pinned = list(self._pinned_references)
            if self._session_id is None:
                with WorkerStore(self.project.database_path) as store:
                    self._start_session(store)
            restore_mark = self._tracker.revisions.get_restore_mark(self.source_path)
            brief = build_brief(source, pinned, restore_mark, feedback)

            transcript = self._current_transcript()
            prompt_item = transcript.add(
                ChatItemKind.USER,
                text=text,
                detail=brief,
                feedback_id=feedback.id if feedback else None,
                images=(feedback.annotated_path,) if feedback else (),
            )
            self._is_interrupt_requested = False
            self._error = ""
            is_ready = bool(self._claude and self._claude.is_running and self._blender)
            self._status = WorkerStatus.RUNNING if is_ready else WorkerStatus.STARTING
            self._emit_item(prompt_item)
            self._emit_state()

        message = f"{brief}\n\n{text}"
        threading.Thread(
            target=self._run_turn,
            args=(message, bool(pinned), restore_mark is not None),
            name=f"turn-{PurePosixPath(self.source_path).stem}",
            daemon=True,
        ).start()
        return prompt_item

    def pin_references(self, reference_paths: list[str]) -> tuple[str, ...]:
        """Pin references to the next prompt, replacing those pinned, and return them sorted.

        Raises:
            KeyError: the source is not present.
        """
        with WorkerStore(self.project.database_path) as store:
            store.set_pinned_references(self.source_path, reference_paths)
            pinned = tuple(store.pinned_references(self.source_path))
        with self._lock:
            self._pinned_references = pinned
        logger.info(f"[Worker] {len(pinned)} references pinned to {self.source_path}")
        self._emit_state()
        return pinned

    def interrupt(self) -> None:
        """Stop the running turn. Its pending approvals are cancelled, and the turn ends as interrupted."""
        with self._lock:
            if not self.is_turn_running:
                logger.debug(f"[Worker] Interrupt on {self.source_path} with no turn running")
                return
            self._is_interrupt_requested = True
            claude = self._claude if self._status is WorkerStatus.RUNNING else None
        self._broker.cancel(self.project.name, self.source_path, "The reviewer interrupted the turn")
        if claude:
            try:
                claude.interrupt()
            except OSError as error:
                logger.warning(f"[Worker] Could not interrupt {self.source_path}: {error}")

    def new_session(self) -> None:
        """End the current session; the next prompt opens a fresh one. Blender keeps running.

        Raises:
            WorkerBusyError: a turn is running.
        """
        with self._lock:
            if self.is_turn_running:
                raise WorkerBusyError(f"{self.source_path} has a turn running; interrupt it before a new session")
            claude = self._claude
            self._claude = None
            self._session_id = None
            self._is_session_stored = False
            self._transcript = None
            if self._status is WorkerStatus.IDLE:
                self._status = WorkerStatus.IDLE if self._blender else WorkerStatus.STOPPED
        if claude:
            claude.stop()
        self._emit(WorkerEvent(kind=WorkerEventKind.RESET, source=self.source_path, info=self.info()))

    def stop(self) -> None:
        """Stop Claude Code and Blender, ending any running turn. The session stays; the next prompt resumes it."""
        with self._lock:
            self._is_interrupt_requested = True
            was_running = self.is_turn_running
            claude, blender = self._claude, self._blender
            self._claude = None
            self._blender = None
        self._broker.cancel(self.project.name, self.source_path, "The worker was stopped")
        if claude:
            claude.stop()
        if blender:
            stop_process(blender, WORKER_STOP_TIMEOUT_S)
        if was_running:
            self._finish_turn("Stopped", is_error=True, status=WorkerStatus.STOPPED)
            return
        with self._lock:
            self._status = WorkerStatus.STOPPED
        self._emit_state()

    def decide(self, tool_name: str, tool_input: dict[str, Any]) -> PolicyDecision:
        """Apply the scope policy to a call, as the worker's hook does."""
        try:
            policy = build_scope_policy(self.project, self._present_source())
        except KeyError as error:
            return PolicyDecision(is_allowed=False, reason=f"The source is no longer in the catalog ({error})")
        return evaluate(policy, tool_name, tool_input)

    def escalate(self, tool_name: str, tool_input: dict[str, Any], reason: str) -> Future[Approval]:
        """Raise an approval card for a call the scope policy did not allow, and return what resolves it.

        The future resolves to the reviewer's answer, or to a cancellation when the turn ends first. Called by the
        worker-side MCP's permission-prompt tool.
        """
        transcript = self._current_transcript()
        summary = _one_line(tool_input)
        approval, future = self._broker.request(
            self.project.name,
            self.source_path,
            tool_name,
            summary,
            tool_input,
            reason,
        )
        item = transcript.add(
            ChatItemKind.APPROVAL,
            text=reason,
            tool_name=tool_name,
            tool_summary=summary,
            approval_id=approval.id,
            approval_status=ApprovalStatus.PENDING,
        )
        self._emit_item(item)
        self._emit_state()

        def on_resolved(resolved: Future[Approval]) -> None:
            answer = resolved.result()
            self._emit_item(
                transcript.update(item.index, approval_status=answer.status, tool_output=answer.message),
            )
            self._emit_state()

        future.add_done_callback(on_resolved)
        return future

    # -----------------------------------------------------------------------------
    # The turn
    # -----------------------------------------------------------------------------

    def _run_turn(self, message: str, had_pinned: bool, had_restore_mark: bool) -> None:
        """Start the processes the turn needs, then send it. Runs on the turn's own thread."""
        try:
            self._ensure_blender()
            with self._lock:
                is_interrupted = self._is_interrupt_requested
            if not is_interrupted:
                claude = self._ensure_claude()
                with self._lock:
                    is_interrupted = self._is_interrupt_requested
                    if not is_interrupted:
                        claude.send_prompt(message)
                        self._status = WorkerStatus.RUNNING
            if is_interrupted:
                self._finish_turn("Interrupted before it started", is_error=False)
                return
            self._emit_state()
        except (OSError, BlenderError, RuntimeError, TimeoutError) as error:
            logger.error(f"[Worker] Could not start the turn on {self.source_path}: {error}")
            with self._lock:
                self._error = str(error)
            self._finish_turn(f"Could not start the turn: {error}", is_error=True, status=WorkerStatus.FAILED)
            return

        # The turn now carries them, so the next one does not repeat them.
        if had_pinned:
            self.pin_references([])
        if had_restore_mark:
            self._tracker.revisions.clear_restore_mark(self.source_path)

    def _ensure_blender(self) -> None:
        with self._lock:
            if self._blender and self._blender.poll() is None:
                return
            if self._blender_port is None:
                self._blender_port = free_port()
            port = self._blender_port
            blender = self._launch_blender(self.project.root / self.source_path, port)
            self._blender = blender
        try:
            wait_for_port(port, blender, WORKER_BLENDER_START_TIMEOUT_S)
        except RuntimeError, TimeoutError:
            stop_process(blender, WORKER_STOP_TIMEOUT_S)
            with self._lock:
                self._blender = None
            raise

    def _ensure_claude(self) -> ClaudeProcess:
        with self._lock:
            if self._claude and self._claude.is_running:
                return self._claude
            session_id = self._session_id
            if session_id is None:
                raise RuntimeError(f"{self.source_path} has no session to run")
            command = self._claude_command(session_id)
            logger.info(f"[Worker] {self.source_path}: {shlex.join(command)}")
            claude: ClaudeProcess | None = None

            def on_exit(return_code: int, stderr_tail: str) -> None:
                if claude is not None:
                    self._on_claude_exit(claude, return_code, stderr_tail)

            claude = ClaudeProcess(command, self.project.root, self._on_claude_event, on_exit)
            self._claude = claude
            return claude

    def _on_claude_event(self, event: dict[str, Any]) -> None:
        """Handle one stream-json event from Claude Code. Runs on its reader thread."""
        event_type = event.get("type")
        if event_type == "system" and event.get("subtype") == "init":
            self._on_session_started(str(event.get("session_id", "")))
            return
        if event_type == "result":
            self._on_result(event)
            return
        transcript = self._current_transcript()
        for item in transcript.apply_event(event):
            self._emit_item(item)

    def _on_session_started(self, session_id: str) -> None:
        with self._lock:
            if session_id and session_id != self._session_id:
                logger.warning(f"[Worker] Claude Code reports session {session_id}, not {self._session_id}")
            if self._is_session_stored:
                return
            self._is_session_stored = True
            current_session = self._session_id
        if current_session:
            with WorkerStore(self.project.database_path) as store:
                store.touch_session(current_session)

    def _on_result(self, event: dict[str, Any]) -> None:
        subtype = str(event.get("subtype", ""))
        is_error = bool(event.get("is_error")) or subtype != "success"
        with self._lock:
            was_interrupted = self._is_interrupt_requested
        seconds = float(event.get("duration_ms") or 0) / 1000
        cost = float(event.get("total_cost_usd") or 0)
        if was_interrupted:
            summary = "Interrupted"
        elif is_error:
            summary = f"Ended with an error ({subtype or 'unknown'})"
        else:
            summary = "Done"
        summary += f" after {seconds:.0f} s" + (f", ${cost:.2f}" if cost else "")
        self._finish_turn(summary, is_error=is_error and not was_interrupted)

    def _on_claude_exit(self, claude: ClaudeProcess, return_code: int, stderr_tail: str) -> None:
        """Handle Claude Code exiting. Stop and New session detach the process first, so its exit is expected."""
        with self._lock:
            is_current = self._claude is claude
            if not is_current:
                return
            self._claude = None
            was_running = self.is_turn_running
            last_line = stderr_tail.splitlines()[-1] if stderr_tail.strip() else f"exited with {return_code}"
            self._error = last_line
        logger.warning(f"[Worker] Claude Code on {self.source_path} exited by itself ({return_code}): {last_line}")
        if was_running:
            self._finish_turn(
                f"Claude Code exited during the turn: {last_line}", is_error=True, status=WorkerStatus.FAILED
            )
            return
        with self._lock:
            self._status = WorkerStatus.FAILED
        self._emit_state()

    def _finish_turn(self, summary: str, is_error: bool, status: WorkerStatus = WorkerStatus.IDLE) -> None:
        """Record the turn's revision, add its result to the chat, then mark the worker `status`.

        A turn can end from more than one side at once (its result arriving as the worker is stopped); the first to
        get here ends it, and the rest find no turn running.
        """
        with self._lock:
            if not self.is_turn_running or self._is_finishing:
                logger.debug(f"[Worker] The turn on {self.source_path} has already ended")
                return
            self._is_finishing = True
        self._broker.cancel(self.project.name, self.source_path, "The turn ended")
        revision_number: int | None = None
        try:
            revision = self._tracker.record(self.source_path, RevisionTrigger.WORKER_TURN)
            revision_number = revision.number if revision else None
        except (KeyError, OSError) as error:
            logger.error(f"[Worker] Could not record a revision after the turn on {self.source_path}: {error}")
            summary += f" (no revision recorded: {error})"

        transcript = self._current_transcript()
        result_item = transcript.add(
            ChatItemKind.RESULT,
            text=summary if revision_number is None else f"{summary}. Recorded r{revision_number}",
            is_error=is_error,
            revision_number=revision_number,
        )
        with WorkerStore(self.project.database_path) as store:
            store.touch_session(transcript.session_id)
        with self._lock:
            has_processes = bool(self._claude and self._claude.is_running)
            if status is WorkerStatus.IDLE and not has_processes:
                status = WorkerStatus.STOPPED
            self._status = status
            self._is_interrupt_requested = False
            self._is_finishing = False
        self._emit_item(result_item)
        self._emit_state()

    # -----------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------

    def _claude_command(self, session_id: str) -> list[str]:
        """The `claude` command line for this worker, writing the config files it names first."""
        self._folder.mkdir(parents=True, exist_ok=True)
        policy_path = self._folder / "policy.json"
        mcp_config_path = self._folder / "mcp.json"
        settings_path = self._folder / "settings.json"

        policy = build_scope_policy(self.project, self._present_source())
        policy_path.write_text(policy.model_dump_json(indent=2), encoding="utf-8")
        mcp_config = {
            "mcpServers": {
                WORKER_BLENDER_SERVER_NAME: {
                    "type": "stdio",
                    "command": self._settings.blender_mcp_command,
                    "args": [],
                    "env": {"BLENDER_MCP_HOST": "127.0.0.1", "BLENDER_MCP_PORT": str(self._blender_port)},
                },
                WORKER_MCP_SERVER_NAME: {
                    "type": "http",
                    "url": self._settings.worker_mcp_url,
                    "headers": {WORKER_TOKEN_HEADER: self.token},
                },
            }
        }
        mcp_config_path.write_text(json.dumps(mcp_config, indent=2), encoding="utf-8")
        hook_command = shlex.join([str(self._settings.hook_python), "-m", "atelierd.workers.hook", str(policy_path)])
        settings = {
            "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": hook_command}]}]}
        }
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        # The secret token is in mcp.json, so only the user can read these files.
        for config_path in (policy_path, mcp_config_path, settings_path):
            config_path.chmod(0o600)

        command = [
            self._settings.claude_command,
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--mcp-config",
            str(mcp_config_path),
            "--strict-mcp-config",
            "--permission-prompt-tool",
            f"mcp__{WORKER_MCP_SERVER_NAME}__{WORKER_APPROVAL_TOOL}",
            "--settings",
            str(settings_path),
        ]
        if not self.project.is_state_in_project:
            # Feedback images and transcripts live in the state folder; let the file tools reach it.
            command.extend(["--add-dir", str(self.project.state_dir)])
        if self._is_session_stored:
            command.extend(["--resume", session_id])
        else:
            command.extend(["--session-id", session_id])
        return command

    def _start_session(self, store: WorkerStore) -> None:
        """Open a new session; called holding the lock."""
        session = store.add_session(self.source_path, str(uuid.uuid4()))
        self._session_id = session.session_id
        self._is_session_stored = False
        self._transcript = self._open_transcript(session.session_id)
        self._emit(WorkerEvent(kind=WorkerEventKind.RESET, source=self.source_path, info=self.info()))
        self._emit_item(self._transcript.add(ChatItemKind.NOTICE, text="New session"))

    def _current_transcript(self) -> Transcript:
        with self._lock:
            if self._transcript is None:
                raise RuntimeError(f"{self.source_path} has no session open")
            return self._transcript

    def _open_transcript(self, session_id: str) -> Transcript:
        return Transcript(self.project.state_dir / TRANSCRIPTS_DIR_NAME, session_id, self.project.root)

    def _present_source(self) -> Source:
        with CatalogStore(self.project.database_path) as store:
            source = store.get_source(self.source_path)
        if source is None:
            raise KeyError(f"No present source '{self.source_path}'")
        return source

    def _broker_pending(self) -> list[Approval]:
        return [
            approval for approval in self._broker.list_pending(self.project.name) if approval.source == self.source_path
        ]

    def _emit_item(self, item: ChatItem) -> None:
        self._emit(WorkerEvent(kind=WorkerEventKind.ITEM, source=self.source_path, info=self.info(), item=item))

    def _emit_state(self) -> None:
        self._emit(WorkerEvent(kind=WorkerEventKind.STATE, source=self.source_path, info=self.info()))


def _one_line(tool_input: dict[str, Any]) -> str:
    """What an escalated call does, for its card: its command, file or URL, else its input."""
    for argument in ("command", "file_path", "notebook_path", "url", "query", "path"):
        if tool_input.get(argument):
            return str(tool_input[argument]).splitlines()[0][:300]
    return json.dumps(tool_input)[:300]
