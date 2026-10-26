"""Workers end to end through the daemon's socket: turns, the scope policy, approvals, sessions and revisions.

Claude Code is the stream-json stub in `fixtures/fake_claude.py`, which runs the worker's real hook and calls its real
permission-prompt tool over HTTP; each worker's Blender is a process that only listens (see `conftest.py`).
"""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import RunningDaemon

from atelierd.revisions import RevisionTrigger, SourceBusyError
from atelierd.rpc import protocol
from atelierd.rpc.client import RpcClient
from atelierd.workers import (
    Approval,
    ApprovalStatus,
    ChatItemKind,
    FeedbackRequest,
    Markup,
    Pin,
    WorkerBusyError,
    WorkerSnapshot,
    WorkerStatus,
)

CARBINE = "SourceArt/Weapons/Carbine/Carbine.blend"
DOG_TAG = "SourceArt/Weapons/Charms/DogTag/DogTag.blend"
# A 1x1 PNG, for a tool that returns an image.
_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/iZk9HQAAAABJRU5ErkJggg=="
)


def wait_for[Value](read: Callable[[], Value], is_done: Callable[[Value], bool], timeout_s: float = 15) -> Value:
    deadline = time.monotonic() + timeout_s
    value = read()
    while not is_done(value):
        if time.monotonic() > deadline:
            pytest.fail(f"Timed out waiting; last value: {value!r}")
        time.sleep(0.05)
        value = read()
    return value


class Session:
    """A client on one open project, with shortcuts for one source's worker."""

    def __init__(self, daemon: RunningDaemon, project_root: Path) -> None:
        self.daemon = daemon
        self.client: RpcClient = daemon.client()
        opened = self.client.call(protocol.OPEN_PROJECT, protocol.OpenProjectParams(reference=str(project_root)))
        self.project = opened.name
        self.root = opened.root
        self.state_dir = opened.state_dir

    def snapshot(self, source: str = CARBINE) -> WorkerSnapshot:
        return self.client.call(protocol.GET_WORKER, protocol.SourceParams(project=self.project, source=source))

    def prompt(self, text: str, source: str = CARBINE, feedback_id: int | None = None) -> None:
        params = protocol.PromptParams(project=self.project, source=source, prompt=text, feedback_id=feedback_id)
        self.client.call(protocol.SEND_PROMPT, params)

    def wait_idle(self, source: str = CARBINE, results: int = 1) -> WorkerSnapshot:
        def is_idle(snapshot: WorkerSnapshot) -> bool:
            finished = [item for item in snapshot.items if item.kind is ChatItemKind.RESULT]
            return len(finished) >= results and not snapshot.info.is_turn_running

        return wait_for(lambda: self.snapshot(source), is_idle)

    def pending_approvals(self) -> list[Approval]:
        return self.client.call(protocol.LIST_APPROVALS, protocol.ApprovalsParams(project=self.project))


@pytest.fixture
def session(daemon: RunningDaemon, project_root: Path) -> Session:
    return Session(daemon, project_root)


def claude_invocations(tmp_path: Path) -> list[list[str]]:
    log = tmp_path / "fake-claude.log"
    return [json.loads(line) for line in log.read_text().splitlines()] if log.is_file() else []


