"""Decision jobs: each review decision written by one `claude -p` run and posted by the daemon.

Claude Code is the print-mode stand-in in `fixtures/fake_claude.py`, told per ticket what to answer through
`$FAKE_CLAUDE_ANSWERS`; the tracker is `FakeTracker`. No test reaches a real Claude or a real tracker.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from conftest import FAKE_CLAUDE, RunningDaemon, wait_for
from fake_tracker import REVIEW_CONFIG, FakeTracker, add_review_scenario

from atelierd.catalog import Catalog
from atelierd.config import ConfigError, ReviewVerdict, load_config
from atelierd.project import Project, open_project
from atelierd.review import (
    Decision,
    DecisionJob,
    DecisionJobEvent,
    DecisionJobs,
    DecisionJobState,
    DecisionRequest,
    ReviewEvent,
    ReviewService,
)
from atelierd.review.writer import AnswerError, parse_answer
from atelierd.rpc import protocol

NOTES_FILE = "Saved/Review/direction-notes.md"
RULES_DOC = "docs/review-rules.md"
JOB_CONFIG = REVIEW_CONFIG.replace(
    "poll_minutes = 60\n",
    f'poll_minutes = 60\ndirection_notes = "{NOTES_FILE}"\nrules = ["{RULES_DOC}"]\n',
)
TAKE = "The barrel is too long. Overall, every weapon needs chunkier grips."
GRIP_NOTE = "every weapon needs chunkier grips"


# ####################### #
#        FIXTURES         #
# ####################### #


@pytest.fixture
def project(project_root: Path, tmp_path: Path) -> Project:
    """The fixture project with a review queue, a notes file and a rules doc configured, its state outside it."""
    config_path = project_root / "atelier.toml"
    config_path.write_text(config_path.read_text(encoding="utf-8") + JOB_CONFIG, encoding="utf-8")
    return open_project(project_root, state_dir=tmp_path / "state")


class Claude:
    """What the fake `claude` answers per ticket, and the prompts it was given."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._answers_path = tmp_path / "answers.json"
        self._prompts_path = tmp_path / "prompts.jsonl"
        self._log_path = tmp_path / "fake-claude.log"
        self.plan({})
        monkeypatch.setenv("FAKE_CLAUDE_ANSWERS", str(self._answers_path))
        monkeypatch.setenv("FAKE_CLAUDE_PROMPTS", str(self._prompts_path))
        monkeypatch.setenv("FAKE_CLAUDE_LOG", str(self._log_path))

    def plan(self, answers: dict[str, dict[str, Any]]) -> None:
        self._answers_path.write_text(json.dumps(answers), encoding="utf-8")

    def prompts(self) -> list[dict[str, str]]:
        if not self._prompts_path.is_file():
            return []
        return [json.loads(line) for line in self._prompts_path.read_text(encoding="utf-8").splitlines()]

    def argument_lists(self) -> list[list[str]]:
        return [json.loads(line) for line in self._log_path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Claude:
    return Claude(tmp_path, monkeypatch)


class Jobs:
    """A refreshed review queue and its decision jobs, with every job event heard."""

    def __init__(self, project: Project, tracker: FakeTracker) -> None:
        self.project = project
        self.tracker = tracker
        self.events: list[DecisionJob] = []
        catalog = Catalog(project)
        catalog.scan()
        self.review = ReviewService(project, catalog, tracker.opener, lambda event: None)
        self.review.refresh()
        self.jobs = self.open()

    def open(self) -> DecisionJobs:
        """A pool over the same state, as a restarted daemon would have."""

        def on_event(event: DecisionJobEvent) -> None:
            self.events.append(event.job)

        return DecisionJobs(self.project, self.review, str(FAKE_CLAUDE), on_event)

    def wait_until_settled(self, job_id: int) -> DecisionJob:
        return wait_for(
            lambda: self.jobs.get(job_id),
            lambda job: job.state in (DecisionJobState.POSTED, DecisionJobState.FAILED),
        )

    def states_of(self, job_id: int) -> list[DecisionJobState]:
        return [job.state for job in self.events if job.id == job_id]

    def tracker_writes(self, ticket: str) -> list[tuple[str, ...]]:
        return [write for write in self.tracker.writes if write[1] == ticket]


@pytest.fixture
def jobs(project: Project, fake_tracker: FakeTracker, claude: Claude) -> Any:
    add_review_scenario(fake_tracker)
    running = Jobs(project, fake_tracker)
    running.jobs.start()
    yield running
    running.jobs.stop()


@pytest.fixture
def drawing(tmp_path: Path) -> Path:
    drawing_path = tmp_path / "drawings" / "Barrel.PNG"
    drawing_path.parent.mkdir()
    drawing_path.write_bytes(b"a drawing")
    return drawing_path


def notes_lines(project: Project) -> list[str]:
    notes_path = project.root / NOTES_FILE
    return notes_path.read_text(encoding="utf-8").splitlines() if notes_path.is_file() else []


# ####################### #
#        DECISIONS        #
# ####################### #


class TestDecisions:
    def test_a_decision_is_written_by_claude_and_posted_by_the_daemon(
        self, jobs: Jobs, claude: Claude, drawing: Path, project: Project
    ) -> None:
        comment = "**For this piece**\n\nThe barrel is too long. ![](WAY-60-markup-1.png)"
        claude.plan(
            {
                "WAY-60": {
                    "answer": {
                        "comment": comment,
                        "direction_notes": [{"scope": "all weapons", "words": GRIP_NOTE}],
                    }
                }
            }
        )
        request = DecisionRequest(
            tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=(TAKE,), drawings={"WAY-60": (drawing,)}
        )
        [queued] = jobs.jobs.submit(request)
        assert queued.state is DecisionJobState.QUEUED
        assert not queued.decision.is_bulk

        job = jobs.wait_until_settled(queued.id)
        assert job.state is DecisionJobState.POSTED, job.error
        assert jobs.states_of(job.id) == [
            DecisionJobState.QUEUED,
            DecisionJobState.WRITING,
            DecisionJobState.POSTING,
            DecisionJobState.POSTED,
        ]
        # The daemon posted Claude's comment as it was, with the drawing under the name the comment uses.
        [posted_comment, attached, stage] = jobs.tracker_writes("WAY-60")
        assert posted_comment[0] == "comment" and posted_comment[3] == comment
        assert attached == ("attach", "WAY-60", posted_comment[2], "WAY-60-markup-1.png")
        assert stage == ("stage", "WAY-60", "Develop")
        assert job.posted_comment is not None and job.posted_comment.comment_id == posted_comment[2]
        assert "WAY-60" not in [item.ticket for item in jobs.review.queue().items]

        today = datetime.now().date().isoformat()
        assert notes_lines(project) == [f'- [ ] all weapons | "{GRIP_NOTE}" | WAY-60 | {today}']

    def test_the_job_runs_claude_in_the_project_with_no_way_to_post(
        self, jobs: Jobs, claude: Claude, drawing: Path, project: Project
    ) -> None:
        claude.plan({"WAY-60": {"answer": {"comment": "Shorter barrel. ![](WAY-60-markup-1.png)"}}})
        request = DecisionRequest(
            tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=(TAKE,), drawings={"WAY-60": (drawing,)}
        )
        [job] = jobs.jobs.submit(request)
        assert jobs.wait_until_settled(job.id).state is DecisionJobState.POSTED

        [run] = claude.prompts()
        assert run["cwd"] == str(project.root)
        prompt = run["prompt"]
        assert "Ticket: WAY-60" in prompt
        assert f"`{RULES_DOC}`" in prompt
        assert f"> {TAKE}" in prompt
        assert "`WAY-60-markup-1.png`" in prompt
        assert "changes requested. Atelier moves the ticket back to Develop." in prompt
        assert "Round 2" in prompt  # the latest delivery, as context
        [arguments] = [argument_list for argument_list in claude.argument_lists() if "--print" in argument_list]
        assert "--strict-mcp-config" in arguments and "--mcp-config" not in arguments
        assert "--tools=Read,Glob,Grep" in arguments
        assert arguments[arguments.index("--permission-prompts") + 1] == "none"
        assert arguments[arguments.index("--output-format") + 1] == "json"
        assert arguments[arguments.index("--add-dir") + 1] == str(project.state_dir)

    def test_a_bare_approval_posts_without_claude_and_applies_the_approve_rules(
        self, jobs: Jobs, claude: Claude
    ) -> None:
        [job] = jobs.jobs.submit(DecisionRequest(tickets=("WAY-70",), verdict=ReviewVerdict.APPROVE))
        job = jobs.wait_until_settled(job.id)
        assert job.state is DecisionJobState.POSTED, job.error
        assert claude.prompts() == []
        assert jobs.tracker_writes("WAY-70")[0][3] == "Approved."
        assert ("stage", "WAY-70", "Done") in jobs.tracker.writes
        assert ("stage", "WAY-71", "Backlog") in jobs.tracker.writes

    def test_a_bulk_decision_is_one_job_per_ticket_each_tailored(self, jobs: Jobs, claude: Claude) -> None:
        claude.plan(
            {
                "WAY-60": {"answer": {"comment": "Carbine: darker wood."}},
                "WAY-61": {"answer": {"comment": "Wall: darker wood."}},
            }
        )
        request = DecisionRequest(tickets=("WAY-60", "WAY-61"), verdict=ReviewVerdict.CHANGES, takes=("Darker wood.",))
        submitted = jobs.jobs.submit(request)
        assert [job.ticket for job in submitted] == ["WAY-60", "WAY-61"]
        assert all(job.decision.bulk_tickets == ("WAY-60", "WAY-61") for job in submitted)

        for job in submitted:
            assert jobs.wait_until_settled(job.id).state is DecisionJobState.POSTED
        assert jobs.tracker_writes("WAY-60")[0][3] == "Carbine: darker wood."
        assert jobs.tracker_writes("WAY-61")[0][3] == "Wall: darker wood."
        prompts = {run["ticket"]: run["prompt"] for run in claude.prompts()}
        assert "for 2 tickets at a time (this one and WAY-61)" in prompts["WAY-60"]
        assert "for 2 tickets at a time (this one and WAY-60)" in prompts["WAY-61"]

    def test_the_pool_runs_no_more_jobs_than_configured(self, jobs: Jobs, claude: Claude, project: Project) -> None:
        config_path = project.config_path
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                "poll_minutes = 60", "poll_minutes = 60\ndecision_jobs = 1"
            ),
            encoding="utf-8",
        )
        claude.plan({"WAY-60": {"sleep": 0.3}, "WAY-61": {"sleep": 0.3}})
        request = DecisionRequest(tickets=("WAY-60", "WAY-61"), verdict=ReviewVerdict.CHANGES, takes=("Darker.",))
        first, second = jobs.jobs.submit(request)
        for job in (first, second):
            assert jobs.wait_until_settled(job.id).state is DecisionJobState.POSTED

        order = [(job.id, job.state) for job in jobs.events]
        assert order.index((first.id, DecisionJobState.POSTED)) < order.index((second.id, DecisionJobState.WRITING))


