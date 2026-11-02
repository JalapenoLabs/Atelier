"""YouTrack's REST shapes, against a local stand-in server: no test reaches the real YouTrack.

The stand-in answers the requests `YouTrackTracker` makes with the shapes YouTrack Cloud returns, and records each
request, so the tests check both what is parsed and what is sent (and what is never sent: the token).
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from atelierd.config import TrackerConfig, TrackerKind
from atelierd.trackers import PostedComment, TicketAttachment, TrackerError, open_tracker
from atelierd.trackers import youtrack as youtrack_module

TOKEN = "Bearer perm:never-shown"


class Request:
    """One request the stand-in received."""

    def __init__(self, method: str, url: str, headers: dict[str, str], body: bytes) -> None:
        parsed = urllib.parse.urlsplit(url)
        self.method = method
        self.path = parsed.path
        self.query = dict(urllib.parse.parse_qsl(parsed.query))
        self.headers = headers
        self.body = body


type Route = Callable[[Request], tuple[int, Any]]


class StandIn:
    """A YouTrack stand-in on 127.0.0.1: routes by (method, path), answers JSON (or bytes), records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Route] = {}
        self.requests: list[Request] = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                request = Request(self.command, self.path, dict(self.headers), self.rfile.read(length))
                stand_in.requests.append(request)
                route = stand_in.routes.get((request.method, request.path))
                status, payload = route(request) if route else (404, {"error_description": "Not Found"})
                body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _answer
            do_POST = _answer

            def log_message(self, format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()

    def answer(self, method: str, path: str, payload: Any, status: int = 200) -> None:
        self.routes[(method, path)] = lambda request: (status, payload)


@pytest.fixture
def stand_in() -> Iterator[StandIn]:
    server = StandIn()
    yield server
    server.server.shutdown()


@pytest.fixture
def credentials(stand_in: StandIn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A Claude Code config with a reviewer entry on the stand-in's host, and one on another host."""
    config_path = tmp_path / "claude.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "tracker-reviewer": {"url": f"{stand_in.url}/mcp", "headers": {"Authorization": TOKEN}},
                    "elsewhere": {"url": "https://other.example/mcp", "headers": {"Authorization": TOKEN}},
                }
            }
        )
    )
    monkeypatch.setenv("ATELIER_CLAUDE_CONFIG", str(config_path))
    return config_path


def _config(stand_in: StandIn, reviewer: str = "tracker-reviewer") -> TrackerConfig:
    return TrackerConfig(kind=TrackerKind.YOUTRACK, url=stand_in.url, project="WAY", reviewer=reviewer)


def _issue(ticket_id: str, updated: int, comments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "idReadable": ticket_id,
        "summary": f"[Art] {ticket_id}",
        "updated": updated,
        "tags": [{"name": "Art asset"}],
        "customFields": [
            {"name": "Priority", "value": {"name": "Normal"}},
            {"name": "Stage", "value": {"name": "Review"}},
            {"name": "Estimation", "value": None},
        ],
        "comments": comments or [],
    }


# ####################### #
#       CREDENTIALS       #
# ####################### #