class TestTurns:
    def test_a_prompt_runs_a_turn_and_records_a_revision(self, session: Session, tmp_path: Path) -> None:
        session.prompt("SAY Hello from the worker")
        snapshot = session.wait_idle()

        kinds = [item.kind for item in snapshot.items]
        assert kinds == [ChatItemKind.NOTICE, ChatItemKind.USER, ChatItemKind.ASSISTANT, ChatItemKind.RESULT]
        assert snapshot.items[2].text == "Hello from the worker"
        assert snapshot.info.status is WorkerStatus.IDLE
        assert snapshot.sessions[0].session_id == snapshot.info.session_id
        assert "Source: SourceArt/Weapons/Carbine/Carbine.blend" in snapshot.items[1].detail

        history = session.client.call(
            protocol.GET_HISTORY, protocol.SourceParams(project=session.project, source=CARBINE)
        )
        assert history.revisions[0].trigger in (RevisionTrigger.BASELINE, RevisionTrigger.WORKER_TURN)

        [arguments] = claude_invocations(tmp_path)
        assert arguments[:7] == [
            "--print",
            "--verbose",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--mcp-config",
        ]
        assert "--strict-mcp-config" in arguments
        assert arguments[arguments.index("--permission-prompt-tool") + 1] == "mcp__atelier__approve"
        assert arguments[arguments.index("--session-id") + 1] == snapshot.info.session_id
        mcp_config = json.loads(Path(arguments[arguments.index("--mcp-config") + 1]).read_text())["mcpServers"]
        [(_, blender_port)] = session.daemon.blender_launches
        assert mcp_config["blender"]["env"]["BLENDER_MCP_PORT"] == str(blender_port)

    def test_a_turn_that_changes_files_records_a_worker_turn_revision(self, session: Session) -> None:
        readme = session.root / "SourceArt/Weapons/Carbine/README.md"
        session.prompt(f"WRITE {readme} A rewritten README")
        snapshot = session.wait_idle()

        assert readme.read_text() == "A rewritten README"
        result = snapshot.items[-1]
        assert result.revision_number is not None
        history = session.client.call(
            protocol.GET_HISTORY, protocol.SourceParams(project=session.project, source=CARBINE)
        )
        assert history.revisions[0].number == result.revision_number
        assert history.revisions[0].trigger is RevisionTrigger.WORKER_TURN
        assert not session.pending_approvals()

    def test_a_second_prompt_while_a_turn_runs_is_refused(self, session: Session) -> None:
        session.prompt("SLEEP 5")
        wait_for(session.snapshot, lambda snapshot: snapshot.info.status is WorkerStatus.RUNNING)
        with pytest.raises(WorkerBusyError):
            session.prompt("SAY too soon")
        session.client.call(protocol.INTERRUPT, protocol.SourceParams(project=session.project, source=CARBINE))
        session.wait_idle()

    def test_interrupt_ends_the_turn(self, session: Session) -> None:
        session.prompt("SLEEP 30")
        wait_for(session.snapshot, lambda snapshot: snapshot.info.status is WorkerStatus.RUNNING)
        session.client.call(protocol.INTERRUPT, protocol.SourceParams(project=session.project, source=CARBINE))
        snapshot = session.wait_idle()
        assert snapshot.items[-1].text.startswith("Interrupted")
        assert not snapshot.items[-1].is_error

    def test_restore_is_refused_while_a_turn_runs(self, session: Session) -> None:
        source = protocol.SourceParams(project=session.project, source=CARBINE)
        revision = wait_for(
            lambda: session.client.call(protocol.GET_HISTORY, source),
            lambda history: bool(history.revisions),
        ).revisions[0]
        session.prompt("SLEEP 5")
        wait_for(session.snapshot, lambda snapshot: snapshot.info.is_turn_running)
        with pytest.raises(SourceBusyError):
            session.client.call(
                protocol.RESTORE,
                protocol.RestoreParams(project=session.project, source=CARBINE, revision_id=revision.id),
            )
        session.client.call(protocol.INTERRUPT, source)
        session.wait_idle()

    def test_a_crash_mid_turn_fails_the_worker_and_still_ends_the_turn(self, session: Session) -> None:
        session.prompt("CRASH")
        snapshot = session.wait_idle()
        assert snapshot.info.status is WorkerStatus.FAILED
        assert snapshot.items[-1].is_error
        assert "crashed on purpose" in snapshot.items[-1].text

        # The next prompt starts Claude Code again, resuming the session.
        session.prompt("SAY back again")
        snapshot = session.wait_idle(results=2)
        assert snapshot.items[-2].text == "back again"


