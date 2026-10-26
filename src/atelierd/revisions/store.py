"""The revision tables in a project's `state.db`: revisions, their file manifests, file hashes and restore marks.

The schema is migration 3 in `atelierd.catalog.store`. Like `CatalogStore`, a `RevisionStore` holds one SQLite
connection, which only the thread that opened it may use, so every call site opens its own.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from atelierd.catalog.store import connect_state_database
from atelierd.revisions.models import FileKind, RestoreMark, Revision, RevisionFile, RevisionTrigger

logger = logging.getLogger(__name__)

_REVISION_COLUMNS = (
    "revisions.id, sources.path AS source, revisions.number, revisions.created_at, revisions.trigger, "
    "revisions.restored_from"
)
_REVISION_FROM = "revisions JOIN sources ON sources.id = revisions.source_id"


class RevisionStore:
    """Read and write revisions in one project's `state.db`."""

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
    #        REVISIONS        #
    # ####################### #

    def list_revisions(self, source_path: str) -> list[Revision]:
        """Return a source's revisions, newest first. A source with none, or unknown, has an empty list."""
        rows = self._connection.execute(
            f"SELECT {_REVISION_COLUMNS} FROM {_REVISION_FROM} WHERE sources.path = ? ORDER BY revisions.number DESC",
            (source_path,),
        ).fetchall()
        return self._revisions_from_rows(rows)

    def get_revision(self, revision_id: int) -> Revision:
        """Return one revision.

        Raises:
            KeyError: there is no such revision.
        """
        rows = self._connection.execute(
            f"SELECT {_REVISION_COLUMNS} FROM {_REVISION_FROM} WHERE revisions.id = ?",
            (revision_id,),
        ).fetchall()
        if not rows:
            raise KeyError(f"No revision {revision_id}")
        return self._revisions_from_rows(rows)[0]

    def latest_revision(self, source_path: str) -> Revision | None:
        """Return a source's newest revision, or None when it has none."""
        rows = self._connection.execute(
            f"SELECT {_REVISION_COLUMNS} FROM {_REVISION_FROM} WHERE sources.path = ? "
            "ORDER BY revisions.number DESC LIMIT 1",
            (source_path,),
        ).fetchall()
        if not rows:
            return None
        return self._revisions_from_rows(rows)[0]

    def insert_revision_if_changed(
        self,
        source_path: str,
        files: list[RevisionFile],
        trigger: RevisionTrigger,
        restored_from: int | None = None,
    ) -> Revision | None:
        """Record `files` as the source's next revision, unless they are exactly its newest revision's files.

        The comparison and the insert share one transaction, so two processes recording the same change (the window
        and `atelier scan --watch`) still record it once.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        manifest = {file.path: file.sha256 for file in files}
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            source_id = self._present_source_id(source_path)
            latest = self.latest_revision(source_path)
            if latest and latest.manifest == manifest:
                self._connection.execute("ROLLBACK")
                logger.debug(f"[Revisions] {source_path} is unchanged since r{latest.number}")
                return None

            number = latest.number + 1 if latest else 1
            cursor = self._connection.execute(
                "INSERT INTO revisions (source_id, number, created_at, trigger, restored_from) VALUES (?, ?, ?, ?, ?)",
                (source_id, number, datetime.now(UTC).isoformat(), trigger.value, restored_from),
            )
            if cursor.lastrowid is None:
                raise RuntimeError(f"Inserting a revision of '{source_path}' returned no row id")
            revision_id = cursor.lastrowid
            self._connection.executemany(
                "INSERT INTO revision_files (revision_id, path, kind, sha256, size) VALUES (?, ?, ?, ?, ?)",
                [(revision_id, file.path, file.kind.value, file.sha256, file.size) for file in files],
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        return self.get_revision(revision_id)

    def revisions_holding(self, source_path: str, file_path: str, sha256: str) -> list[int]:
        """Return the ids of a source's revisions in which `file_path` had exactly these bytes, newest first."""
        rows = self._connection.execute(
            f"SELECT revisions.id FROM {_REVISION_FROM} "
            "JOIN revision_files ON revision_files.revision_id = revisions.id "
            "WHERE sources.path = ? AND revision_files.path = ? AND revision_files.sha256 = ? "
            "ORDER BY revisions.number DESC",
            (source_path, file_path, sha256),
        ).fetchall()
        return [row["id"] for row in rows]

    # ####################### #
    #       FILE HASHES       #
    # ####################### #

    def cached_hash(self, path: str, size: int, mtime_ns: int) -> str | None:
        """Return the hash last recorded for a file, if it still has the size and modification time it had then."""
        row = self._connection.execute(
            "SELECT sha256 FROM file_hashes WHERE path = ? AND size = ? AND mtime_ns = ?",
            (path, size, mtime_ns),
        ).fetchone()
        if row is None:
            return None
        sha256: str = row["sha256"]
        return sha256

    def remember_hash(self, path: str, size: int, mtime_ns: int, sha256: str) -> None:
        """Record a file's hash with its size and modification time."""
        self._connection.execute(
            "INSERT OR REPLACE INTO file_hashes (path, size, mtime_ns, sha256) VALUES (?, ?, ?, ?)",
            (path, size, mtime_ns, sha256),
        )

    # ####################### #
    #      RESTORE MARKS      #
    # ####################### #

    def set_restore_mark(self, source_path: str, revision_id: int) -> None:
        """Mark a source as restored to a revision, replacing any earlier mark."""
        self._connection.execute(
            "INSERT OR REPLACE INTO restore_marks (source_id, revision_id, restored_at) VALUES (?, ?, ?)",
            (self._present_source_id(source_path), revision_id, datetime.now(UTC).isoformat()),
        )

    def get_restore_mark(self, source_path: str) -> RestoreMark | None:
        """Return a source's restore mark, or None when it has not been restored since its last worker turn."""
        row = self._connection.execute(
            "SELECT sources.path, restore_marks.revision_id, revisions.number, restore_marks.restored_at "
            "FROM restore_marks JOIN sources ON sources.id = restore_marks.source_id "
            "JOIN revisions ON revisions.id = restore_marks.revision_id WHERE sources.path = ?",
            (source_path,),
        ).fetchone()
        if row is None:
            return None
        return RestoreMark(
            source=row["path"],
            revision_id=row["revision_id"],
            revision_number=row["number"],
            restored_at=row["restored_at"],
        )

    def clear_restore_mark(self, source_path: str) -> None:
        """Remove a source's restore mark, if it has one."""
        self._connection.execute(
            "DELETE FROM restore_marks WHERE source_id IN (SELECT id FROM sources WHERE path = ?)",
            (source_path,),
        )

    # ####################### #
    #          PRUNE          #
    # ####################### #

    def revisions_beyond(self, keep: int) -> list[int]:
        """Return the ids of every revision older than each source's newest `keep`, except restore-marked ones."""
        rows = self._connection.execute(
            """
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY source_id ORDER BY number DESC) AS age
                FROM revisions
            )
            WHERE age > ? AND id NOT IN (SELECT revision_id FROM restore_marks)
            ORDER BY id
            """,
            (keep,),
        ).fetchall()
        return [row["id"] for row in rows]

    def delete_revisions(self, revision_ids: Iterable[int]) -> None:
        """Delete revisions and their manifests in one transaction."""
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.executemany(
                "DELETE FROM revisions WHERE id = ?", [(revision_id,) for revision_id in revision_ids]
            )
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

    def referenced_hashes(self) -> set[str]:
        """Return the hash of every object some remaining revision names."""
        rows = self._connection.execute("SELECT DISTINCT sha256 FROM revision_files").fetchall()
        return {row["sha256"] for row in rows}

    def revision_ids(self) -> set[int]:
        """Return the id of every remaining revision."""
        return {row["id"] for row in self._connection.execute("SELECT id FROM revisions")}

    # -----------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------

    def _present_source_id(self, path: str) -> int:
        row = self._connection.execute(
            "SELECT id FROM sources WHERE path = ? AND missing_since IS NULL",
            (path,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No present source '{path}'")
        source_id: int = row["id"]
        return source_id

    def _revisions_from_rows(self, rows: list[sqlite3.Row]) -> list[Revision]:
        """Build revisions from their rows, reading every manifest in one query."""
        if not rows:
            return []
        revision_ids = [row["id"] for row in rows]
        placeholders = ", ".join("?" for _ in revision_ids)
        files_by_revision: dict[int, list[RevisionFile]] = {}
        for file_row in self._connection.execute(
            f"SELECT revision_id, path, kind, sha256, size FROM revision_files "
            f"WHERE revision_id IN ({placeholders}) ORDER BY path",
            revision_ids,
        ):
            files_by_revision.setdefault(file_row["revision_id"], []).append(
                RevisionFile(
                    path=file_row["path"],
                    kind=FileKind(file_row["kind"]),
                    sha256=file_row["sha256"],
                    size=file_row["size"],
                )
            )

        return [
            Revision(
                id=row["id"],
                source=row["source"],
                number=row["number"],
                created_at=row["created_at"],
                trigger=RevisionTrigger(row["trigger"]),
                restored_from=row["restored_from"],
                files=tuple(files_by_revision.get(row["id"], [])),
            )
            for row in rows
        ]