# ####################### #
#        FAILURES         #
# ####################### #


class TestFailures:
    def test_a_malformed_answer_fails_with_its_output_kept_and_posts_nothing(self, jobs: Jobs, claude: Claude) -> None:
        claude.plan({"WAY-60": {"text": "Sure! Here's the comment you asked for."}})
        [job] = jobs.jobs.submit(DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=(TAKE,)))
        job = jobs.wait_until_settled(job.id)
        assert job.state is DecisionJobState.FAILED
        assert "not JSON" in job.error
        assert "Here's the comment" in job.raw_output
        assert jobs.tracker.writes == []

        # Never retried silently; a retry asked for runs Claude again.
        claude.plan({"WAY-60": {"text": '```json\n{"comment": "The barrel is too long.", "direction_notes": []}\n```'}})
        assert jobs.jobs.retry(job.id).state is DecisionJobState.QUEUED
        assert jobs.wait_until_settled(job.id).state is DecisionJobState.POSTED
        assert jobs.tracker_writes("WAY-60")[0][3] == "The barrel is too long."

    def test_an_answer_that_misquotes_the_reviewer_is_refused(
        self, jobs: Jobs, claude: Claude, project: Project
    ) -> None:
        answer = {"comment": "Shorter barrel.", "direction_notes": [{"scope": "all weapons", "words": "thicker grips"}]}
        claude.plan({"WAY-60": {"answer": answer}})
        [job] = jobs.jobs.submit(DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=(TAKE,)))
        job = jobs.wait_until_settled(job.id)
        assert job.state is DecisionJobState.FAILED
        assert "not the reviewer's words" in job.error
        assert jobs.tracker.writes == []
        assert notes_lines(project) == []

    def test_a_failed_run_fails_the_job(self, jobs: Jobs, claude: Claude) -> None:
        claude.plan({"WAY-60": {"error": "Credit balance is too low"}})
        [job] = jobs.jobs.submit(DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=(TAKE,)))
        job = jobs.wait_until_settled(job.id)
        assert job.state is DecisionJobState.FAILED
        assert "exited with 1" in job.error
        assert "Credit balance is too low" in job.raw_output

    def test_a_retry_after_the_comment_posted_never_posts_it_or_the_notes_again(
        self, jobs: Jobs, claude: Claude, drawing: Path, project: Project
    ) -> None:
        answer = {
            "comment": "Shorter barrel. ![](WAY-60-markup-1.png)",
            "direction_notes": [{"scope": "all weapons", "words": GRIP_NOTE}],
        }
        claude.plan({"WAY-60": {"answer": answer}})
        jobs.tracker.attach_error = "YouTrack answered 500"
        request = DecisionRequest(
            tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=(TAKE,), drawings={"WAY-60": (drawing,)}
        )
        [job] = jobs.jobs.submit(request)
        job = jobs.wait_until_settled(job.id)
        assert job.state is DecisionJobState.FAILED
        assert "500" in job.error
        assert job.posted_comment is not None
        # A new decision cannot take its place: the comment is already on the ticket.
        with pytest.raises(ValueError, match="retry it"):
            jobs.jobs.submit(DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.APPROVE))

        jobs.tracker.attach_error = None
        jobs.jobs.retry(job.id)
        assert jobs.wait_until_settled(job.id).state is DecisionJobState.POSTED
        assert [write[0] for write in jobs.tracker_writes("WAY-60")] == ["comment", "attach", "stage"]
        assert len(claude.prompts()) == 1
        assert len(notes_lines(project)) == 1

    def test_a_retry_after_the_drawings_attached_never_attaches_them_again(
        self, jobs: Jobs, claude: Claude, drawing: Path
    ) -> None:
        claude.plan({"WAY-60": {"answer": {"comment": "Shorter barrel. ![](WAY-60-markup-1.png)"}}})
        jobs.tracker.stage_error = "YouTrack answered 400 to POST /api/commands"
        request = DecisionRequest(
            tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=(TAKE,), drawings={"WAY-60": (drawing,)}
        )
        [job] = jobs.jobs.submit(request)
        assert jobs.wait_until_settled(job.id).state is DecisionJobState.FAILED

        jobs.tracker.stage_error = None
        jobs.jobs.retry(job.id)
        assert jobs.wait_until_settled(job.id).state is DecisionJobState.POSTED
        assert [write[0] for write in jobs.tracker_writes("WAY-60")] == ["comment", "attach", "stage"]

    def test_a_failed_job_is_replaced_by_a_new_decision(self, jobs: Jobs, claude: Claude) -> None:
        claude.plan({"WAY-60": {"text": "nonsense"}})
        [failed] = jobs.jobs.submit(DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=(TAKE,)))
        assert jobs.wait_until_settled(failed.id).state is DecisionJobState.FAILED

        [replacement] = jobs.jobs.submit(DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.APPROVE))
        assert jobs.wait_until_settled(replacement.id).state is DecisionJobState.POSTED
        assert [job.id for job in jobs.jobs.list_jobs()] == [replacement.id]


