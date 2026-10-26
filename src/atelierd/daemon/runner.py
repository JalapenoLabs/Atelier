"""Running the daemon: the core behind both front doors on one asyncio loop, until it is told to stop.

Startup order matters. The HTTP socket is bound first, so the worker-side MCP's port is known before any worker can
be configured with it (port 0 picks a free one, which the tests use). Then the core, then the RPC socket, then HTTP
serving. Stopping runs it backwards: no new RPC requests, then every project (its workers' processes, watcher and
tracker), then HTTP.

uvicorn normally captures SIGINT and SIGTERM itself; here the daemon owns shutdown (it must stop workers, not only
HTTP), so `_Uvicorn` leaves signals to the daemon's own handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import socket
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

import uvicorn
from pydantic import BaseModel, ConfigDict, Field

from atelierd.constants import (
    BLENDER_MCP_COMMAND,
    BLENDER_MCP_COMMAND_ENV,
    CLAUDE_COMMAND,
    CLAUDE_COMMAND_ENV,
)
from atelierd.daemon.core import AtelierCore
from atelierd.daemon.desktop import notify_approval
from atelierd.mcp.app import MCP_PATH, WORKER_MOUNT, build_http_app
from atelierd.rpc.protocol import DaemonInfo
from atelierd.rpc.server import RpcServer
from atelierd.workers import BlenderLauncher, WorkerSettings, launch_background_blender
from atelierd.workers.models import Approval

logger = logging.getLogger(__name__)

_HTTP_SHUTDOWN_TIMEOUT_S = 5


class DaemonOptions(BaseModel):
    """How one daemon runs."""

    model_config = ConfigDict(frozen=True)

    socket_path: Path
    registry_path: Path
    http_port: int = Field(description="The HTTP front door's port on 127.0.0.1; 0 picks a free one.")
    state_root: Path | None = None


class _Uvicorn(uvicorn.Server):
    """uvicorn without its own signal handling: the daemon decides when to stop."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class Daemon:
    """One running daemon."""

    def __init__(
        self,
        options: DaemonOptions,
        launch_blender: BlenderLauncher = launch_background_blender,
        notify: Callable[[Approval], None] = notify_approval,
    ) -> None:
        self.options = options
        self._launch_blender = launch_blender
        self._notify = notify
        self.core: AtelierCore | None = None
        self.http_port = 0
        self.is_ready = threading.Event()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Serve until `stop_event` is set, then shut down cleanly.

        Raises:
            SocketInUseError: another daemon serves on the socket.
            OSError: the HTTP port is taken.
        """
        http_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        http_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        http_socket.bind(("127.0.0.1", self.options.http_port))
        http_socket.listen()
        self.http_port = http_socket.getsockname()[1]

        settings = WorkerSettings(
            claude_command=_find_command(CLAUDE_COMMAND_ENV, CLAUDE_COMMAND),
            blender_mcp_command=_find_command(BLENDER_MCP_COMMAND_ENV, BLENDER_MCP_COMMAND),
            worker_mcp_url=f"http://127.0.0.1:{self.http_port}{WORKER_MOUNT}{MCP_PATH}",
            hook_python=Path(sys.executable),
        )
        core = AtelierCore(
            self.options.registry_path,
            settings,
            self._launch_blender,
            self._notify,
            state_root=self.options.state_root,
        )
        self.core = core
        info = DaemonInfo(
            pid=os.getpid(),
            socket_path=self.options.socket_path,
            http_port=self.http_port,
            state_root=self.options.state_root,
        )
        rpc = RpcServer(core, info)
        try:
            await rpc.start()
        except BaseException:
            http_socket.close()
            raise

        http_config = uvicorn.Config(
            build_http_app(core),
            log_level="warning",
            lifespan="on",
            # A worker's open MCP connection must not hold the daemon up once its worker has stopped.
            timeout_graceful_shutdown=_HTTP_SHUTDOWN_TIMEOUT_S,
        )
        http = _Uvicorn(http_config)
        http_task = asyncio.create_task(http.serve(sockets=[http_socket]))
        logger.info(
            f"[Daemon] Serving: RPC on {self.options.socket_path}, MCP on http://127.0.0.1:{self.http_port} "
            f"(pid {os.getpid()})"
        )
        self.is_ready.set()

        try:
            await stop_event.wait()
        finally:
            logger.info("[Daemon] Stopping")
            await rpc.close()
            # Workers first: stopping them cancels their pending approvals, which ends the HTTP calls waiting on them.
            await asyncio.to_thread(core.shutdown)
            http.should_exit = True
            await http_task
            http_socket.close()
            logger.info("[Daemon] Stopped")


def _find_command(environment_variable: str, default: str) -> str:
    """A command by absolute path when it can be found, honouring its override variable."""
    command = os.environ.get(environment_variable) or default
    executable = shutil.which(command)
    if executable:
        return executable
    logger.warning(
        f"[Daemon] '{command}' is not on PATH; workers will fail to start until it is (or set {environment_variable})"
    )
    return command
