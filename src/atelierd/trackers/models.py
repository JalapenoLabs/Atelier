"""What every ticket tracker reads and writes, in the tracker's own terms: tickets, comments and attachments.

These are the tracker layer's models, shared by every implementation. They carry what the tracker said and nothing
Atelier derived from it; the review queue (`atelierd.review`) turns them into queue items with local files. An
attachment's `download_url` may carry a signature that downloads it without credentials, so it stays inside the
daemon: no front door returns these models.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TrackerError(Exception):
    """The tracker refused or failed a request, or its credentials cannot be read. Never carries a credential."""


class _TrackerModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class TicketAttachment(_TrackerModel):
    """A file attached to a comment."""

    id: str
    name: str = Field(description="The name its uploader gave it: untrusted, never used as a path as it is.")
    mime_type: str = ""
    size: int = 0
    download_url: str = Field(description="Where its bytes download from; may carry a signature, so never shown.")


class TicketComment(_TrackerModel):
    """One comment on a ticket, oldest first in a ticket."""

    id: str
    author_login: str
    author_name: str
    text: str
    created_at: str = Field(description="ISO 8601, UTC.")
    attachments: tuple[TicketAttachment, ...] = ()


class Ticket(_TrackerModel):
    """A ticket as a queue query returns it."""

    id: str = Field(description="The tracker's readable id, such as WAY-60.")
    summary: str
    stage: str | None = Field(default=None, description="The value of the configured stage field, when set.")
    tags: tuple[str, ...] = ()
    url: str = Field(description="The ticket's page in the tracker.")
    updated_at: str = Field(description="ISO 8601, UTC.")
    comments: tuple[TicketComment, ...] = ()


class LinkedTicket(_TrackerModel):
    """A ticket linked to another, as the stage rules see it."""

    id: str
    summary: str
    stage: str | None = None
    link: str = Field(description="The link's name as seen from the first ticket, such as 'relates to'.")


class PostedComment(_TrackerModel):
    """A comment just posted, with the identity that posted it: only that identity may attach files to it."""

    ticket: str
    comment_id: str
    identity: str = Field(description="The credentials entry the comment was posted with.")