# ####################### #
#        REFUSALS         #
# ####################### #


class TestRefusals:
    def test_a_ticket_with_a_decision_on_its_way_refuses_another(self, jobs: Jobs, claude: Claude) -> None:
        claude.plan({"WAY-60": {"sleep": 1}})
        jobs.jobs.submit(DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=(TAKE,)))
        with pytest.raises(ValueError, match="already has a decision on its way"):
            jobs.jobs.submit(DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.APPROVE))

    def test_incomplete_decisions_are_refused(self, jobs: Jobs, drawing: Path) -> None:
        with pytest.raises(ValueError, match="needs the reviewer's words or a drawing"):
            jobs.jobs.submit(DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES))
        with pytest.raises(ValueError, match="does not cover"):
            jobs.jobs.submit(
                DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.APPROVE, drawings={"WAY-61": (drawing,)})
            )
        with pytest.raises(KeyError, match="not in the review queue"):
            jobs.jobs.submit(DecisionRequest(tickets=("WAY-99",), verdict=ReviewVerdict.APPROVE))
        with pytest.raises(FileNotFoundError):
            jobs.jobs.submit(
                DecisionRequest(
                    tickets=("WAY-60",), verdict=ReviewVerdict.APPROVE, drawings={"WAY-60": (drawing.parent / "x",)}
                )
            )
        with pytest.raises(ValueError, match="no words"):
            DecisionRequest(tickets=("WAY-60",), verdict=ReviewVerdict.CHANGES, takes=("  ",))
        assert jobs.jobs.list_jobs() == []

    def test_only_a_failed_job_is_retried(self, jobs: Jobs) -> None:
        [job] = jobs.jobs.submit(DecisionRequest(tickets=("WAY-61",), verdict=ReviewVerdict.APPROVE))
        assert jobs.wait_until_settled(job.id).state is DecisionJobState.POSTED
        with pytest.raises(ValueError, match="only a failed job is retried"):
            jobs.jobs.retry(job.id)
        with pytest.raises(KeyError):
            jobs.jobs.retry(9999)


