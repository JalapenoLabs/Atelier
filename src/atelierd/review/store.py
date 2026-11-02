"""The decision jobs table in a project's `state.db`, so a decision survives a daemon restart.

The schema is migration 5 in `atelierd.catalog.store`. A job's decision, its queue item, its answer and its posted
comment are kept as JSON. Like the other stores, a `DecisionJobStore` holds one SQLite connection, which only the
thread that opened it may use, so every call site opens its own.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from atelierd.catalog.store import connect_state_database
from atelierd.review.models import Decision, DecisionJob, DecisionJobState, ReviewItem, WrittenDecision
from atelierd.trackers import PostedComment

logger = logging.getLogger(__name__)

_JOB_COLUMNS = "id, state, created_at, updated_at, decision, answer, are_notes_saved, posted_comment, error, raw_output"


class DecisionJobStore:
    """Read and write decision jobs in one project's `state.db`."""

    def __init__(self, database_path: Path) -> None:
        self._connection = connect_state_database(database_path)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection."""
        self._connection.close()

    # ####################### #
    #          READS          #
    # ####################### #

    def get(self, job_id: int) -> DecisionJob:
        """Return one job.

        Raises:
            KeyError: there is no such job.
        """
        row = self._connection.execute(f"SELECT {_JOB_COLUMNS} FROM decision_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"No decision job {job_id}")
        return _job_from_row(row)

    def list_jobs(self) -> list[DecisionJob]:
        """Return every job, newest first."""
        rows = self._connection.execute(f"SELECT {_JOB_COLUMNS} FROM decision_jobs ORDER BY id DESC").fetchall()
        return [_job_from_row(row) for row in rows]

    def latest_for_ticket(self, ticket: str) -> DecisionJob | None:
        """Return a ticket's newest job, if it has any."""
        row = self._connection.execute(
            f"SELECT {_JOB_COLUMNS} FROM decision_jobs WHERE ticket = ? ORDER BY id DESC LIMIT 1",
            (ticket,),
        ).fetchone()
        return _job_from_row(row) if row else None

    def queued_ids(self) -> list[int]:
        """Return the ids of the jobs waiting to run, oldest first."""
        rows = self._connection.execute(
            "SELECT id FROM decision_jobs WHERE state = ? ORDER BY id",
            (DecisionJobState.QUEUED.value,),
        ).fetchall()
        return [row["id"] for row in rows]

    def item(self, job_id: int) -> ReviewItem:
        """Return the queue item a job's ticket was when it was decided.

        Raises:
            KeyError: there is no such job.
        """
        row = self._connection.execute("SELECT item FROM decision_jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"No decision job {job_id}")
        return ReviewItem.model_validate_json(row["item"])

    # ####################### #
    #         WRITES          #
    # ####################### #

    def add_jobs(self, decisions: Sequence[tuple[Decision, ReviewItem]], replaced_ids: Sequence[int]) -> list[int]:
        """Queue a job for each decision, removing the failed jobs they replace, all at once; return the new ids."""
        now = datetime.now(UTC).isoformat()
        job_ids: list[int] = []
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.executemany(
                "DELETE FROM decision_jobs WHERE id = ?",
                [(job_id,) for job_id in replaced_ids],
            )
            for decision, item in decisions:
                cursor = self._connection.execute(
                    "INSERT INTO decision_jobs (ticket, state, created_at, updated_at, decision, item) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        decision.ticket,
                        DecisionJobState.QUEUED.value,
                        now,
                        now,
                        decision.model_dump_json(),
                        item.model_dump_json(),
                    ),
                )
                if cursor.lastrowid is None:
                    raise RuntimeError(f"Queueing a decision on {decision.ticket} returned no row id")
                job_ids.append(cursor.lastrowid)
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return job_ids

    def set_state(self, job_id: int, state: DecisionJobState, error: str = "", raw_output: str = "") -> DecisionJob:
        """Move a job to a state, with why it failed and what Claude printed when it did; return it."""
        self._update(job_id, "state = ?, error = ?, raw_output = ?", (state.value, error, raw_output))
        return self.get(job_id)

    def save_answer(self, job_id: int, answer: WrittenDecision) -> None:
        """Keep the comment and direction notes Claude wrote, so posting them never needs Claude again."""
        self._update(job_id, "answer = ?", (answer.model_dump_json(),))

    def mark_notes_saved(self, job_id: int) -> None:
        """Note that a job's direction notes are in the notes file, so they are never appended twice."""
        self._update(job_id, "are_notes_saved = 1", ())

    def save_posted_comment(self, job_id: int, posted: PostedComment) -> None:
        """Keep the comment a job put on its ticket, so finishing it later never posts it twice."""
        self._update(job_id, "posted_comment = ?", (posted.model_dump_json(),))

    def _update(self, job_id: int, assignments: str, parameters: tuple[object, ...]) -> None:
        cursor = self._connection.execute(
            f"UPDATE decision_jobs SET {assignments}, updated_at = ? WHERE id = ?",
            (*parameters, datetime.now(UTC).isoformat(), job_id),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"No decision job {job_id}")


def _job_from_row(row: sqlite3.Row) -> DecisionJob:
    return DecisionJob(
        id=row["id"],
        decision=Decision.model_validate_json(row["decision"]),
        state=DecisionJobState(row["state"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        answer=WrittenDecision.model_validate_json(row["answer"]) if row["answer"] else None,
        are_notes_saved=bool(row["are_notes_saved"]),
        posted_comment=PostedComment.model_validate_json(row["posted_comment"]) if row["posted_comment"] else None,
        error=row["error"],
        raw_output=row["raw_output"],
    )
