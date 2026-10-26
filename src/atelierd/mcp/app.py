"""The daemon's HTTP front door on 127.0.0.1: the MCP servers it hosts, as one Starlette app.

- `/worker/mcp`: the worker-side `atelier` server (`worker.py`). Every request must carry a live worker's token in
  `X-Atelier-Worker`; anything else is refused before the MCP layer sees it, so the open port serves nobody else.
- `/mcp`: the manager MCP (`manager.py`), for any Claude Code session on the machine. It needs no token: like the
  window, it can only direct workers and read state, never answer an approval or sign a source off.

Both servers are stateless and answer over SSE rather than with a plain JSON body: an approval can wait on the
reviewer for hours, and a manager can wait on a turn for many minutes, while Claude Code abandons a response whose
first byte never comes after about a minute. An SSE response starts at once and is kept alive with pings. Both refuse
a `Host` or `Origin` that is not loopback (the MCP SDK's DNS rebinding protection), so a web page cannot reach them.

The manager app is mounted at the root, with its own route at `/mcp`, rather than mounted at `/mcp`: a mount at a
path matches only below it, and `POST /mcp` would be redirected to `/mcp/`, which MCP clients do not follow. A
mounted app's own lifespan never runs, so this app's lifespan runs every server's session manager.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send

from atelierd.constants import WORKER_TOKEN_HEADER
from atelierd.daemon.core import AtelierCore
from atelierd.mcp.manager import build_manager_mcp
from atelierd.mcp.worker import build_worker_mcp, find_worker

logger = logging.getLogger(__name__)

WORKER_MOUNT = "/worker"
MCP_PATH = "/mcp"


class _WorkerTokenGuard:
    """Refuse any request to the worker server that does not carry a live worker's token."""

    def __init__(self, app: ASGIApp, core: AtelierCore) -> None:
        self._app = app
        self._core = core
        self._header = WORKER_TOKEN_HEADER.lower().encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            token = next((value.decode() for name, value in scope["headers"] if name == self._header), "")
            if not token or find_worker(self._core, token) is None:
                logger.warning(
                    f"[HTTP] Refused a worker MCP request without a live worker's token from {scope.get('client')}"
                )
                body = json.dumps({"error": "Not a worker of this daemon"}).encode()
                await send(
                    {"type": "http.response.start", "status": 401, "headers": [(b"content-type", b"application/json")]}
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self._app(scope, receive, send)


def build_http_app(core: AtelierCore) -> Starlette:
    """Return the HTTP app serving every MCP server the daemon hosts."""
    worker_server = build_worker_mcp(core)
    worker_app = worker_server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=False,
        stateless_http=True,
    )

    manager_server = build_manager_mcp(core)
    manager_app = manager_server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        json_response=False,
        stateless_http=True,
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with worker_server.session_manager.run(), manager_server.session_manager.run():
            yield

    return Starlette(
        routes=[
            Mount(WORKER_MOUNT, app=_WorkerTokenGuard(worker_app, core)),
            # Last, so the worker mount is matched first; this app's one route is `/mcp`.
            Mount("", app=manager_app),
        ],
        lifespan=lifespan,
    )