# ####################### #
#        RESTARTS         #
# ####################### #


class TestRestarts:
    def test_jobs_survive_a_restart(self, project: Project, fake_tracker: FakeTracker, claude: Claude) -> None:
        add_review_scenario(fake_tracker)
        before = Jobs(project, fake_tracker)
        # Never started: the decisions sit in state.db as a stopped daemon leaves them.
        writing, posting, posted_already = before.jobs.submit(
            DecisionRequest(tickets=("WAY-60", "WAY-61", "WAY-70"), verdict=ReviewVerdict.CHANGES, takes=("Darker.",))
        )
        with sqlite3.connect(project.database_path) as connection:
            connection.execute("UPDATE decision_jobs SET state = 'writing' WHERE id = ?", (writing.id,))
            connection.execute("UPDATE decision_jobs SET state = 'posting' WHERE id = ?", (posting.id,))
            connection.execute(
                "UPDATE decision_jobs SET state = 'posting', posted_comment = ? WHERE id = ?",
                (
                    json.dumps({"ticket": "WAY-70", "comment_id": "4-1", "identity": "tracker-reviewer"}),
                    posted_already.id,
                ),
            )

        after = Jobs(project, fake_tracker)
        after.jobs.start()
        try:
            # Writing had reached nothing outside the daemon, so it simply runs again.
            assert after.wait_until_settled(writing.id).state is DecisionJobState.POSTED
            # Posting may have reached the tracker: it waits for the reviewer, saying why.
            unknown = after.jobs.get(posting.id)
            assert unknown.state is DecisionJobState.FAILED
            assert "may or may not be on WAY-61" in unknown.error
            known = after.jobs.get(posted_already.id)
            assert known.state is DecisionJobState.FAILED
            assert "without posting it again" in known.error

            after.jobs.retry(posted_already.id)
            assert after.wait_until_settled(posted_already.id).state is DecisionJobState.POSTED
            assert [write[0] for write in after.tracker_writes("WAY-70")] == ["stage"]
        finally:
            after.jobs.stop()


