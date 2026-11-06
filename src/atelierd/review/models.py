"""The review queue as the front doors see it: tickets waiting for review, with their latest delivery on disk.

A queue item is a ticket, not a source: some tickets (a UI design, a sound) have no source at all, and those that do
link to it through a ticket file (`links.py`). Paths to downloaded files are absolute, because the window reads them
straight from disk the way it reads renders.

A decision on a ticket travels as a `DecisionJob`: the `Decision` the reviewer made, the `WrittenDecision` Claude
wrote from it, and the comment once posted (`jobs.py`).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from atelierd.config import ReviewVerdict
from atelierd.trackers.models import PostedComment


class _ReviewModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class ReviewAttachment(_ReviewModel):
    """One file of a delivery, downloaded into the project's state folder. Untrusted data: shown, never run."""

    name: str = Field(description="Its file name on disk, made safe from the name its uploader gave it.")
    path: Path
    mime_type: str = ""
    size: int = 0
    caption: str = Field(default="", description="What the reviewer sees it called, in place of its file name.")
    is_reference: bool = Field(default=False, description="Whether it is measuring material rather than the art.")


class ReviewDelivery(_ReviewModel):
    """A ticket's latest delivery: the newest comment with attachments that the reviewer did not write.

    Its attachments are in the order they are shown (`images.py`): the art, best first, then reference material.
    """

    comment_id: str
    author: str
    created_at: str
    text: str
    attachments: tuple[ReviewAttachment, ...] = ()
    error: str = Field(default="", description="Why some of its attachments could not be downloaded, if any.")


class ReviewComment(_ReviewModel):
    """One comment on a ticket."""

    id: str
    author: str
    created_at: str
    text: str
    attachment_names: tuple[str, ...] = ()
    captions: dict[str, str] = Field(default_factory=dict, description="What each attachment is called, by its name.")
    is_reviewer: bool = Field(default=False, description="Whether the reviewer's own identity wrote it.")


class ReviewLink(_ReviewModel):
    """The catalog source a ticket's asset is built in, found through a file that names the ticket."""

    source: str = Field(description="The source's .blend path.")
    exports: tuple[str, ...] = Field(default=(), description="Of its exports, those the ticket file names.")
    ticket_file: str = Field(description="The project-relative file that names the ticket.")


class ReviewItem(_ReviewModel):
    """One ticket waiting for review."""

    ticket: str
    summary: str
    stage: str | None = None
    tags: tuple[str, ...] = ()
    url: str = Field(description="The ticket's page in the tracker.")
    updated_at: str
    queues: tuple[str, ...] = Field(description="The queues that list it, in the config's order.")
    comments: tuple[ReviewComment, ...] = ()
    delivery: ReviewDelivery | None = None
    round: int = Field(default=0, description="Which round of delivery the latest one is, from 1; 0 with none.")
    link: ReviewLink | None = None


class ReviewQueueTab(_ReviewModel):
    """One configured queue and the tickets it lists, least recently updated first."""

    name: str
    query: str
    tickets: tuple[str, ...] = ()


class ReviewQueue(_ReviewModel):
    """A project's whole review queue as of its last refresh."""

    is_configured: bool = Field(description="Whether the project's atelier.toml has a [review] table.")
    tracker_name: str = Field(default="", description="The tracker's name, as the reviewer knows it.")
    tabs: tuple[ReviewQueueTab, ...] = ()
    items: tuple[ReviewItem, ...] = ()
    refreshed_at: str | None = Field(default=None, description="When the last refresh finished; None before one.")
    error: str = Field(default="", description="Why the last refresh failed; its items are the previous refresh's.")


class ReviewEventKind(StrEnum):
    """What happened to a project's review queue."""

    REFRESHED = "refreshed"
    FAILED = "failed"
    DECIDED = "decided"


class ReviewEvent(_ReviewModel):
    """A change to the review queue; a client reads the queue again to see it."""

    kind: ReviewEventKind
    added: tuple[str, ...] = Field(default=(), description="Tickets new to the queue.")
    removed: tuple[str, ...] = Field(default=(), description="Tickets that left it, decided or moved on.")
    changed: tuple[str, ...] = Field(default=(), description="Tickets still in it whose details changed.")
    error: str = ""


