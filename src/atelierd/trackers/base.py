"""The contract every ticket tracker implements, so the review queue never knows which tracker it talks to.

One implementation per tracker (`youtrack.py`), chosen by the `kind` in `[review.tracker]` through `lookup.py`.
Another tracker (Jira, GitHub) is one more module and one more entry in that lookup; nothing else changes.

Implementations act as one identity, the `reviewer` credentials entry, for reads and writes alike. Writing a comment
returns a `PostedComment` naming that identity, and attaching to it checks the identity matches, because trackers
refuse attachments on a comment someone else wrote (YouTrack answers 403).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

from atelierd.config import TrackerConfig
from atelierd.trackers.models import LinkedTicket, PostedComment, Ticket, TicketAttachment


class Tracker(Protocol):
    """A ticket tracker, as the review queue uses it. Every call may block on the network."""

    @property
    def identity(self) -> str:
        """The credentials entry this tracker acts as."""
        ...

    def identity_login(self) -> str:
        """Return the tracker login those credentials act as, so the queue can tell the reviewer's own comments."""
        ...

    def search(self, query: str) -> list[Ticket]:
        """Return every ticket of the configured project matching a query, with its comments and their attachments."""
        ...

    def get_ticket(self, ticket_id: str) -> Ticket:
        """Return one ticket as it is now, with its comments."""
        ...

    def linked_tickets(self, ticket_id: str) -> list[LinkedTicket]:
        """Return the tickets linked to one, with their current stage."""
        ...

    def download(self, attachment: TicketAttachment, destination: Path) -> None:
        """Write an attachment's bytes to `destination`, whole or not at all."""
        ...

    def post_comment(self, ticket_id: str, text: str) -> PostedComment:
        """Post a comment on a ticket as this tracker's identity."""
        ...

    def attach_to_comment(self, comment: PostedComment, files: Sequence[Path]) -> None:
        """Attach files to a comment this same identity posted.

        Raises:
            TrackerError: the comment was posted by another identity, or the tracker refused a file.
        """
        ...

    def move_stage(self, ticket_id: str, stage: str) -> None:
        """Set a ticket's stage field."""
        ...


# How the daemon opens a project's tracker; the tests pass a fake one in its place.
type TrackerOpener = Callable[[TrackerConfig], Tracker]
