"""A project's review queue: read from its ticket tracker on demand and on a slow poll, decisions written back.

`ReviewService` is what the front doors call (`runtime.review`):

- `queue()` returns the queue as of the last refresh, at once; `refresh()` reads the tracker again and waits for it.
  A refresh reads every configured queue, links each ticket to its source, and downloads each ticket's latest delivery
  into `<state>/review/<ticket>/delivery/<comment id>/`, skipping files already there. Refreshes never overlap.
- A poll thread refreshes every `poll_minutes`, and the config is read afresh each time, so a `[review]` table added
  or edited applies on the next refresh without restarting anything.
- `apply_decision()` posts a decision: the comment, its attachments (with the same identity, which the tracker
  requires), the stage its verdict moves to, and the on-approve rules for linked tickets. The ticket then leaves the
  queue at once, and a refresh already under way when it was decided cannot bring it back.

Everything that changes the queue is reported as a `ReviewEvent`. A failed refresh keeps the previous items and says
why in the queue's `error`, so the window still shows what it had.
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from atelierd.catalog import Catalog
from atelierd.config import ConfigError, ReviewConfig, ReviewVerdict, load_config
from atelierd.constants import (
    DEFAULT_REVIEW_POLL_MINUTES,
    REVIEW_DELIVERY_DIR_NAME,
    REVIEW_DIR_NAME,
    REVIEW_DOWNLOAD_WORKERS,
)
from atelierd.project import Project
from atelierd.review.links import find_ticket_links
from atelierd.review.models import (
    PostedDecision,
    ReviewAttachment,
    ReviewComment,
    ReviewDelivery,
    ReviewEvent,
    ReviewEventKind,
    ReviewItem,
    ReviewLink,
    ReviewQueue,
    ReviewQueueTab,
)
from atelierd.trackers import PostedComment, Ticket, TicketAttachment, Tracker, TrackerError, TrackerOpener

logger = logging.getLogger(__name__)

# A ticket id becomes a folder name, and comes from the tracker, so it must look like one before it touches a path.
_TICKET_ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
# Attachment names come from whoever uploaded them: anything that could step outside a folder is replaced.
_UNSAFE_NAME_PATTERN = re.compile(r"[/\\\x00]")


class DecisionError(TrackerError):
    """A decision failed part-way. `posted` is the comment already on the ticket, if it got that far."""

    def __init__(self, message: str, posted: PostedComment | None) -> None:
        super().__init__(message)
        self.posted = posted


class ReviewService:
    """One project's review queue."""

    def __init__(
        self,
        project: Project,
        catalog: Catalog,
        open_tracker: TrackerOpener,
        emit: Callable[[ReviewEvent], None],
    ) -> None:
        self.project = project
        self._catalog = catalog
        self._open_tracker = open_tracker
        self._emit = emit
        self._review_dir = project.state_dir / REVIEW_DIR_NAME
        self._refresh_guard = threading.Lock()
        self._state_guard = threading.Lock()
        self._queue = ReviewQueue(is_configured=False)
        # When each ticket was last decided (monotonic), so a refresh that began before a decision leaves it out.
        self._decided_at: dict[str, float] = {}
        self._poll_stop = threading.Event()
        self._poll_thread: threading.Thread | None = None

    # ####################### #
    #         POLLING         #
    # ####################### #

    def start(self) -> None:
        """Refresh in the background now, then every `poll_minutes`."""
        self._poll_thread = threading.Thread(target=self._poll, name=f"review-{self.project.name}", daemon=True)
        self._poll_thread.start()

    def stop(self) -> None:
        """Stop polling; a refresh under way finishes its current request first."""
        self._poll_stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            if self._poll_thread.is_alive():
                logger.warning(f"[Review] The poll of {self.project.name} did not stop within 5 s")

    def _poll(self) -> None:
        while not self._poll_stop.is_set():
            poll_minutes = DEFAULT_REVIEW_POLL_MINUTES
            try:
                queue = self.refresh()
                review_config = load_config(self.project.config_path).review
                if review_config:
                    poll_minutes = review_config.poll_minutes
                if not queue.is_configured:
                    logger.debug(f"[Review] {self.project.name} has no [review] table; checking again later")
            except (ConfigError, TrackerError, OSError) as error:
                # Already reported to the window as a failed refresh; the next poll tries again.
                logger.warning(f"[Review] Refreshing {self.project.name} failed: {error}")
            except Exception as error:
                logger.exception(f"[Review] Refreshing {self.project.name} failed unexpectedly: {error}")
            self._poll_stop.wait(poll_minutes * 60)

    # ####################### #
    #          READS          #
    # ####################### #

    def queue(self) -> ReviewQueue:
        """Return the queue as of the last refresh."""
        with self._state_guard:
            return self._queue

    def item(self, ticket: str) -> ReviewItem:
        """Return one queue item.

        Raises:
            KeyError: the ticket is not in the queue.
        """
        found = next((item for item in self.queue().items if item.ticket == ticket), None)
        if found is None:
            raise KeyError(f"{ticket} is not in the review queue of {self.project.name}")
        return found

    def refresh(self) -> ReviewQueue:
        """Read every queue from the tracker again and download new deliveries; waits for one under way first.

        Raises:
            ConfigError: atelier.toml cannot be read.
            TrackerError: the tracker could not be read.
        """
        with self._refresh_guard:
            started_at = time.monotonic()
            try:
                review_config = load_config(self.project.config_path).review
                if review_config is None:
                    refreshed = ReviewQueue(
                        is_configured=False, refreshed_at=datetime.now(UTC).isoformat(timespec="seconds")
                    )
                else:
                    refreshed = self._read_queue(review_config)
            except (ConfigError, TrackerError) as error:
                self._fail(str(error))
                raise

            with self._state_guard:
                # A ticket decided while this refresh read the tracker may still be listed there; it stays out.
                decided_since_start = {
                    ticket for ticket, decided_at in self._decided_at.items() if decided_at >= started_at
                }
                self._decided_at = {ticket: self._decided_at[ticket] for ticket in decided_since_start}
                refreshed = _without_tickets(refreshed, decided_since_start)
                previous = self._queue
                self._queue = refreshed

            self._report_changes(previous, refreshed)
            return refreshed

    def _read_queue(self, review_config: ReviewConfig) -> ReviewQueue:
        tracker = self._open_tracker(review_config.tracker)
        reviewer_login = tracker.identity_login()

        tickets_by_id: dict[str, Ticket] = {}
        queues_by_ticket: dict[str, list[str]] = {}
        tabs: list[ReviewQueueTab] = []
        for queue_name, query in review_config.queues.items():
            listed = sorted(tracker.search(query), key=lambda ticket: ticket.updated_at)
            for ticket in listed:
                tickets_by_id[ticket.id] = ticket
                queues_by_ticket.setdefault(ticket.id, []).append(queue_name)
            tabs.append(ReviewQueueTab(name=queue_name, query=query, tickets=tuple(ticket.id for ticket in listed)))

        links = find_ticket_links(self.project.root, review_config.ticket_files, self._catalog.list_sources())
        deliveries = self._download_deliveries(tracker, list(tickets_by_id.values()), reviewer_login)
        items = tuple(
            _review_item(ticket, tuple(queues_by_ticket[ticket.id]), reviewer_login, deliveries.get(ticket.id), links)
            for ticket in sorted(tickets_by_id.values(), key=lambda ticket: ticket.updated_at)
        )
        logger.info(f"[Review] Read {len(items)} tickets in {len(tabs)} queues for {self.project.name}")
        return ReviewQueue(
            is_configured=True,
            tabs=tuple(tabs),
            items=items,
            refreshed_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )

    # ####################### #
    #        DELIVERIES       #
    # ####################### #

    def _download_deliveries(
        self,
        tracker: Tracker,
        tickets: list[Ticket],
        reviewer_login: str,
    ) -> dict[str, ReviewDelivery]:
        """Download each ticket's latest delivery, keeping files already on disk, and drop superseded ones."""
        planned: dict[str, tuple[str, list[tuple[TicketAttachment, Path]]]] = {}
        downloads: list[tuple[str, TicketAttachment, Path]] = []
        deliveries: dict[str, ReviewDelivery] = {}
        for ticket in tickets:
            if not _TICKET_ID_PATTERN.match(ticket.id):
                logger.warning(f"[Review] '{ticket.id}' does not look like a ticket id; not downloading its files")
                continue
            delivery_comment = next(
                (
                    comment
                    for comment in reversed(ticket.comments)
                    if comment.attachments and comment.author_login != reviewer_login
                ),
                None,
            )
            if delivery_comment is None:
                continue
            comment_folder = self._review_dir / ticket.id / REVIEW_DELIVERY_DIR_NAME / delivery_comment.id
            files = [
                (attachment, comment_folder / file_name)
                for attachment, file_name in zip(
                    delivery_comment.attachments,
                    _safe_file_names(delivery_comment.attachments),
                    strict=True,
                )
            ]
            planned[ticket.id] = (delivery_comment.id, files)
            downloads.extend(
                (ticket.id, attachment, destination)
                for attachment, destination in files
                if not (destination.is_file() and destination.stat().st_size == attachment.size)
            )

        errors_by_ticket: dict[str, list[str]] = {}
        with ThreadPoolExecutor(max_workers=REVIEW_DOWNLOAD_WORKERS, thread_name_prefix="review-download") as pool:
            futures = [
                (ticket_id, attachment, pool.submit(tracker.download, attachment, destination))
                for ticket_id, attachment, destination in downloads
            ]
            for ticket_id, attachment, future in futures:
                try:
                    future.result()
                except (TrackerError, OSError) as error:
                    logger.warning(f"[Review] Could not download {attachment.name} of {ticket_id}: {error}")
                    errors_by_ticket.setdefault(ticket_id, []).append(f"{attachment.name}: {error}")
        if downloads:
            logger.info(f"[Review] Downloaded {len(downloads)} delivery files for {self.project.name}")

        for ticket in tickets:
            if ticket.id not in planned:
                continue
            comment_id, files = planned[ticket.id]
            comment = next(comment for comment in ticket.comments if comment.id == comment_id)
            deliveries[ticket.id] = ReviewDelivery(
                comment_id=comment_id,
                author=comment.author_name,
                created_at=comment.created_at,
                text=comment.text,
                attachments=tuple(
                    ReviewAttachment(
                        name=destination.name,
                        path=destination,
                        mime_type=attachment.mime_type,
                        size=attachment.size,
                    )
                    for attachment, destination in files
                    if destination.is_file()
                ),
                error="; ".join(errors_by_ticket.get(ticket.id, [])),
            )

        self._prune_deliveries({ticket_id: comment_id for ticket_id, (comment_id, _) in planned.items()})
        return deliveries

    def _prune_deliveries(self, current_comment_by_ticket: dict[str, str]) -> None:
        """Delete delivery downloads no ticket in the queue shows any more: superseded rounds and decided tickets."""
        if not self._review_dir.is_dir():
            return
        for ticket_folder in self._review_dir.iterdir():
            delivery_folder = ticket_folder / REVIEW_DELIVERY_DIR_NAME
            if not delivery_folder.is_dir():
                continue
            current_comment = current_comment_by_ticket.get(ticket_folder.name)
            for comment_folder in delivery_folder.iterdir():
                if comment_folder.name != current_comment:
                    logger.debug(f"[Review] Removing the superseded download {comment_folder}")
                    shutil.rmtree(comment_folder, ignore_errors=True)

    # ####################### #
    #        DECISIONS        #
    # ####################### #

    def apply_decision(
        self,
        ticket: str,
        verdict: ReviewVerdict,
        comment: str,
        attachments: Sequence[Path] = (),
        posted: PostedComment | None = None,
    ) -> PostedDecision:
        """Post a decision on a ticket as the reviewer, move its stage, and apply the on-approve rules.

        Args:
            ticket: the ticket decided.
            verdict: approve or changes; the config names the stage each moves to.
            comment: the comment's text, posted as it is.
            attachments: files to attach to that comment (drawings).
            posted: the comment a failed attempt already posted (from its `DecisionError`), so retrying it never
                posts the comment twice.

        Raises:
            ConfigError: atelier.toml cannot be read.
            ValueError: the project has no [review] table.
            DecisionError: a step failed; `posted` says whether the comment is already on the ticket.
        """
        review_config = load_config(self.project.config_path).review
        if review_config is None:
            raise ValueError(f"{self.project.name} has no [review] table in atelier.toml")
        stage = review_config.stages[verdict]

        try:
            tracker = self._open_tracker(review_config.tracker)
        except TrackerError as error:
            raise DecisionError(str(error), posted) from error
        try:
            if posted is None:
                posted = tracker.post_comment(ticket, comment)
            if attachments:
                tracker.attach_to_comment(posted, attachments)
            tracker.move_stage(ticket, stage)
            moved_linked: list[str] = []
            if verdict is ReviewVerdict.APPROVE and review_config.on_approve:
                moved_linked = self._apply_on_approve_rules(tracker, review_config, ticket)
        except TrackerError as error:
            step = "posting its comment" if posted is None else "after posting its comment"
            raise DecisionError(f"Deciding {ticket} failed {step}: {error}", posted) from error

        with self._state_guard:
            self._decided_at[ticket] = time.monotonic()
            self._queue = _without_tickets(self._queue, {ticket})
        self._emit(ReviewEvent(kind=ReviewEventKind.DECIDED, removed=(ticket,)))
        logger.info(f"[Review] {ticket}: {verdict.value}, moved to {stage}")
        return PostedDecision(
            ticket=ticket,
            verdict=verdict,
            comment_id=posted.comment_id,
            stage=stage,
            attachments=tuple(path.name for path in attachments),
            moved_linked=tuple(moved_linked),
        )

    def _apply_on_approve_rules(self, tracker: Tracker, review_config: ReviewConfig, ticket: str) -> list[str]:
        """Move the linked tickets each matching rule names, and return them."""
        tags = set(tracker.get_ticket(ticket).tags)
        rules = [rule for rule in review_config.on_approve if rule.tagged is None or rule.tagged in tags]
        if not rules:
            return []

        moved: list[str] = []
        for linked in tracker.linked_tickets(ticket):
            rule = next(
                (
                    rule
                    for rule in rules
                    if linked.stage == rule.from_stage and re.match(rule.linked_summary, linked.summary)
                ),
                None,
            )
            if rule is None:
                continue
            tracker.move_stage(linked.id, rule.to_stage)
            logger.info(f"[Review] Approving {ticket} moved {linked.id} from {rule.from_stage} to {rule.to_stage}")
            moved.append(linked.id)
        return moved

    # ####################### #
    #         EVENTS          #
    # ####################### #

    def _fail(self, error: str) -> None:
        with self._state_guard:
            self._queue = self._queue.model_copy(update={"error": error})
        self._emit(ReviewEvent(kind=ReviewEventKind.FAILED, error=error))

    def _report_changes(self, previous: ReviewQueue, refreshed: ReviewQueue) -> None:
        previous_by_ticket = {item.ticket: item for item in previous.items}
        refreshed_by_ticket = {item.ticket: item for item in refreshed.items}
        added = tuple(ticket for ticket in refreshed_by_ticket if ticket not in previous_by_ticket)
        removed = tuple(ticket for ticket in previous_by_ticket if ticket not in refreshed_by_ticket)
        changed = tuple(
            ticket
            for ticket, item in refreshed_by_ticket.items()
            if ticket in previous_by_ticket and previous_by_ticket[ticket] != item
        )
        is_same_shape = previous.is_configured == refreshed.is_configured and previous.tabs == refreshed.tabs
        if added or removed or changed or previous.error or not is_same_shape:
            self._emit(ReviewEvent(kind=ReviewEventKind.REFRESHED, added=added, removed=removed, changed=changed))