class TestSessions:
    def test_each_turn_shows_its_own_cost(self, session: Session) -> None:
        session.prompt("SAY one")
        session.wait_idle()
        session.prompt("SAY two")
        snapshot = session.wait_idle(results=2)
        results = [item.text for item in snapshot.items if item.kind is ChatItemKind.RESULT]
        assert [result.split(", ")[-1] for result in results] == ["$0.25", "$0.25"]

    def test_a_restarted_worker_resumes_its_session(self, session: Session, tmp_path: Path) -> None:
        session.prompt("SAY one")
        first = session.wait_idle()
        session.client.call(protocol.STOP_WORKER, protocol.SourceParams(project=session.project, source=CARBINE))
        session.prompt("SAY two")
        second = session.wait_idle(results=2)

        assert second.info.session_id == first.info.session_id
        first_run, second_run = claude_invocations(tmp_path)
        assert first_run[first_run.index("--session-id") + 1] == first.info.session_id
        assert second_run[second_run.index("--resume") + 1] == first.info.session_id

    def test_new_session_starts_a_fresh_chat(self, session: Session) -> None:
        session.prompt("SAY one")
        first = session.wait_idle()
        session.client.call(protocol.NEW_SESSION, protocol.SourceParams(project=session.project, source=CARBINE))
        assert session.snapshot().items == ()

        session.prompt("SAY two")
        second = session.wait_idle()
        assert second.info.session_id != first.info.session_id
        assert [item.text for item in second.items if item.kind is ChatItemKind.ASSISTANT] == ["two"]
        assert [info.session_id for info in second.sessions] == [second.info.session_id, first.info.session_id]

    def test_the_transcript_survives_the_daemon(self, session: Session, project_root: Path, tmp_path: Path) -> None:
        session.prompt("SAY remembered")
        before = session.wait_idle()
        session.daemon.stop()

        restarted = RunningDaemon(session.daemon.options)
        restarted.start()
        try:
            again = Session(restarted, project_root)
            after = again.snapshot()
            assert after.items == before.items
            assert after.info.status is WorkerStatus.STOPPED
        finally:
            restarted.stop()


class TestApprovals:
    def test_an_out_of_scope_write_waits_for_the_reviewer_and_runs_when_allowed(self, session: Session) -> None:
        shared_script = session.root / "SourceArt/_lib/run_asset.py"
        session.prompt(f"WRITE {shared_script} edited")
        [approval] = wait_for(session.pending_approvals, lambda approvals: len(approvals) == 1)

        assert approval.source == CARBINE
        assert approval.tool_name == "Write"
        assert "shared path" in approval.reason
        assert session.daemon.notifications[0].id == approval.id
        card = wait_for(session.snapshot, lambda snapshot: snapshot.items[-1].kind is ChatItemKind.APPROVAL).items[-1]
        assert card.approval_status is ApprovalStatus.PENDING
        assert shared_script.read_text() != "edited"

        answered = session.client.call(
            protocol.ANSWER_APPROVAL,
            protocol.AnswerApprovalParams(approval_id=approval.id, is_allowed=True),
        )
        assert answered.status is ApprovalStatus.ALLOWED
        snapshot = session.wait_idle()
        assert shared_script.read_text() == "edited"
        assert next(item for item in snapshot.items if item.approval_id).approval_status is ApprovalStatus.ALLOWED

    def test_a_denied_write_is_not_made_and_the_worker_hears_why(self, session: Session) -> None:
        outside = session.root / "Content" / "Weapons" / "SK_Carbine.uasset"
        session.prompt(f"WRITE {outside} nope")
        [approval] = wait_for(session.pending_approvals, lambda approvals: len(approvals) == 1)
        assert "Content/" in approval.reason
        session.client.call(
            protocol.ANSWER_APPROVAL,
            protocol.AnswerApprovalParams(approval_id=approval.id, is_allowed=False, message="Not in Content"),
        )
        snapshot = session.wait_idle()
        assert not outside.exists()
        tool_item = next(item for item in snapshot.items if item.kind is ChatItemKind.TOOL)
        assert tool_item.is_error
        assert "Not in Content" in tool_item.tool_output

    def test_interrupting_cancels_a_pending_approval(self, session: Session) -> None:
        session.prompt(f"WRITE {session.root / 'elsewhere.txt'} nope")
        wait_for(session.pending_approvals, lambda approvals: len(approvals) == 1)
        session.client.call(protocol.INTERRUPT, protocol.SourceParams(project=session.project, source=CARBINE))
        snapshot = session.wait_idle()
        assert session.pending_approvals() == []
        card = next(item for item in snapshot.items if item.kind is ChatItemKind.APPROVAL)
        assert card.approval_status is ApprovalStatus.CANCELLED
        assert not (session.root / "elsewhere.txt").exists()


