"""The catalog's tables in a project's `state.db` (SQLite, WAL).

`state.db` is shared by every part of the daemon (catalog, revisions, feedback, sessions), so its schema is versioned
with `PRAGMA user_version` and grown by appending to `_MIGRATIONS`, never by editing an applied migration.

A source whose `.blend` disappears is marked missing rather than deleted. Its status, and in later versions its
revisions and feedback, survive a file that is briefly gone during a save or a move and comes back.

A `sqlite3.Connection` may only be used by the thread that opened it, so each thread opens its own `CatalogStore`.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from atelierd.catalog.models import (
    CameraBookmark,
    CatalogChange,
    DiscoveredSource,
    Reference,
    Source,
    SourceStatus,
)

logger = logging.getLogger(__name__)

_STATUS_VALUES = ", ".join(f"'{status.value}'" for status in SourceStatus)

# Each entry is one schema version: the statements that take the database from the previous version to it.
_MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        f"""
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            path TEXT NOT NULL UNIQUE,
            builder TEXT,
            readme TEXT,
            has_output_override INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT '{SourceStatus.DRAFT.value}' CHECK (status IN ({_STATUS_VALUES})),
            first_seen_at TEXT NOT NULL,
            missing_since TEXT
        )
        """,
        """
        CREATE TABLE source_outputs (
            source_id INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('export', 'render')),
            PRIMARY KEY (source_id, path)
        )
        """,
        """
        CREATE TABLE reference_images (
            path TEXT PRIMARY KEY,
            tag TEXT NOT NULL
        )
        """,
    ),
    (
        """
        CREATE TABLE camera_bookmarks (
            source_id INTEGER NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            target_x_cm REAL NOT NULL,
            target_y_cm REAL NOT NULL,
            target_z_cm REAL NOT NULL,
            yaw_degrees REAL NOT NULL,
            pitch_degrees REAL NOT NULL,
            distance_cm REAL NOT NULL,
            PRIMARY KEY (source_id, name)
        )
        """,
    ),
)

_SOURCE_COLUMNS = "id, path, builder, readme, has_output_override, status"


class CatalogStore:
    """Read and write the catalog in one project's `state.db`."""

    def __init__(self, database_path: Path) -> None:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit=True leaves every transaction to an explicit BEGIN IMMEDIATE ... COMMIT below.
        self._connection = sqlite3.connect(database_path, autocommit=True)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._migrate()

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

    def list_sources(self) -> list[Source]:
        """Return every present source, sorted by path."""
        rows = self._connection.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE missing_since IS NULL ORDER BY path"
        ).fetchall()
        outputs_by_source = self._outputs_by_source()
        return [self._source_from_row(row, outputs_by_source.get(row["id"], {})) for row in rows]

    def get_source(self, path: str) -> Source | None:
        """Return one present source, or None when the catalog has no such source."""
        row = self._connection.execute(
            f"SELECT {_SOURCE_COLUMNS} FROM sources WHERE path = ? AND missing_since IS NULL",
            (path,),
        ).fetchone()
        if row is None:
            logger.debug(f"[Store] No present source '{path}'")
            return None
        outputs_by_source = self._outputs_by_source(source_id=row["id"])
        return self._source_from_row(row, outputs_by_source.get(row["id"], {}))

    def list_references(self) -> list[Reference]:
        """Return every reference image, sorted by path."""
        rows = self._connection.execute("SELECT path, tag FROM reference_images ORDER BY path").fetchall()
        return [Reference(path=row["path"], tag=row["tag"]) for row in rows]

    def list_bookmarks(self, source_path: str) -> list[CameraBookmark]:
        """Return a present source's camera bookmarks, sorted by name.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        rows = self._connection.execute(
            "SELECT * FROM camera_bookmarks WHERE source_id = ? ORDER BY name",
            (self._present_source_id(source_path),),
        ).fetchall()
        return [
            CameraBookmark(
                name=row["name"],
                target_cm=(row["target_x_cm"], row["target_y_cm"], row["target_z_cm"]),
                yaw_degrees=row["yaw_degrees"],
                pitch_degrees=row["pitch_degrees"],
                distance_cm=row["distance_cm"],
            )
            for row in rows
        ]

    # ####################### #
    #         WRITES          #
    # ####################### #

    def save_bookmark(self, source_path: str, bookmark: CameraBookmark) -> None:
        """Save a camera bookmark on a present source, replacing any of the same name.

        Raises:
            KeyError: the catalog has no present source at `source_path`.
        """
        target_x, target_y, target_z = bookmark.target_cm
        self._connection.execute(
            "INSERT OR REPLACE INTO camera_bookmarks (source_id, name, target_x_cm, target_y_cm, target_z_cm, "
            "yaw_degrees, pitch_degrees, distance_cm) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._present_source_id(source_path),
                bookmark.name,
                target_x,
                target_y,
                target_z,
                bookmark.yaw_degrees,
                bookmark.pitch_degrees,
                bookmark.distance_cm,
            ),
        )

    def delete_bookmark(self, source_path: str, name: str) -> None:
        """Delete a source's camera bookmark.

        Raises:
            KeyError: the catalog has no present source at `source_path`, or it has no bookmark of that name.
        """
        cursor = self._connection.execute(
            "DELETE FROM camera_bookmarks WHERE source_id = ? AND name = ?",
            (self._present_source_id(source_path), name),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"Source '{source_path}' has no bookmark '{name}'")

    def set_status(self, path: str, status: SourceStatus) -> None:
        """Set a present source's review status.

        Raises:
            KeyError: the catalog has no present source at `path`.
        """
        cursor = self._connection.execute(
            "UPDATE sources SET status = ? WHERE path = ? AND missing_since IS NULL",
            (status.value, path),
        )
        if cursor.rowcount == 0:
            raise KeyError(f"No present source '{path}'")

    def apply_scan(self, discovered: list[DiscoveredSource], references: list[Reference]) -> CatalogChange:
        """Make the stored catalog match one scan, keeping every source's status, and report what changed."""
        now = datetime.now(UTC).isoformat()
        added: list[str] = []
        removed: list[str] = []
        changed: list[str] = []

        self._connection.execute("BEGIN IMMEDIATE")
        try:
            stored_rows = self._connection.execute(f"SELECT {_SOURCE_COLUMNS}, missing_since FROM sources").fetchall()
            stored_by_path = {row["path"]: row for row in stored_rows}
            outputs_by_source = self._outputs_by_source()

            for source in discovered:
                stored_row = stored_by_path.get(source.path)
                if stored_row is None:
                    self._insert_source(source, now)
                    added.append(source.path)
                    continue

                stored_source = self._source_from_row(stored_row, outputs_by_source.get(stored_row["id"], {}))
                is_reappearing = stored_row["missing_since"] is not None
                is_different = stored_source.model_dump(exclude={"status"}) != source.model_dump()
                if is_reappearing or is_different:
                    self._update_source(stored_row["id"], source)
                if is_reappearing:
                    added.append(source.path)
                elif is_different:
                    changed.append(source.path)

            discovered_paths = {source.path for source in discovered}
            for path, stored_row in stored_by_path.items():
                if path in discovered_paths or stored_row["missing_since"] is not None:
                    continue
                self._connection.execute("UPDATE sources SET missing_since = ? WHERE id = ?", (now, stored_row["id"]))
                removed.append(path)

            references_changed = self._replace_references(references)
            self._connection.execute("COMMIT")
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise

        change = CatalogChange(
            added=tuple(added),
            removed=tuple(sorted(removed)),
            changed=tuple(changed),
            references_changed=references_changed,
        )
        logger.debug(f"[Store] Applied scan: {change}")
        return change

    # -----------------------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------------------

    def _migrate(self) -> None:
        """Bring the schema up to the newest version, one migration per transaction."""
        current_version: int = self._connection.execute("PRAGMA user_version").fetchone()[0]
        if current_version > len(_MIGRATIONS):
            raise RuntimeError(
                f"state.db schema version {current_version} is newer than this Atelier ({len(_MIGRATIONS)})"
            )

        for version, statements in enumerate(_MIGRATIONS[current_version:], start=current_version + 1):
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                # executescript would COMMIT the open transaction first, so each statement runs on its own.
                for statement in statements:
                    self._connection.execute(statement)
                self._connection.execute(f"PRAGMA user_version = {version}")
                self._connection.execute("COMMIT")
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            logger.info(f"[Store] Migrated state.db to schema version {version}")

    def _present_source_id(self, path: str) -> int:
        row = self._connection.execute(
            "SELECT id FROM sources WHERE path = ? AND missing_since IS NULL",
            (path,),
        ).fetchone()
        if row is None:
            raise KeyError(f"No present source '{path}'")
        source_id: int = row["id"]
        return source_id

    def _outputs_by_source(self, source_id: int | None = None) -> dict[int, dict[str, list[str]]]:
        """Return each source's output paths grouped by kind, for one source or all of them."""
        query = "SELECT source_id, path, kind FROM source_outputs"
        parameters: tuple[int, ...] = ()
        if source_id is not None:
            query += " WHERE source_id = ?"
            parameters = (source_id,)

        outputs_by_source: dict[int, dict[str, list[str]]] = {}
        for row in self._connection.execute(f"{query} ORDER BY path", parameters):
            outputs_by_kind = outputs_by_source.setdefault(row["source_id"], {})
            outputs_by_kind.setdefault(row["kind"], []).append(row["path"])
        return outputs_by_source

    @staticmethod
    def _source_from_row(row: sqlite3.Row, outputs_by_kind: dict[str, list[str]]) -> Source:
        return Source(
            path=row["path"],
            builder=row["builder"],
            readme=row["readme"],
            exports=tuple(outputs_by_kind.get("export", [])),
            renders=tuple(outputs_by_kind.get("render", [])),
            has_output_override=bool(row["has_output_override"]),
            status=SourceStatus(row["status"]),
        )

    def _insert_source(self, source: DiscoveredSource, now: str) -> None:
        cursor = self._connection.execute(
            "INSERT INTO sources (path, builder, readme, has_output_override, first_seen_at) VALUES (?, ?, ?, ?, ?)",
            (source.path, source.builder, source.readme, source.has_output_override, now),
        )
        if cursor.lastrowid is None:
            raise RuntimeError(f"Inserting source '{source.path}' returned no row id")
        self._insert_outputs(cursor.lastrowid, source)

    def _update_source(self, source_id: int, source: DiscoveredSource) -> None:
        self._connection.execute(
            "UPDATE sources SET builder = ?, readme = ?, has_output_override = ?, missing_since = NULL WHERE id = ?",
            (source.builder, source.readme, source.has_output_override, source_id),
        )
        self._connection.execute("DELETE FROM source_outputs WHERE source_id = ?", (source_id,))
        self._insert_outputs(source_id, source)

    def _insert_outputs(self, source_id: int, source: DiscoveredSource) -> None:
        rows = [(source_id, path, "export") for path in source.exports]
        rows.extend((source_id, path, "render") for path in source.renders)
        self._connection.executemany("INSERT INTO source_outputs (source_id, path, kind) VALUES (?, ?, ?)", rows)

    def _replace_references(self, references: list[Reference]) -> bool:
        """Store exactly `references` and report whether that differs from what was stored."""
        stored_rows = self._connection.execute("SELECT path, tag FROM reference_images").fetchall()
        stored_references = {(row["path"], row["tag"]) for row in stored_rows}
        new_references = {(reference.path, reference.tag) for reference in references}
        if stored_references == new_references:
            return False

        self._connection.execute("DELETE FROM reference_images")
        self._connection.executemany("INSERT INTO reference_images (path, tag) VALUES (?, ?)", sorted(new_references))
        return True