# -----------------------------------------------------------------------------
# Building queue items
# -----------------------------------------------------------------------------


def _review_item(
    ticket: Ticket,
    queues: tuple[str, ...],
    reviewer_login: str,
    delivery: ReviewDelivery | None,
    links: dict[str, ReviewLink],
) -> ReviewItem:
    return ReviewItem(
        ticket=ticket.id,
        summary=ticket.summary,
        stage=ticket.stage,
        tags=ticket.tags,
        url=ticket.url,
        updated_at=ticket.updated_at,
        queues=queues,
        comments=tuple(
            ReviewComment(
                id=comment.id,
                author=comment.author_name,
                created_at=comment.created_at,
                text=comment.text,
                attachment_names=tuple(attachment.name for attachment in comment.attachments),
                is_reviewer=comment.author_login == reviewer_login,
            )
            for comment in ticket.comments
        ),
        delivery=delivery,
        link=links.get(ticket.id),
    )


def _without_tickets(queue: ReviewQueue, tickets: set[str]) -> ReviewQueue:
    """The queue with some tickets taken out of its items and tabs."""
    if not tickets:
        return queue
    return queue.model_copy(
        update={
            "items": tuple(item for item in queue.items if item.ticket not in tickets),
            "tabs": tuple(
                tab.model_copy(update={"tickets": tuple(ticket for ticket in tab.tickets if ticket not in tickets)})
                for tab in queue.tabs
            ),
        }
    )


def _safe_file_names(attachments: Sequence[TicketAttachment]) -> list[str]:
    """Give each attachment a file name that stays inside its folder and is unique there.

    Comments often carry several files named `image.png`; a repeated name gets the attachment's id added.
    """
    taken_names: set[str] = set()
    file_names: list[str] = []
    for attachment in attachments:
        safe_name = _UNSAFE_NAME_PATTERN.sub("_", attachment.name).lstrip(".") or attachment.id
        if safe_name in taken_names:
            stem, dot, extension = safe_name.rpartition(".")
            safe_name = f"{stem}-{attachment.id}{dot}{extension}" if dot else f"{safe_name}-{attachment.id}"
        taken_names.add(safe_name)
        file_names.append(safe_name)
    return file_names