class PostedDecision(_ReviewModel):
    """What a decision did on the tracker."""

    ticket: str
    verdict: ReviewVerdict
    comment_id: str
    stage: str
    attachments: tuple[str, ...] = Field(default=(), description="The files attached to the comment, by name.")
    moved_linked: tuple[str, ...] = Field(default=(), description="Linked tickets the on-approve rules moved.")


# ========================================================================================
# Decision jobs
# ========================================================================================


class DecisionRequest(_ReviewModel):
    """A decision as the window submits it: one ticket, or many sharing one note (a bulk decision)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tickets: tuple[str, ...] = Field(min_length=1, description="The tickets decided; more than one is a bulk decision.")
    verdict: ReviewVerdict
    takes: tuple[str, ...] = Field(
        default=(),
        description="The reviewer's words, every take in order, verbatim. A bulk decision's note is shared by all.",
    )
    drawings: dict[str, tuple[Path, ...]] = Field(
        default_factory=dict,
        description="Drawings to attach to the comment, by ticket: image files the window saved.",
    )

    @field_validator("takes")
    @classmethod
    def _takes_have_words(cls, takes: tuple[str, ...]) -> tuple[str, ...]:
        if any(not take.strip() for take in takes):
            raise ValueError("A take holds no words; leave it out instead")
        return takes

    @field_validator("tickets")
    @classmethod
    def _tickets_are_distinct(cls, tickets: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(tickets)) != len(tickets):
            raise ValueError("A ticket is listed twice in one decision")
        return tickets


class Decision(_ReviewModel):
    """What the reviewer decided about one ticket: the record a decision job writes its comment from."""

    ticket: str
    verdict: ReviewVerdict
    takes: tuple[str, ...] = Field(default=(), description="The reviewer's words, every take in order, verbatim.")
    drawings: tuple[Path, ...] = Field(
        default=(),
        description="The job's own copies of the drawings, named as they are attached (`WAY-60-markup-1.png`).",
    )
    bulk_tickets: tuple[str, ...] = Field(
        default=(),
        description="For a bulk decision, every ticket its shared note was given for, this one included.",
    )

    @property
    def is_bulk(self) -> bool:
        """Whether one note was given for many tickets."""
        return bool(self.bulk_tickets)


class DirectionNote(_ReviewModel):
    """Feedback for all the art or a whole category, split out of a decision's words for the art direction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = Field(min_length=1, description="What it applies to, such as 'all weapons' or 'all the art'.")
    words: str = Field(min_length=1, description="The reviewer's exact words.")


class WrittenDecision(_ReviewModel):
    """A decision job's answer from Claude: the ticket's comment and the direction notes split out of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    comment: str = Field(min_length=1, description="The comment in the reviewer's voice, posted as it is.")
    direction_notes: tuple[DirectionNote, ...] = ()


class DecisionJobState(StrEnum):
    """Where a decision job is: waiting, Claude writing its comment, the daemon posting it, done, or failed."""

    QUEUED = "queued"
    WRITING = "writing"
    POSTING = "posting"
    POSTED = "posted"
    FAILED = "failed"


class DecisionJob(_ReviewModel):
    """One decision on one ticket, on its way to the tracker."""

    id: int
    decision: Decision
    state: DecisionJobState
    created_at: str
    updated_at: str
    answer: WrittenDecision | None = Field(default=None, description="The comment and direction notes, once written.")
    are_notes_saved: bool = Field(default=False, description="Whether its direction notes are in the notes file.")
    posted_comment: PostedComment | None = Field(default=None, description="Its comment, once on the ticket.")
    error: str = Field(default="", description="Why it failed, when it did.")
    raw_output: str = Field(default="", description="What `claude -p` printed, kept when its answer was refused.")

    @property
    def ticket(self) -> str:
        """The ticket decided."""
        return self.decision.ticket


class DecisionJobEvent(_ReviewModel):
    """A decision job changed state; the event carries the job as it is now."""

    job: DecisionJob