class TestTwoWorkers:
    def test_two_sources_run_at_once_with_their_own_blender(self, session: Session) -> None:
        session.prompt("SLEEP 1\nSAY carbine", source=CARBINE)
        session.prompt("SLEEP 1\nSAY dog tag", source=DOG_TAG)
        wait_for(
            lambda: [session.snapshot(source).info.status for source in (CARBINE, DOG_TAG)],
            lambda statuses: statuses == [WorkerStatus.RUNNING, WorkerStatus.RUNNING],
        )
        carbine, dog_tag = session.wait_idle(CARBINE), session.wait_idle(DOG_TAG)

        assert carbine.info.blender_port != dog_tag.info.blender_port
        assert {path.name for path, _ in session.daemon.blender_launches} == {"Carbine.blend", "DogTag.blend"}
        assert carbine.items[-2].text == "carbine"
        assert dog_tag.items[-2].text == "dog tag"


class TestBrief:
    def test_a_restore_is_announced_to_the_next_turn_only(self, session: Session) -> None:
        source = protocol.SourceParams(project=session.project, source=CARBINE)
        first = wait_for(
            lambda: session.client.call(protocol.GET_HISTORY, source), lambda history: bool(history.revisions)
        )
        restore = protocol.RestoreParams(project=session.project, source=CARBINE, revision_id=first.revisions[0].id)
        session.client.call(protocol.RESTORE, restore)

        session.prompt("SAY after the restore")
        told = session.wait_idle()
        assert "restored this source to r1" in told.items[1].detail
        history = session.client.call(protocol.GET_HISTORY, source)
        assert history.restore_mark is None

        session.prompt("SAY later")
        later = session.wait_idle(results=2)
        assert "restored" not in later.items[-3].detail

    def test_pinned_references_ride_along_once(self, session: Session) -> None:
        pinned = session.client.call(
            protocol.PIN_REFERENCES,
            protocol.PinReferencesParams(
                project=session.project, source=CARBINE, references=["References/Palette/palette.png"]
            ),
        )
        assert pinned == ["References/Palette/palette.png"]
        session.prompt("SAY with references")
        first = session.wait_idle()
        assert "References/Palette/palette.png" in first.items[1].detail
        assert first.info.pinned_references == ()

        session.prompt("SAY without")
        second = session.wait_idle(results=2)
        assert "Pinned references" not in second.items[-3].detail

    def test_feedback_names_the_annotated_image_and_the_worker_reads_it(self, session: Session, tmp_path: Path) -> None:
        capture = tmp_path / "capture.png"
        annotated = tmp_path / "annotated.png"
        capture.write_bytes(_PIXEL_PNG)
        annotated.write_bytes(_PIXEL_PNG)
        request = FeedbackRequest(
            source=CARBINE,
            capture_path=capture,
            annotated_path=annotated,
            markup=Markup(pins=(Pin(number=1, x=0.5, y=0.5, note="Longer magazine"),)),
        )
        feedback = session.client.call(
            protocol.CREATE_FEEDBACK, protocol.CreateFeedbackParams(project=session.project, request=request)
        )
        assert feedback.annotated_path.read_bytes() == _PIXEL_PNG
        assert feedback.annotated_path.is_relative_to(
            session.state_dir / "feedback" / "SourceArt/Weapons/Carbine/Carbine"
        )
        assert (feedback.folder / "markup.json").is_file()

        session.prompt(f"READ {feedback.annotated_path}", feedback_id=feedback.id)
        snapshot = session.wait_idle()
        prompt_item = next(item for item in snapshot.items if item.kind is ChatItemKind.USER)
        assert prompt_item.feedback_id == feedback.id
        assert str(feedback.annotated_path) in prompt_item.detail
        assert "1: Longer magazine" in prompt_item.detail
        read_item = next(item for item in snapshot.items if item.kind is ChatItemKind.TOOL)
        assert read_item.tool_name == "Read"
        assert not read_item.is_error
        assert [image.read_bytes() for image in read_item.images] == [_PIXEL_PNG]

        listed = session.client.call(
            protocol.LIST_FEEDBACK, protocol.SourceParams(project=session.project, source=CARBINE)
        )
        assert [item.id for item in listed] == [feedback.id]
        assert listed[0].pins[0].note == "Longer magazine"
