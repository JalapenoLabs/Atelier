"""The review queue as the front doors see it: tickets waiting for review, with their latest delivery on disk.

A queue item is a ticket, not a source: some tickets (a UI design, a sound) have no source at all, and those that do
link to it through a ticket file (`links.py`). Paths to downloaded files are absolute, because the window reads them
straight from disk the way it reads renders.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from atelierd.config import ReviewVerdict


class _ReviewModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class ReviewAttachment(_ReviewModel):
    """One file of a delivery, downloaded into the project's state folder. Untrusted data: shown, never run."""

    name: str = Field(description="Its file name on disk, made safe from the name its uploader gave it.")
    path: Path
    mime_type: str = ""
    size: int = 0


class ReviewDelivery(_ReviewModel):
    """A ticket's latest delivery: the newest comment with attachments that the reviewer did not write."""

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
    link: ReviewLink | None = None


class ReviewQueueTab(_ReviewModel):
    """One configured queue and the tickets it lists, least recently updated first."""

    name: str
    query: str
    tickets: tuple[str, ...] = ()


class ReviewQueue(_ReviewModel):
    """A project's whole review queue as of its last refresh."""

    is_configured: bool = Field(description="Whether the project's atelier.toml has a [review] table.")
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