# ####################### #
#         ANSWERS         #
# ####################### #


def _envelope(answer: object, **extra: object) -> str:
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "structured_output": answer, **extra})


class TestAnswers:
    decision = Decision(ticket="WAY-60", verdict=ReviewVerdict.CHANGES, takes=(TAKE,))

    def test_an_answer_with_unknown_keys_is_refused(self) -> None:
        with pytest.raises(AnswerError, match="not the answer asked for"):
            parse_answer(_envelope({"comment": "Fine.", "direction_notes": [], "stage": "Done"}), self.decision)

    def test_a_direction_note_may_differ_from_the_take_only_in_spacing_and_case(self) -> None:
        note = {"scope": "all weapons", "words": "Every weapon  needs\nchunkier grips"}
        written = parse_answer(_envelope({"comment": "Shorter barrel.", "direction_notes": [note]}), self.decision)
        assert written.direction_notes[0].scope == "all weapons"

    def test_every_drawing_must_be_referenced(self) -> None:
        with_drawing = self.decision.model_copy(update={"drawings": (Path("/state/WAY-60-markup-1.png"),)})
        with pytest.raises(AnswerError, match="WAY-60-markup-1.png"):
            parse_answer(_envelope({"comment": "Shorter barrel."}), with_drawing)

    def test_a_failed_result_is_refused(self) -> None:
        failed = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True, "result": ""})
        with pytest.raises(AnswerError, match="error_max_turns"):
            parse_answer(failed, self.decision)


