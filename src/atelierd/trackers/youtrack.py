"""YouTrack, over its REST API, acting as the identity of one Claude Code `mcpServers` entry.

The REST details this relies on were measured against YouTrack Cloud:

- A listing returns only the fields named in `fields=`, nested as `comments(attachments(...))`, and pages with
  `$top` and `$skip`. Times are epoch milliseconds.
- An attachment's `url` is relative and signed: it downloads with no Authorization header, and `/api/files/<id>`
  with the header but no signature answers 400. So downloads deliberately send no header.
- Attachments upload one file per multipart request (field `upload`): a request with several stores them all but
  lists only the last. A comment's attachments must come from the comment's author (403 otherwise).
- Fields change through the commands API (`Stage Done`), where a value with spaces is written in braces. Tags are
  never applied through commands, which would create a tag literally named `{Two words}`; Atelier only reads them.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelierd.config import TrackerConfig
from atelierd.constants import TRACKER_TIMEOUT_S
from atelierd.trackers.credentials import load_authorization
from atelierd.trackers.models import (
    LinkedTicket,
    PostedComment,
    Ticket,
    TicketAttachment,
    TicketComment,
    TrackerError,
)

logger = logging.getLogger(__name__)

_ATTACHMENT_FIELDS = "id,name,mimeType,size,url,removed"
_COMMENT_FIELDS = f"id,text,created,deleted,author(login,fullName),attachments({_ATTACHMENT_FIELDS})"
_CUSTOM_FIELD_FIELDS = "name,value(name)"
_ISSUE_FIELDS = (
    f"idReadable,summary,updated,tags(name),customFields({_CUSTOM_FIELD_FIELDS}),comments({_COMMENT_FIELDS})"
)
_LINK_FIELDS = (
    "direction,linkType(name,sourceToTarget,targetToSource),"
    f"issues(idReadable,summary,customFields({_CUSTOM_FIELD_FIELDS}))"
)
# Issues per page of a search; each carries all its comments, so pages stay well under YouTrack's own cap.
_PAGE_SIZE = 50
# Which of a link type's names reads from the ticket asked about, by the direction YouTrack reports.
_LINK_NAME_KEY_BY_DIRECTION = {
    "OUTWARD": "sourceToTarget",
    "INWARD": "targetToSource",
    "BOTH": "sourceToTarget",
}


class YouTrackTracker:
    """One YouTrack project, read and written as the configured reviewer."""

    def __init__(self, config: TrackerConfig) -> None:
        """Read the reviewer's credentials.

        Raises:
            TrackerError: the credentials cannot be read, or belong to another host.
        """
        self._config = config
        self._origin = config.url
        self._authorization = load_authorization(config.reviewer, config.url)
        self._login: str | None = None

    def __repr__(self) -> str:
        # Spelled out so nothing that prints a tracker can ever print its credentials.
        return f"YouTrackTracker({self._origin}, project {self._config.project}, as {self._config.reviewer})"

    @property
    def identity(self) -> str:
        """The credentials entry this tracker acts as."""
        return self._config.reviewer

    # ####################### #
    #          READS          #
    # ####################### #

    def identity_login(self) -> str:
        """Return the login the reviewer's credentials act as (read once)."""
        if self._login is None:
            user = self._call("GET", "users/me", {"fields": "login"})
            self._login = str(user["login"])
        return self._login

    def search(self, query: str) -> list[Ticket]:
        """Return every ticket of the project matching a query, a page at a time."""
        scoped_query = f"project: {self._config.project} {query}"
        tickets: list[Ticket] = []
        while True:
            page = self._call(
                "GET",
                "issues",
                {"query": scoped_query, "fields": _ISSUE_FIELDS, "$top": _PAGE_SIZE, "$skip": len(tickets)},
            )
            tickets.extend(self._ticket(issue) for issue in page)
            if len(page) < _PAGE_SIZE:
                logger.debug(f"[YouTrack] '{scoped_query}' lists {len(tickets)} tickets")
                return tickets

    def get_ticket(self, ticket_id: str) -> Ticket:
        """Return one ticket as it is now."""
        issue = self._call("GET", f"issues/{urllib.parse.quote(ticket_id)}", {"fields": _ISSUE_FIELDS})
        return self._ticket(issue)

    def linked_tickets(self, ticket_id: str) -> list[LinkedTicket]:
        """Return the tickets linked to one, in every direction, with their stage."""
        links = self._call("GET", f"issues/{urllib.parse.quote(ticket_id)}/links", {"fields": _LINK_FIELDS})
        linked: list[LinkedTicket] = []
        for link in links:
            link_type = link.get("linkType") or {}
            name_key = _LINK_NAME_KEY_BY_DIRECTION.get(str(link.get("direction")), "sourceToTarget")
            link_name = link_type.get(name_key) or link_type.get("name") or ""
            for issue in link.get("issues") or []:
                linked.append(
                    LinkedTicket(
                        id=issue["idReadable"],
                        summary=issue.get("summary") or "",
                        stage=self._stage_of(issue),
                        link=link_name,
                    )
                )
        return linked

    def download(self, attachment: TicketAttachment, destination: Path) -> None:
        """Write an attachment's bytes to `destination` through a temporary file, so a failure leaves nothing."""
        # The signed URL is the credential here; the reviewer's token is never sent to the file endpoint.
        download_url = urllib.parse.urljoin(f"{self._origin}/", attachment.download_url)
        partial_path = destination.with_name(f".{destination.name}.part")
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            with partial_path.open("wb") as partial_file:
                partial_file.write(self._send("GET", download_url, authorization=None))
            partial_path.replace(destination)
        finally:
            partial_path.unlink(missing_ok=True)

    # ####################### #
    #         WRITES          #
    # ####################### #

    def post_comment(self, ticket_id: str, text: str) -> PostedComment:
        """Post a comment as the reviewer."""
        comment = self._call(
            "POST",
            f"issues/{urllib.parse.quote(ticket_id)}/comments",
            {"fields": "id"},
            payload={"text": text},
        )
        logger.info(f"[YouTrack] Posted comment {comment['id']} on {ticket_id} as {self.identity}")
        return PostedComment(ticket=ticket_id, comment_id=str(comment["id"]), identity=self.identity)

    def attach_to_comment(self, comment: PostedComment, files: Sequence[Path]) -> None:
        """Attach files to a comment the reviewer posted, one request per file so each one's answer is its own.

        Raises:
            TrackerError: the comment was posted with other credentials, or YouTrack refused a file.
        """
        if comment.identity != self.identity:
            raise TrackerError(
                f"Comment {comment.comment_id} on {comment.ticket} was posted as {comment.identity}, not "
                f"{self.identity}; YouTrack only takes a comment's attachments from its author"
            )
        endpoint = (
            f"issues/{urllib.parse.quote(comment.ticket)}/comments/{urllib.parse.quote(comment.comment_id)}/attachments"
        )
        for file_path in files:
            boundary = f"atelier-{uuid.uuid4().hex}"
            content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
            body = b"".join(
                (
                    f"--{boundary}\r\n".encode(),
                    f'Content-Disposition: form-data; name="upload"; filename="{file_path.name}"\r\n'.encode(),
                    f"Content-Type: {content_type}\r\n\r\n".encode(),
                    file_path.read_bytes(),
                    f"\r\n--{boundary}--\r\n".encode(),
                )
            )
            self._send(
                "POST",
                f"{self._origin}/api/{endpoint}?fields=id",
                authorization=self._authorization,
                body=body,
                content_type=f"multipart/form-data; boundary={boundary}",
            )
            logger.info(f"[YouTrack] Attached {file_path.name} to comment {comment.comment_id} on {comment.ticket}")

    def move_stage(self, ticket_id: str, stage: str) -> None:
        """Set the ticket's stage through the commands API."""
        # A field or value with spaces is one word in a command only when it is wrapped in braces.
        command_words: list[str] = []
        for word in (self._config.stage_field, stage):
            has_space = any(character.isspace() for character in word)
            command_words.append(f"{{{word}}}" if has_space else word)
        self._call(
            "POST",
            "commands",
            payload={"query": " ".join(command_words), "issues": [{"idReadable": ticket_id}]},
        )
        logger.info(f"[YouTrack] Moved {ticket_id} to {self._config.stage_field} {stage}")

    # ####################### #
    #          HTTP           #
    # ####################### #

    def _call(
        self,
        method: str,
        path: str,
        query: dict[str, str | int] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        """Call a JSON endpoint under /api with the reviewer's credentials and decode its answer."""
        url = f"{self._origin}/api/{path}"
        if query:
            url += f"?{urllib.parse.urlencode(query)}"
        body = json.dumps(payload).encode() if payload is not None else None
        content_type = "application/json" if payload is not None else None
        response = self._send(method, url, self._authorization, body, content_type)
        if not response:
            return None
        return json.loads(response)

    def _send(
        self,
        method: str,
        url: str,
        authorization: str | None,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        """Send one request and return its body, raising YouTrack's own explanation of a refusal."""
        headers = {"Accept": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        # Only the path is ever reported: a query may carry an attachment's signature.
        path = urllib.parse.urlsplit(url).path
        try:
            with urllib.request.urlopen(request, timeout=TRACKER_TIMEOUT_S) as response:
                response_body: bytes = response.read()
                return response_body
        except urllib.error.HTTPError as error:
            response_text = error.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(response_text).get("error_description") or response_text
            except json.JSONDecodeError, AttributeError:
                detail = response_text
            raise TrackerError(
                f"YouTrack answered {error.code} to {method} {path}: {str(detail).strip()[:300]}"
            ) from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise TrackerError(f"Could not reach YouTrack for {method} {path}: {error}") from error

    # ####################### #
    #         PARSING         #
    # ####################### #

    def _ticket(self, issue: dict[str, Any]) -> Ticket:
        ticket_id = issue["idReadable"]
        comments = tuple(
            self._comment(comment) for comment in issue.get("comments") or [] if not comment.get("deleted")
        )
        return Ticket(
            id=ticket_id,
            summary=issue.get("summary") or "",
            stage=self._stage_of(issue),
            tags=tuple(tag["name"] for tag in issue.get("tags") or []),
            url=f"{self._origin}/issue/{ticket_id}",
            updated_at=_iso_from_milliseconds(issue.get("updated")),
            comments=tuple(sorted(comments, key=lambda comment: comment.created_at)),
        )

    def _comment(self, comment: dict[str, Any]) -> TicketComment:
        author = comment.get("author") or {}
        return TicketComment(
            id=str(comment["id"]),
            author_login=author.get("login") or "",
            author_name=author.get("fullName") or author.get("login") or "",
            text=comment.get("text") or "",
            created_at=_iso_from_milliseconds(comment.get("created")),
            attachments=tuple(
                TicketAttachment(
                    id=str(attachment["id"]),
                    name=attachment.get("name") or str(attachment["id"]),
                    mime_type=attachment.get("mimeType") or "",
                    size=int(attachment.get("size") or 0),
                    download_url=attachment.get("url") or "",
                )
                for attachment in comment.get("attachments") or []
                if not attachment.get("removed") and attachment.get("url")
            ),
        )

    def _stage_of(self, issue: dict[str, Any]) -> str | None:
        """The stage field's value: a state's name, or None when the field is unset or missing."""
        for field in issue.get("customFields") or []:
            if field.get("name") != self._config.stage_field:
                continue
            value = field.get("value")
            if isinstance(value, dict):
                return str(value.get("name"))
            return None
        return None


def _iso_from_milliseconds(milliseconds: int | None) -> str:
    """YouTrack's epoch milliseconds as ISO 8601 in UTC, or the epoch itself when YouTrack gave none."""
    return datetime.fromtimestamp((milliseconds or 0) / 1000, UTC).isoformat(timespec="seconds")
