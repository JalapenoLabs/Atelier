"""Decision jobs: every review decision becomes a background job that writes the ticket's comment and posts it.

The reviewer never waits. `DecisionJobs.submit()` records a decision in `state.db` and returns at once; a pool of at
most `[review] decision_jobs` jobs (read afresh each time one starts) then takes each through:

- **writing**: one `claude -p` run in the project root writes the comment in the reviewer's voice and splits out the
  direction notes (`writer.py`). Its answer is checked strictly and kept, so nothing after it needs Claude again. An
  approval with no words and no drawings has nothing to voice and posts `Approved.` with no run.
- **posting**: the daemon, never Claude, posts the comment with the drawings and moves the stage through
  `ReviewService.apply_decision`. The comment is kept the moment it is on the ticket.
- **posted**, or **failed** with why. A failed job waits for `retry()`, which carries on from where it stopped: a kept
  answer is not written again, and a kept comment is not posted again. Nothing is ever retried silently.

Direction notes are appended to the `[review] direction_notes` file once the answer is in, whatever happens to the post.

Jobs survive the daemon: on `start()`, a job that was queued or still writing runs again (nothing of it reached the
tracker), and one that was posting fails with a reason, because whether its comment landed cannot be known.

Every state change is reported as a `DecisionJobEvent`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from atelierd.config import ConfigError, ReviewConfig, ReviewVerdict, load_config
from atelierd.constants import (
    BARE_APPROVAL_COMMENT,
    DECISION_JOB_TIMEOUT_S,
    DEFAULT_DECISION_JOBS,
    REVIEW_DECISIONS_DIR_NAME,
    REVIEW_DIR_NAME,
    WORKER_STOP_TIMEOUT_S,
)
from atelierd.project import Project
from atelierd.review.models import (
    Decision,
    DecisionJob,
    DecisionJobEvent,
    DecisionJobState,
    DecisionRequest,
    ReviewItem,
    WrittenDecision,
)
from atelierd.review.service import TICKET_ID_PATTERN, DecisionError, ReviewService
from atelierd.review.store import DecisionJobStore
from atelierd.review.writer import AnswerError, build_prompt, claude_command_line, parse_answer
from atelierd.workers.process import stop_process, worker_environment

logger = logging.getLogger(__name__)

# The jobs that may still change, and so block a new decision on their ticket.
_LIVE_STATES = frozenset({DecisionJobState.QUEUED, DecisionJobState.WRITING, DecisionJobState.POSTING})
# Characters of a failed run's stderr quoted in its error; the whole of it is kept in the raw output.
_STDERR_QUOTE_CHARS = 500


class _ClaudeRunError(Exception):
    """`claude -p` did not finish with an answer; `raw_output` is what it printed."""

    def __init__(self, message: str, raw_output: str) -> None:
        super().__init__(message)
        self.raw_output = raw_output


class DecisionJobs:
    """One project's decision jobs. Every public method is safe to call from any thread."""

    def __init__(
        self,
        project: Project,
        review: ReviewService,
        claude_command: str,
        emit: Callable[[DecisionJobEvent], None],
    ) -> None:
        self.project = project
        self._review = review
        self._claude_command = claude_command
        self._emit = emit
        # Guards the running jobs, their processes and threads, and whether the pool runs jobs at all.
        self._guard = threading.Lock()
        # Makes checking a ticket's jobs and queueing a new one a single step, so a ticket never gets two at once.
        self._submit_guard = threading.Lock()
        self._notes_guard = threading.Lock()
        self._running: set[int] = set()
        self._processes: dict[int, subprocess.Popen[str]] = {}
        self._threads: dict[int, threading.Thread] = {}
        # Off until `start()`, which picks up what a previous daemon left first, and off again from `stop()`.
        self._is_running = False

    # ####################### #
    #        LIFECYCLE        #
    # ####################### #

    def start(self) -> None:
        """Pick up the jobs a previous daemon left, then start the queued ones."""
        with DecisionJobStore(self.project.database_path) as store:
            for job in store.list_jobs():
                if job.state is DecisionJobState.WRITING:
                    # Nothing of it reached the tracker, so writing it again is the same attempt, not a retry.
                    logger.info(f"[Decisions] Job {job.id} on {job.ticket} was writing when the daemon stopped")
                    self._emit(DecisionJobEvent(job=store.set_state(job.id, DecisionJobState.QUEUED)))
                elif job.state is DecisionJobState.POSTING:
                    logger.warning(f"[Decisions] Job {job.id} on {job.ticket} was posting when the daemon stopped")
                    if job.posted_comment:
                        error = (
                            "Atelier stopped while posting this decision. Its comment is on the ticket; retrying "
                            "finishes the rest without posting it again."
                        )
                    else:
                        error = (
                            "Atelier stopped while posting this decision, so its comment may or may not be on "
                            f"{job.ticket}. Check the ticket before retrying, or the comment may be posted twice."
                        )
                    self._emit(DecisionJobEvent(job=store.set_state(job.id, DecisionJobState.FAILED, error=error)))
        with self._guard:
            self._is_running = True
        self._dispatch()

    def stop(self) -> None:
        """Start no more jobs and end the `claude -p` runs under way; they write again on the next start."""
        with self._guard:
            self._is_running = False
            processes = list(self._processes.values())
            threads = list(self._threads.values())
        for process in processes:
            stop_process(process, WORKER_STOP_TIMEOUT_S)
        for thread in threads:
            thread.join(timeout=WORKER_STOP_TIMEOUT_S)
            if thread.is_alive():
                logger.warning(f"[Decisions] {thread.name} of {self.project.name} did not stop in time")

    # ####################### #
    #          READS          #
    # ####################### #

    def list_jobs(self) -> list[DecisionJob]:
        """Return every job, newest first."""
        with DecisionJobStore(self.project.database_path) as store:
            return store.list_jobs()

    def get(self, job_id: int) -> DecisionJob:
        """Return one job.

        Raises:
            KeyError: there is no such job.
        """
        with DecisionJobStore(self.project.database_path) as store:
            return store.get(job_id)

    # ####################### #
    #     SUBMIT AND RETRY    #
    # ####################### #

    def submit(self, request: DecisionRequest) -> list[DecisionJob]:
        """Queue a decision, one job per ticket, and return the jobs at once.

        A ticket whose last job failed before posting anything takes the new decision in its place.

        Raises:
            ConfigError: atelier.toml cannot be read.
            ValueError: the project has no [review] table, a ticket already has a decision on its way, or the
                decision is incomplete (changes with neither words nor a drawing, drawings for a ticket not decided).
            KeyError: a ticket is not in the review queue.
            FileNotFoundError: a drawing is not a file.
        """
        if load_config(self.project.config_path).review is None:
            raise ValueError(f"{self.project.name} has no [review] table in atelier.toml")
        undecided = sorted(set(request.drawings) - set(request.tickets))
        if undecided:
            raise ValueError(f"Drawings were given for tickets the decision does not cover: {undecided}")
        if request.verdict is ReviewVerdict.CHANGES and not request.takes:
            wordless = [ticket for ticket in request.tickets if not request.drawings.get(ticket)]
            if wordless:
                raise ValueError(f"Requesting changes needs the reviewer's words or a drawing: {wordless} have none")
        items = [self._review.item(ticket) for ticket in request.tickets]
        for item in items:
            if not TICKET_ID_PATTERN.match(item.ticket):
                raise ValueError(f"'{item.ticket}' does not look like a ticket id")
        missing_drawings = [str(path) for paths in request.drawings.values() for path in paths if not path.is_file()]
        if missing_drawings:
            raise FileNotFoundError(f"These drawings are not files: {missing_drawings}")

        bulk_tickets = request.tickets if len(request.tickets) > 1 else ()
        with self._submit_guard, DecisionJobStore(self.project.database_path) as store:
            replaced: list[DecisionJob] = []
            for ticket in request.tickets:
                latest = store.latest_for_ticket(ticket)
                if latest is None or latest.state is DecisionJobState.POSTED:
                    continue
                if latest.state in _LIVE_STATES:
                    raise ValueError(f"{ticket} already has a decision on its way (job {latest.id}, {latest.state})")
                if latest.posted_comment:
                    raise ValueError(
                        f"{ticket}'s last decision (job {latest.id}) failed after its comment was posted; retry it"
                    )
                replaced.append(latest)

            decisions: list[tuple[Decision, ReviewItem]] = [
                (
                    Decision(
                        ticket=item.ticket,
                        verdict=request.verdict,
                        takes=request.takes,
                        drawings=self._copy_drawings(item.ticket, request.drawings.get(item.ticket, ())),
                        bulk_tickets=bulk_tickets,
                    ),
                    item,
                )
                for item in items
            ]
            job_ids = store.add_jobs(decisions, [job.id for job in replaced])
            jobs = [store.get(job_id) for job_id in job_ids]

        for replaced_job in replaced:
            logger.info(f"[Decisions] Job {replaced_job.id} on {replaced_job.ticket} is replaced by a new decision")
            for drawing_folder in {drawing.parent for drawing in replaced_job.decision.drawings}:
                shutil.rmtree(drawing_folder, ignore_errors=True)
        for job in jobs:
            logger.info(f"[Decisions] Queued job {job.id}: {job.ticket} {job.decision.verdict.value}")
            self._emit(DecisionJobEvent(job=job))
        self._dispatch()
        return jobs

    def retry(self, job_id: int) -> DecisionJob:
        """Queue a failed job again; it carries on from where it stopped.

        Raises:
            KeyError: there is no such job.
            ValueError: the job has not failed.
        """
        with self._submit_guard, DecisionJobStore(self.project.database_path) as store:
            job = store.get(job_id)
            if job.state is not DecisionJobState.FAILED:
                raise ValueError(f"Job {job_id} on {job.ticket} is {job.state}; only a failed job is retried")
            job = store.set_state(job_id, DecisionJobState.QUEUED)
        logger.info(f"[Decisions] Retrying job {job_id} on {job.ticket}")
        self._emit(DecisionJobEvent(job=job))
        self._dispatch()
        return job

    def _copy_drawings(self, ticket: str, drawings: Sequence[Path]) -> tuple[Path, ...]:
        """Copy a decision's drawings into a folder of the job's own, named as they are attached."""
        if not drawings:
            return ()
        folder = self.project.state_dir / REVIEW_DIR_NAME / ticket / REVIEW_DECISIONS_DIR_NAME / uuid.uuid4().hex
        folder.mkdir(parents=True)
        copies: list[Path] = []
        for number, drawing in enumerate(drawings, start=1):
            copy = folder / f"{ticket}-markup-{number}{drawing.suffix.lower()}"
            shutil.copyfile(drawing, copy)
            copies.append(copy)
        return tuple(copies)

    # ####################### #
    #         THE POOL        #
    # ####################### #

    def _dispatch(self) -> None:
        """Start queued jobs, oldest first, until the pool is full."""
        with self._guard:
            if not self._is_running:
                return
            free_slots = self._pool_size() - len(self._running)
            if free_slots <= 0:
                return
            with DecisionJobStore(self.project.database_path) as store:
                waiting = [job_id for job_id in store.queued_ids() if job_id not in self._running]
            for job_id in waiting[:free_slots]:
                self._running.add(job_id)
                thread = threading.Thread(target=self._run, args=(job_id,), name=f"decision-{job_id}", daemon=True)
                self._threads[job_id] = thread
                thread.start()

    def _pool_size(self) -> int:
        try:
            review_config = load_config(self.project.config_path).review
        except ConfigError as error:
            logger.warning(f"[Decisions] Cannot read the pool size of {self.project.name}: {error}")
            return DEFAULT_DECISION_JOBS
        return review_config.decision_jobs if review_config else DEFAULT_DECISION_JOBS

    def _run(self, job_id: int) -> None:
        try:
            with DecisionJobStore(self.project.database_path) as store:
                self._advance(store, job_id)
        except Exception as error:
            logger.exception(f"[Decisions] Job {job_id} failed unexpectedly: {error}")
            with DecisionJobStore(self.project.database_path) as store:
                failed = store.set_state(job_id, DecisionJobState.FAILED, error=f"{type(error).__name__}: {error}")
            self._emit(DecisionJobEvent(job=failed))
        finally:
            with self._guard:
                self._running.discard(job_id)
                self._threads.pop(job_id, None)
            self._dispatch()

    # ####################### #
    #         ONE JOB         #
    # ####################### #

    def _advance(self, store: DecisionJobStore, job_id: int) -> None:
        """Take one job from where it is to posted, or to failed."""
        job = store.get(job_id)
        try:
            review_config = load_config(self.project.config_path).review
        except ConfigError as error:
            self._fail(store, job_id, str(error))
            return
        if review_config is None:
            self._fail(store, job_id, f"{self.project.name} has no [review] table in atelier.toml")
            return

        answer = job.answer
        if answer is None:
            self._set_state(store, job_id, DecisionJobState.WRITING)
            answer = self._write(store, job, review_config)
            if answer is None:
                return
            store.save_answer(job_id, answer)
            job = store.get(job_id)

        if answer.direction_notes and not job.are_notes_saved:
            try:
                self._save_direction_notes(job.ticket, answer, review_config)
            except OSError as error:
                self._fail(store, job_id, f"Could not save the direction notes: {error}")
                return
            store.mark_notes_saved(job_id)

        self._set_state(store, job_id, DecisionJobState.POSTING)
        try:
            self._review.apply_decision(
                job.ticket,
                job.decision.verdict,
                answer.comment,
                job.decision.drawings,
                posted=job.posted_comment,
                on_posted=lambda posted: store.save_posted_comment(job_id, posted),
            )
        except DecisionError as error:
            if error.posted:
                store.save_posted_comment(job_id, error.posted)
            self._fail(store, job_id, str(error))
            return
        except (ConfigError, ValueError) as error:
            self._fail(store, job_id, str(error))
            return
        self._set_state(store, job_id, DecisionJobState.POSTED)
        logger.info(f"[Decisions] Job {job_id}: {job.ticket} posted")

    def _write(self, store: DecisionJobStore, job: DecisionJob, review_config: ReviewConfig) -> WrittenDecision | None:
        """Return the job's comment and direction notes, or None when it failed or the pool is stopping."""
        decision = job.decision
        if decision.verdict is ReviewVerdict.APPROVE and not decision.takes and not decision.drawings:
            logger.debug(f"[Decisions] Job {job.id}: a bare approval needs no writing")
            return WrittenDecision(comment=BARE_APPROVAL_COMMENT)

        prompt = build_prompt(decision, store.item(job.id), review_config)
        try:
            output = self._run_claude(job.id, prompt)
        except _ClaudeRunError as error:
            with self._guard:
                is_running = self._is_running
            if not is_running:
                # Left writing on purpose: the next start writes it again.
                logger.info(f"[Decisions] Job {job.id} stopped while writing; it resumes when the daemon starts")
                return None
            self._fail(store, job.id, str(error), raw_output=error.raw_output)
            return None
        try:
            return parse_answer(output, decision)
        except AnswerError as error:
            self._fail(store, job.id, str(error), raw_output=output)
            return None

    def _run_claude(self, job_id: int, prompt: str) -> str:
        """Run `claude -p` on a prompt in the project root and return what it printed.

        Raises:
            _ClaudeRunError: it could not start, ran too long, or exited with an error.
        """
        command = claude_command_line(self._claude_command, self.project)
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project.root,
                env=worker_environment(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as error:
            raise _ClaudeRunError(f"Could not run {self._claude_command}: {error}", "") from error
        with self._guard:
            self._processes[job_id] = process
            is_running = self._is_running
        if not is_running:
            stop_process(process, WORKER_STOP_TIMEOUT_S)
        logger.info(f"[Decisions] Job {job_id}: claude -p started (pid {process.pid})")
        try:
            stdout, stderr = process.communicate(prompt, timeout=DECISION_JOB_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            stop_process(process, WORKER_STOP_TIMEOUT_S)
            stdout, stderr = process.communicate()
            raise _ClaudeRunError(
                f"claude -p was still running after {DECISION_JOB_TIMEOUT_S // 60} minutes and was stopped",
                f"{stdout}\n{stderr}".strip(),
            ) from None
        finally:
            with self._guard:
                self._processes.pop(job_id, None)
        if process.returncode != 0:
            raise _ClaudeRunError(
                f"claude -p exited with {process.returncode}: {stderr.strip()[-_STDERR_QUOTE_CHARS:]}",
                f"{stdout}\n{stderr}".strip(),
            )
        return stdout

    def _save_direction_notes(self, ticket: str, answer: WrittenDecision, review_config: ReviewConfig) -> None:
        """Append a decision's direction notes to the notes file, one line each, or keep them on the job only."""
        if review_config.direction_notes is None:
            logger.info(f"[Decisions] {ticket} has direction notes, but [review] names no direction_notes file")
            return
        notes_path = self.project.root / review_config.direction_notes
        today = datetime.now().date().isoformat()
        # One line per note: any line breaks in the words would split it, so all whitespace becomes single spaces.
        lines = [
            f'- [ ] {" ".join(note.scope.split())} | "{" ".join(note.words.split())}" | {ticket} | {today}\n'
            for note in answer.direction_notes
        ]
        with self._notes_guard:
            notes_path.parent.mkdir(parents=True, exist_ok=True)
            with notes_path.open("a", encoding="utf-8") as notes_file:
                notes_file.writelines(lines)
        logger.info(f"[Decisions] Saved {len(lines)} direction notes from {ticket} to {notes_path}")

    # -----------------------------------------------------------------------------
    # States
    # -----------------------------------------------------------------------------

    def _set_state(self, store: DecisionJobStore, job_id: int, state: DecisionJobState) -> None:
        self._emit(DecisionJobEvent(job=store.set_state(job_id, state)))

    def _fail(self, store: DecisionJobStore, job_id: int, error: str, raw_output: str = "") -> None:
        logger.warning(f"[Decisions] Job {job_id} failed: {error}")
        self._emit(DecisionJobEvent(job=store.set_state(job_id, DecisionJobState.FAILED, error, raw_output)))