class TestCredentials:
    def test_credentials_are_never_sent_to_another_host(self, stand_in: StandIn, credentials: Path) -> None:
        with pytest.raises(TrackerError, match="refusing to send its credentials"):
            open_tracker(_config(stand_in, reviewer="elsewhere"))

    def test_a_missing_entry_says_which(self, stand_in: StandIn, credentials: Path) -> None:
        with pytest.raises(TrackerError, match="mcpServers.nobody"):
            open_tracker(_config(stand_in, reviewer="nobody"))

    def test_the_token_never_reaches_a_message_or_the_log(
        self,
        stand_in: StandIn,
        credentials: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        tracker = open_tracker(_config(stand_in))
        stand_in.answer("GET", "/api/users/me", {"error_description": "Unauthorized"}, status=401)
        with pytest.raises(TrackerError, match="401") as failure:
            tracker.identity_login()
        assert "never-shown" not in str(failure.value)
        assert "never-shown" not in repr(tracker)
        assert "never-shown" not in caplog.text


# ####################### #
#          READS          #
# ####################### #


class TestReads:
    def test_search_pages_through_issues_and_parses_them(
        self,
        stand_in: StandIn,
        credentials: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(youtrack_module, "_PAGE_SIZE", 2)
        delivery = {
            "id": "4-3",
            "text": "Round 2",
            "created": 1_790_000_000_000,
            "author": {"login": "alex+claude", "fullName": "Claude"},
            "attachments": [
                {"id": "8-1", "name": "hero.png", "mimeType": "image/png", "size": 4, "url": "/api/files/8-1?sign=x"},
                {"id": "8-2", "name": "old.png", "removed": True, "url": "/api/files/8-2?sign=x"},
            ],
        }
        first = {"id": "4-1", "text": "Round 1", "created": 1_780_000_000_000, "author": {"login": "alex+claude"}}
        deleted = {"id": "4-2", "text": "", "created": 1_785_000_000_000, "deleted": True}
        pages = [
            [_issue("WAY-60", 1_790_000_000_000, [delivery, first, deleted]), _issue("WAY-61", 1)],
            [_issue("WAY-62", 2)],
        ]
        stand_in.routes[("GET", "/api/issues")] = lambda request: (200, pages[int(request.query["$skip"]) // 2])

        tickets = open_tracker(_config(stand_in)).search("tag: {Art asset} Stage: Review")

        assert [ticket.id for ticket in tickets] == ["WAY-60", "WAY-61", "WAY-62"]
        searches = [request for request in stand_in.requests if request.path == "/api/issues"]
        assert [request.query["$skip"] for request in searches] == ["0", "2"]
        assert searches[0].query["query"] == "project: WAY tag: {Art asset} Stage: Review"
        assert searches[0].headers["Authorization"] == TOKEN

        carbine = tickets[0]
        assert (carbine.stage, carbine.tags, carbine.url) == ("Review", ("Art asset",), f"{stand_in.url}/issue/WAY-60")
        assert carbine.updated_at == "2026-09-21T14:13:20+00:00"
        assert [comment.id for comment in carbine.comments] == ["4-1", "4-3"]
        assert carbine.comments[0].author_name == "alex+claude"
        assert [attachment.name for attachment in carbine.comments[1].attachments] == ["hero.png"]
        assert carbine.comments[1].attachments[0].download_url == "/api/files/8-1?sign=x"

    def test_the_identity_login_is_read_once(self, stand_in: StandIn, credentials: Path) -> None:
        stand_in.answer("GET", "/api/users/me", {"login": "nebulablade"})
        tracker = open_tracker(_config(stand_in))
        assert tracker.identity_login() == tracker.identity_login() == "nebulablade"
        assert len(stand_in.requests) == 1

    def test_downloads_use_the_signed_url_without_the_token(
        self,
        stand_in: StandIn,
        credentials: Path,
        tmp_path: Path,
    ) -> None:
        stand_in.answer("GET", "/api/files/8-1", b"\x89PNG")
        attachment = TicketAttachment(id="8-1", name="hero.png", size=4, download_url="/api/files/8-1?sign=abc")
        destination = tmp_path / "review" / "hero.png"

        open_tracker(_config(stand_in)).download(attachment, destination)

        assert destination.read_bytes() == b"\x89PNG"
        assert stand_in.requests[0].query == {"sign": "abc"}
        assert "Authorization" not in stand_in.requests[0].headers
        assert list(destination.parent.iterdir()) == [destination]

    def test_linked_tickets_are_named_from_the_ticket_asked_about(self, stand_in: StandIn, credentials: Path) -> None:
        adopt = {
            "idReadable": "WAY-71",
            "summary": "[UI] Adopt the build menu",
            "customFields": [{"name": "Stage", "value": {"name": "Blocked"}}],
        }
        stand_in.answer(
            "GET",
            "/api/issues/WAY-70/links",
            [
                {
                    "direction": "INWARD",
                    "linkType": {"name": "Depend", "sourceToTarget": "depends on", "targetToSource": "is required for"},
                    "issues": [adopt],
                },
                {"direction": "BOTH", "linkType": {"name": "Relates", "sourceToTarget": "relates to"}, "issues": []},
            ],
        )
        linked = open_tracker(_config(stand_in)).linked_tickets("WAY-70")
        assert [(ticket.id, ticket.stage, ticket.link) for ticket in linked] == [
            ("WAY-71", "Blocked", "is required for")
        ]


# ####################### #
#         WRITES          #
# ####################### #


class TestWrites:
    def test_a_comment_and_its_attachments_go_up_as_the_same_identity(
        self,
        stand_in: StandIn,
        credentials: Path,
        tmp_path: Path,
    ) -> None:
        stand_in.answer("POST", "/api/issues/WAY-60/comments", {"id": "4-9"})
        stand_in.answer("POST", "/api/issues/WAY-60/comments/4-9/attachments", [{"id": "8-9"}])
        drawings = [tmp_path / "markup-1.png", tmp_path / "markup-2.png"]
        for drawing in drawings:
            drawing.write_bytes(b"png")
        tracker = open_tracker(_config(stand_in))

        posted = tracker.post_comment("WAY-60", "Longer barrel.")
        tracker.attach_to_comment(posted, drawings)

        assert posted == PostedComment(ticket="WAY-60", comment_id="4-9", identity="tracker-reviewer")
        comment_request, *upload_requests = stand_in.requests
        assert json.loads(comment_request.body) == {"text": "Longer barrel."}
        # One file per request, so each answer is that file's.
        assert len(upload_requests) == 2
        assert all(request.headers["Authorization"] == TOKEN for request in upload_requests)
        assert b'name="upload"; filename="markup-2.png"' in upload_requests[1].body

    def test_attaching_to_another_identitys_comment_is_refused_before_sending(
        self,
        stand_in: StandIn,
        credentials: Path,
        tmp_path: Path,
    ) -> None:
        theirs = PostedComment(ticket="WAY-60", comment_id="4-9", identity="youtrack")
        with pytest.raises(TrackerError, match="only takes a comment's attachments from its author"):
            open_tracker(_config(stand_in)).attach_to_comment(theirs, [tmp_path / "markup.png"])
        assert stand_in.requests == []

    def test_a_stage_moves_through_a_command_with_spaced_values_as_they_are(
        self,
        stand_in: StandIn,
        credentials: Path,
    ) -> None:
        stand_in.answer("POST", "/api/commands", {})
        tracker = open_tracker(_config(stand_in))
        tracker.move_stage("WAY-60", "Done")
        tracker.move_stage("WAY-61", "In Progress")
        assert [json.loads(request.body) for request in stand_in.requests] == [
            {"query": "Stage Done", "issues": [{"idReadable": "WAY-60"}]},
            {"query": "Stage In Progress", "issues": [{"idReadable": "WAY-61"}]},
        ]
