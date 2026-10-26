"""Domain models of revisions: snapshots of a source's files, their standard renders, and restores.

File paths are project-relative and POSIX-style like the catalog's. Paths into Atelier's state folder (objects,
standard renders) are absolute, like the export cache's: the window reads those files straight from disk.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class RevisionTrigger(StrEnum):
    """Why a revision was recorded."""

    # The source's first revision, recorded when Atelier first opened the project with it.
    BASELINE = "baseline"
    # Its files changed and settled outside a worker turn: a manual edit, a rebuild from the terminal.
    CHANGE = "change"
    # A worker turn on it ended.
    WORKER_TURN = "worker-turn"
    # The state just before a restore, so the restore can be undone.
    PRE_RESTORE = "pre-restore"
    # The state a restore wrote.
    RESTORE = "restore"


class FileKind(StrEnum):
    """What part of a source a snapshotted file is."""

    BLEND = "blend"
    BUILDER = "builder"
    README = "readme"
    EXPORT = "export"
    RENDER = "render"


class RevisionFile(BaseModel):
    """One file of a revision: its project-relative path and the hash naming its bytes in the object store."""

    model_config = ConfigDict(frozen=True)

    path: str
    kind: FileKind
    sha256: str
    size: int


class Revision(BaseModel):
    """A manifest of one source's files at a moment."""

    model_config = ConfigDict(frozen=True)

    id: int = Field(description="Unique within the project; names the revision's standard render folder.")
    source: str = Field(description="The source's project-relative .blend path.")
    number: int = Field(description="1 for a source's first revision, counting up; shown as r1, r2, ...")
    created_at: str = Field(description="ISO 8601, UTC.")
    trigger: RevisionTrigger
    restored_from: int | None = Field(
        default=None,
        description="For a restore, the id of the revision whose files it wrote back.",
    )
    files: tuple[RevisionFile, ...] = Field(default=(), description="Sorted by path.")

    @property
    def manifest(self) -> dict[str, str]:
        """Every path mapped to its sha256: two revisions with equal manifests hold the same bytes."""
        return {file.path: file.sha256 for file in self.files}

    @property
    def exports(self) -> tuple[RevisionFile, ...]:
        """The revision's FBX exports."""
        return tuple(file for file in self.files if file.kind is FileKind.EXPORT)


class RenderState(StrEnum):
    """Where one export's standard renders stand in one revision."""

    DONE = "done"
    FAILED = "failed"
    PENDING = "pending"
    # Not rendered and not queued: a baseline revision's exports are rendered only when asked for.
    MISSING = "missing"


class StandardRenderSet(BaseModel):
    """One export's standard views in one revision."""

    model_config = ConfigDict(frozen=True)

    revision_id: int
    export: str = Field(description="The export's project-relative path.")
    export_sha256: str
    state: RenderState
    images: dict[str, Path] = Field(
        default_factory=dict,
        description="Each view's PNG, by view name (hero, front, side, top, clay), when the state is done.",
    )
    error: str = Field(default="", description="Why Blender could not render it, when the state is failed.")


class RestoreMark(BaseModel):
    """A source's restore, kept until the next worker turn on it has been told about it."""

    model_config = ConfigDict(frozen=True)

    source: str
    revision_id: int = Field(description="The revision the source was restored to.")
    revision_number: int
    restored_at: str = Field(description="ISO 8601, UTC.")


class RestoreResult(BaseModel):
    """What a restore did."""

    model_config = ConfigDict(frozen=True)

    pre_restore: Revision = Field(description="The state before the restore; restoring it undoes the restore.")
    restored: Revision = Field(description="The new revision recording the restored state.")
    written: tuple[str, ...] = Field(default=(), description="Files written back.")
    deleted: tuple[str, ...] = Field(default=(), description="Files the restored revision did not have.")
    skipped: tuple[str, ...] = Field(
        default=(),
        description="Files that differ but were left alone because they are shared with other sources.",
    )


class SourceHistory(BaseModel):
    """Everything the window shows about one source's revisions."""

    model_config = ConfigDict(frozen=True)

    source: str
    revisions: tuple[Revision, ...] = Field(default=(), description="Newest first.")
    standard_renders: tuple[StandardRenderSet, ...] = Field(
        default=(),
        description="Every revision's standard render sets, one per export.",
    )
    restore_mark: RestoreMark | None = None


class PruneResult(BaseModel):
    """What `atelier prune` removed."""

    model_config = ConfigDict(frozen=True)

    revisions_removed: int = 0
    objects_removed: int = 0
    bytes_freed: int = 0
    render_folders_removed: int = 0


class RevisionEventKind(StrEnum):
    """What a `RevisionEvent` reports."""

    RECORDED = "recorded"
    RENDERED = "rendered"


class RevisionEvent(BaseModel):
    """A new revision, or standard renders that finished (or failed) for one."""

    model_config = ConfigDict(frozen=True)

    kind: RevisionEventKind
    source: str
    revision_id: int
