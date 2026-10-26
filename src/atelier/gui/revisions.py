"""Building what the filmstrip, the standard render strip and compare show from a source's history.

Plain functions over core models, like `tree.py` and `gallery.py`, so they are tested without Qt.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from atelierd.revisions import RenderState, Revision, RevisionTrigger, SourceHistory, StandardRenderSet

# How the filmstrip names why each revision was recorded.
TRIGGER_LABELS = {
    RevisionTrigger.BASELINE: "First seen",
    RevisionTrigger.CHANGE: "Files changed",
    RevisionTrigger.WORKER_TURN: "Worker turn",
    RevisionTrigger.PRE_RESTORE: "Before restore",
    RevisionTrigger.RESTORE: "Restore",
}

# What a standard render tile says when it has no image.
RENDER_STATE_LABELS = {
    RenderState.DONE: "",
    RenderState.FAILED: "Render failed",
    RenderState.PENDING: "Rendering…",
    RenderState.MISSING: "Not rendered",
}


class RevisionRow(BaseModel):
    """One revision in the filmstrip."""

    model_config = ConfigDict(frozen=True)

    id: int
    label: str
    when: str
    trigger: str
    note: str
    thumbnail: str
    is_staged: bool
    is_base: bool


def revision_label(revision: Revision) -> str:
    """The short name a revision goes by, `r3`."""
    return f"r{revision.number}"


def find_render_set(history: SourceHistory, revision_id: int, export_path: str) -> StandardRenderSet | None:
    """Return one export's standard render set in one revision, if the revision has that export."""
    return next(
        (
            render
            for render in history.standard_renders
            if render.revision_id == revision_id and render.export == export_path
        ),
        None,
    )


def build_revision_rows(
    history: SourceHistory,
    export_path: str,
    staged_id: int,
    base_id: int,
) -> list[RevisionRow]:
    """Return the filmstrip's rows, newest first, each with the hero render of `export_path` as its thumbnail.

    Args:
        history: the source's history.
        export_path: the export whose hero render is each revision's thumbnail.
        staged_id: the revision the stage shows.
        base_id: the revision compare shows beside it, or 0.
    """
    number_by_id = {revision.id: revision.number for revision in history.revisions}
    rows: list[RevisionRow] = []
    for revision in history.revisions:
        render = find_render_set(history, revision.id, export_path)
        thumbnail = ""
        if render and render.state is RenderState.DONE:
            thumbnail = render.images["hero"].as_uri()

        note = ""
        if revision.restored_from in number_by_id:
            note = f"to r{number_by_id[revision.restored_from]}"
        rows.append(
            RevisionRow(
                id=revision.id,
                label=revision_label(revision),
                when=_format_time(revision.created_at),
                trigger=TRIGGER_LABELS[revision.trigger],
                note=note,
                thumbnail=thumbnail,
                is_staged=revision.id == staged_id,
                is_base=revision.id == base_id,
            )
        )
    return rows


def previous_revision_id(history: SourceHistory, revision_id: int) -> int:
    """Return the id of the revision recorded just before `revision_id`, or 0 when it is the first."""
    ids = [revision.id for revision in history.revisions]
    if revision_id not in ids:
        return 0
    position = ids.index(revision_id)
    if position + 1 >= len(ids):
        return 0
    return ids[position + 1]


def _format_time(created_at: str) -> str:
    """Show a revision's time in the machine's own time zone, as `Oct 26 13:05`."""
    return datetime.fromisoformat(created_at).astimezone().strftime("%b %d %H:%M")