class TestConfig:
    @pytest.mark.parametrize(
        ("setting", "outside"),
        [
            (f'direction_notes = "{NOTES_FILE}"', 'direction_notes = "../outside.md"'),
            (f'rules = ["{RULES_DOC}"]', 'rules = ["/etc/passwd"]'),
        ],
    )
    def test_paths_must_stay_in_the_project(self, project: Project, setting: str, outside: str) -> None:
        config_path = project.config_path
        config_path.write_text(config_path.read_text(encoding="utf-8").replace(setting, outside), encoding="utf-8")
        with pytest.raises(ConfigError, match="must be a path inside the project"):
            load_config(config_path)


# ####################### #
#       FRONT DOOR        #
# ####################### #


def test_decisions_are_submitted_and_followed_over_the_socket(
    daemon: RunningDaemon,
    project_root: Path,
    fake_tracker: FakeTracker,
    claude: Claude,
) -> None:
    config_path = project_root / "atelier.toml"
    config_path.write_text(config_path.read_text(encoding="utf-8") + JOB_CONFIG, encoding="utf-8")
    add_review_scenario(fake_tracker)
    claude.plan({"WAY-60": {"text": "not an answer"}})
    job_events: list[DecisionJob] = []
    refreshed = threading.Event()

    def on_event(project: str, event: object) -> None:
        if isinstance(event, ReviewEvent):
            refreshed.set()
        if isinstance(event, DecisionJobEvent):
            job_events.append(event.job)

    client = daemon.client(on_event)
    client.call(protocol.SUBSCRIBE, protocol.SubscribeParams())
    project = client.call(protocol.OPEN_PROJECT, protocol.OpenProjectParams(reference=str(project_root))).name
    assert refreshed.wait(15)

    request = DecisionRequest(tickets=("WAY-60", "WAY-61"), verdict=ReviewVerdict.CHANGES, takes=("Darker.",))
    submitted = client.call(protocol.DECIDE, protocol.DecideParams(project=project, request=request))
    assert [job.ticket for job in submitted] == ["WAY-60", "WAY-61"]

    def jobs_by_ticket() -> dict[str, DecisionJob]:
        listed = client.call(protocol.LIST_DECISION_JOBS, protocol.ProjectParams(project=project))
        return {job.ticket: job for job in listed}

    settled = wait_for(
        jobs_by_ticket,
        lambda listed: {job.state for job in listed.values()} == {DecisionJobState.POSTED, DecisionJobState.FAILED},
    )
    assert settled["WAY-61"].state is DecisionJobState.POSTED
    failed = settled["WAY-60"]
    assert failed.raw_output and failed.error
    wait_for(lambda: [job.state for job in job_events if job.ticket == "WAY-60"], lambda states: len(states) == 3)
    assert [job.state for job in job_events if job.ticket == "WAY-60"] == [
        DecisionJobState.QUEUED,
        DecisionJobState.WRITING,
        DecisionJobState.FAILED,
    ]

    with pytest.raises(ValueError, match="only a failed job is retried"):
        client.call(
            protocol.RETRY_DECISION_JOB, protocol.DecisionJobParams(project=project, job_id=settled["WAY-61"].id)
        )
    claude.plan({})
    retried = client.call(protocol.RETRY_DECISION_JOB, protocol.DecisionJobParams(project=project, job_id=failed.id))
    assert retried.state is DecisionJobState.QUEUED
    assert wait_for(jobs_by_ticket, lambda listed: listed["WAY-60"].state is DecisionJobState.POSTED)
