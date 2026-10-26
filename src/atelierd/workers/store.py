"""The worker tables in a project's `state.db`: sessions, feedback and pinned references.

The schema is migration 4 in `atelierd.catalog.store`. Like the other stores, a `WorkerStore` holds one SQLite
connection, which only the thread that opened it may use, so every call site opens its own.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from pydantic import BaseModel, ConfigDict, TypeAdapter

from atelierd.catalog.models import CameraBookmark
from atelierd.catalog.store import connect_state_database
from atelierd.workers.models import Pin, SessionInfo

logger = logging.getLogger(__name__)

_PINS = TypeAdapter(tuple[Pin, ...])


class StoredFeedback(BaseModel):
    """A `feedback` row: what `state.db` keeps of a feedback, before its folder's paths are attached."""

    model_config = ConfigDict(frozen=True)

    id: int
    source: str
    created_at: str
    pins: tuple[Pin, ...] = ()
    revision_id: int | None = None
    revision_number: int | None = None
    export: str | None = None
    camera: CameraBookmark | None = None


class WorkerStore:
    """Read and write sessions, feedback and pinned references in one project's `state.db`."""

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
    #        SESSIONS         #
    # ####################### #

    def list_sessions(self, source_path: str) -> list[SessionInfo]:
        """Return a source's sessions, newest first."""
        rows = self._connection.execute(
            "SELECT worker_sessions.*, sources.path AS source FROM worker_sessions "
            "JOIN sources ON sources.id = worker_sessions.source_id WHERE sources.path = ? "
            "ORDER BY worker_sessions.started_at DESC, worker_sessions.rowid DESC",
            (source_path,),
        ).fetchall()
        return [
            SessionInfo(
                session_id=row["session_id"],
                source=row["source"],
                started_at=row["started_at"],
                last_turn_at=row["last_turn_at"],
            )
            for row in rows
        ]

    def add_session(self, source_path: str, session_id: str) -> SessionInfo:
        """Record a new session on a source; it becomes the one the composer continues.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        started_at = datetime.now(UTC).isoformat()
        self._connection.execute(
            "INSERT INTO worker_sessions (session_id, source_id, started_at) VALUES (?, ?, ?)",
            (session_id, self._present_source_id(source_path), started_at),
        )
        return SessionInfo(session_id=session_id, source=source_path, started_at=started_at)

    def touch_session(self, session_id: str) -> None:
        """Note that a turn of a session ended now."""
        self._connection.execute(
            "UPDATE worker_sessions SET last_turn_at = ? WHERE session_id = ?",
            (datetime.now(UTC).isoformat(), session_id),
        )

    # ####################### #
    #   PINNED REFERENCES     #
    # ####################### #

    def pinned_references(self, source_path: str) -> list[str]:
        """Return the references pinned to a source's next prompt, sorted."""
        rows = self._connection.execute(
            "SELECT pinned_references.path FROM pinned_references "
            "JOIN sources ON sources.id = pinned_references.source_id WHERE sources.path = ? ORDER BY 1",
            (source_path,),
        ).fetchall()
        return [row["path"] for row in rows]

    def set_pinned_references(self, source_path: str, reference_paths: list[str]) -> None:
        """Replace the references pinned to a source's next prompt.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        source_id = self._present_source_id(source_path)
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute("DELETE FROM pinned_references WHERE source_id = ?", (source_id,))
            self._connection.executemany(
                "INSERT INTO pinned_references (source_id, path) VALUES (?, ?)",
                [(source_id, path) for path in sorted(set(reference_paths))],
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    # ####################### #
    #        FEEDBACK         #
    # ####################### #

    def insert_feedback(
        self,
        source_path: str,
        pins: tuple[Pin, ...],
        revision_id: int | None,
        export: str | None,
        camera: CameraBookmark | None,
    ) -> StoredFeedback:
        """Record a feedback and return it with its new id.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        created_at = datetime.now(UTC).isoformat()
        cursor = self._connection.execute(
            "INSERT INTO feedback (source_id, revision_id, export, created_at, pins, camera) VALUES (?, ?, ?, ?, ?, ?)",
            (
                self._present_source_id(source_path),
                revision_id,
                export,
                created_at,
                _PINS.dump_json(pins).decode(),
                camera.model_dump_json() if camera else None,
            ),
        )
        if cursor.lastrowid is None:
            raise RuntimeError(f"Inserting feedback on '{source_path}' returned no row id")
        return self.get_feedback(cursor.lastrowid)

    def get_feedback(self, feedback_id: int) -> StoredFeedback:
        """Return one feedback.

        Raises:
            KeyError: there is no such feedback.
        """
        rows = self._feedback_rows("feedback.id = ?", (feedback_id,))
        if not rows:
            raise KeyError(f"No feedback {feedback_id}")
        return rows[0]

    def list_feedback(self, source_path: str) -> list[StoredFeedback]:
        """Return a source's feedback, newest first."""
        return self._feedback_rows("sources.path = ?", (source_path,))

    # -----------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------

    def _feedback_rows(self, condition: str, parameters: tuple[object, ...]) -> list[StoredFeedback]:
        rows = self._connection.execute(
            "SELECT feedback.*, sources.path AS source, revisions.number AS revision_number FROM feedback "
            "JOIN sources ON sources.id = feedback.source_id "
            "LEFT JOIN revisions ON revisions.id = feedback.revision_id "
            f"WHERE {condition} ORDER BY feedback.id DESC",
            parameters,
        ).fetchall()
        return [
            StoredFeedback(
                id=row["id"],
                source=row["source"],
                created_at=row["created_at"],
                pins=_PINS.validate_json(row["pins"]),
                revision_id=row["revision_id"],
                revision_number=row["revision_number"],
                export=row["export"],
                camera=CameraBookmark.model_validate(json.loads(row["camera"])) if row["camera"] else None,
            )
            for row in rows
        ]

    def _present_source_id(self, path: str) -> int:
        row: sqlite3.Row | None = self._connection.execute(
            "SELECT id FROM sources WHERE path = ? AND missing_since IS NULL",
            (path,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No present source '{path}'")
        source_id: int = row["id"]
        return source_id
