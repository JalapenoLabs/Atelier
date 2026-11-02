"""A ticket tracker held in memory, standing in for YouTrack in every test: no test reaches a real tracker.

Queries are looked up literally in `results`, so a test says exactly which tickets each queue lists; of those, only
the ones still in Review are returned, as the real queue queries ask. Every write is recorded in `writes` and applied,
so a refresh after a decision no longer lists the ticket.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from atelierd.config import TrackerConfig
from atelierd.trackers import LinkedTicket, PostedComment, Ticket, TicketAttachment, TicketComment, TrackerError

REVIEWER_LOGIN = "reviewer"
ARTIST_LOGIN = "claude"
ART_QUERY = "tag: {Art asset} Stage: Review"
UI_QUERY = "tag: {UI design} Stage: Review"

REVIEW_CONFIG = """
[review]
poll_minutes = 60

[review.tracker]
kind = "youtrack"
url = "https://tracker.example"
project = "WAY"
reviewer = "tracker-reviewer"

[review.queues]
Art = "tag: {Art asset} Stage: Review"
UI = "tag: {UI design} Stage: Review"

[review.stages]
approve = "Done"
changes = "Develop"

[[review.on_approve]]
tagged = "UI design"
linked_summary = '\\[UI\\] Adopt'
from_stage = "Blocked"
to_stage = "Backlog"
"""


class FakeTracker:
    """Tickets, their links and their files, answered the way the `Tracker` contract describes."""

    def __init__(self, identity: str = "tracker-reviewer") -> None:
        self._identity = identity
        self.tickets: dict[str, Ticket] = {}
        self.results: dict[str, list[str]] = {}
        self.links: dict[str, list[LinkedTicket]] = {}
        self.files: dict[str, bytes] = {}
        self.writes: list[tuple[str, ...]] = []
        self.downloads: list[str] = []
        self.failing_attachments: set[str] = set()
        self.search_error: str | None = None
        self.attach_error: str | None = None
        self.stage_error: str | None = None
        # Called once, in the middle of the next search, to act while a refresh is reading.
        self.during_search: Callable[[], None] | None = None
        self.opened_with: list[TrackerConfig] = []
        self._next_comment = 100
        self._guard = threading.Lock()

    def opener(self, config: TrackerConfig) -> FakeTracker:
        """Stand in for `open_tracker`, remembering the config it was opened with."""
        self.opened_with.append(config)
        return self

    @property
    def identity(self) -> str:
        return self._identity

    def identity_login(self) -> str:
        return REVIEWER_LOGIN

    def search(self, query: str) -> list[Ticket]:
        if self.search_error:
            raise TrackerError(self.search_error)
        listed = [self.tickets[ticket_id] for ticket_id in self.results.get(query, [])]
        in_review = [ticket for ticket in listed if ticket.stage == "Review"]
        # Acting after the tickets were read is what a decision landing mid-refresh looks like.
        if self.during_search:
            action, self.during_search = self.during_search, None
            action()
        return in_review

    def get_ticket(self, ticket_id: str) -> Ticket:
        return self.tickets[ticket_id]

    def linked_tickets(self, ticket_id: str) -> list[LinkedTicket]:
        return [
            linked.model_copy(update={"stage": self.tickets[linked.id].stage}) if linked.id in self.tickets else linked
            for linked in self.links.get(ticket_id, [])
        ]

    def download(self, attachment: TicketAttachment, destination: Path) -> None:
        if attachment.id in self.failing_attachments:
            raise TrackerError(f"attachment {attachment.id} is gone")
        with self._guard:
            self.downloads.append(attachment.id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.files[attachment.id])

    def post_comment(self, ticket_id: str, text: str) -> PostedComment:
        self._next_comment += 1
        comment_id = f"4-{self._next_comment}"
        self.writes.append(("comment", ticket_id, comment_id, text))
        if ticket_id in self.tickets:
            posted = make_comment(comment_id, "2026-11-01T00:00:00+00:00", author_login=REVIEWER_LOGIN, text=text)
            ticket = self.tickets[ticket_id]
            self.tickets[ticket_id] = ticket.model_copy(update={"comments": (*ticket.comments, posted)})
        return PostedComment(ticket=ticket_id, comment_id=comment_id, identity=self._identity)

    def attach_to_comment(self, comment: PostedComment, files: Sequence[Path]) -> None:
        if comment.identity != self._identity:
            raise TrackerError("not this comment's author")
        if self.attach_error:
            raise TrackerError(self.attach_error)
        for file_path in files:
            self.writes.append(("attach", comment.ticket, comment.comment_id, file_path.name))
        if comment.ticket not in self.tickets:
            return
        ticket = self.tickets[comment.ticket]
        added = tuple(
            TicketAttachment(id=f"8-{file_path.name}", name=file_path.name, download_url="/api/files/x")
            for file_path in files
        )
        comments = tuple(
            found.model_copy(update={"attachments": (*found.attachments, *added)})
            if found.id == comment.comment_id
            else found
            for found in ticket.comments
        )
        self.tickets[comment.ticket] = ticket.model_copy(update={"comments": comments})

    def move_stage(self, ticket_id: str, stage: str) -> None:
        if self.stage_error:
            raise TrackerError(self.stage_error)
        self.writes.append(("stage", ticket_id, stage))
        if ticket_id in self.tickets:
            self.tickets[ticket_id] = self.tickets[ticket_id].model_copy(update={"stage": stage})


def make_ticket(
    ticket_id: str,
    summary: str,
    updated_at: str,
    comments: Sequence[TicketComment] = (),
    tags: Sequence[str] = ("Art asset",),
    stage: str = "Review",
) -> Ticket:
    return Ticket(
        id=ticket_id,
        summary=summary,
        stage=stage,
        tags=tuple(tags),
        url=f"https://tracker.example/issue/{ticket_id}",
        updated_at=updated_at,
        comments=tuple(comments),
    )


def make_comment(
    comment_id: str,
    created_at: str,
    attachments: Sequence[TicketAttachment] = (),
    author_login: str = ARTIST_LOGIN,
    text: str = "Delivered",
) -> TicketComment:
    return TicketComment(
        id=comment_id,
        author_login=author_login,
        author_name=author_login.title(),
        text=text,
        created_at=created_at,
        attachments=tuple(attachments),
    )


def make_attachment(
    attachment_id: str, name: str, data: bytes, tracker: FakeTracker, mime_type: str = "image/png"
) -> TicketAttachment:
    tracker.files[attachment_id] = data
    return TicketAttachment(
        id=attachment_id,
        name=name,
        mime_type=mime_type,
        size=len(data),
        download_url=f"/api/files/{attachment_id}?sign=secret",
    )


def add_review_scenario(tracker: FakeTracker) -> None:
    """Three tickets in review, for a project configured with `REVIEW_CONFIG`.

    Two art tickets (WAY-60 with three rounds of comments, WAY-61 with none) and a UI design (WAY-70, also tagged
    Art asset, so both queues list it) with three linked tickets of which only WAY-71 matches the on-approve rule.
    """
    hero = make_attachment("8-1", "hero.png", b"hero-round-2", tracker)
    side = make_attachment("8-2", "image.png", b"side", tracker)
    side_again = make_attachment("8-3", "image.png", b"side-again", tracker)
    sneaky = make_attachment("8-4", "../../escape.png", b"sneaky", tracker)
    old_round = make_attachment("8-0", "hero.png", b"hero-round-1", tracker)
    drawing = make_attachment("8-9", "markup.png", b"drawing", tracker)
    tracker.tickets["WAY-60"] = make_ticket(
        "WAY-60",
        "[Art] Carbine",
        "2026-10-02T00:00:00+00:00",
        comments=(
            make_comment("4-1", "2026-10-01T00:00:00+00:00", (old_round,), text="Round 1"),
            make_comment("4-2", "2026-10-01T06:00:00+00:00", (drawing,), author_login=REVIEWER_LOGIN, text="Longer"),
            make_comment("4-3", "2026-10-02T00:00:00+00:00", (hero, side, side_again, sneaky), text="Round 2"),
            make_comment("4-4", "2026-10-02T01:00:00+00:00", text="A note with no files"),
        ),
    )
    tracker.tickets["WAY-61"] = make_ticket("WAY-61", "[Art] Wall", "2026-10-01T00:00:00+00:00")
    tracker.tickets["WAY-70"] = make_ticket(
        "WAY-70",
        "[UI design] Build menu",
        "2026-10-03T00:00:00+00:00",
        tags=("UI design", "Art asset"),
    )
    tracker.tickets["WAY-71"] = make_ticket("WAY-71", "[UI] Adopt the build menu", "2026", tags=(), stage="Blocked")
    tracker.tickets["WAY-72"] = make_ticket("WAY-72", "[UI] Adopt the old menu", "2026", tags=(), stage="Develop")
    tracker.tickets["WAY-73"] = make_ticket("WAY-73", "Build menu code", "2026", tags=(), stage="Blocked")
    tracker.links["WAY-70"] = [
        LinkedTicket(id=ticket_id, summary=tracker.tickets[ticket_id].summary, link="relates to")
        for ticket_id in ("WAY-71", "WAY-72", "WAY-73")
    ]
    tracker.results[ART_QUERY] = ["WAY-60", "WAY-61", "WAY-70"]
    tracker.results[UI_QUERY] = ["WAY-70"]
