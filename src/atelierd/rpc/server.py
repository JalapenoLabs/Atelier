"""The GUI's front door: JSON-RPC 2.0 over `$XDG_RUNTIME_DIR/atelier.sock`, with an event subscription.

The socket is created with mode 0600, so filesystem permissions are the authentication: only the user can connect.
Starting refuses a socket a live daemon answers on, and replaces one left behind by a daemon that died.

Each request runs its core call in a thread (`asyncio.to_thread`), so a scan or a restore never holds up another
client or the events. Every line a connection receives is written by that connection's own writer task, in order, so
responses and events never interleave mid-line. Core events arrive on the core's threads and are hopped onto the
event loop with `call_soon_threadsafe`, the same way the window hops them onto its UI thread.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from atelierd.daemon.core import AtelierCore, CoreEvent
from atelierd.exports import convert_export
from atelierd.revisions.service import OBJECTS_DIR_NAME
from atelierd.rpc import protocol
from atelierd.rpc.protocol import (
    ERROR_TYPE_BY_NAME,
    EVENT_KIND_BY_MODEL,
    EVENT_METHOD,
    MAX_LINE_BYTES,
    DaemonInfo,
    EventNotification,
    OpenedProject,
    RpcMethod,
)

logger = logging.getLogger(__name__)

type _Handler = Callable[[Any], Any]


class SocketInUseError(Exception):
    """Another daemon is already serving on the socket."""


class _Connection:
    """One client: its outgoing lines, in order, and which project's events it wants."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.outgoing: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.is_subscribed = False
        self.subscribed_project: str | None = None
        # Requests in flight, held so the loop never drops a task it only weakly references.
        self.tasks: set[asyncio.Task[None]] = set()
        # Writes every outgoing line, and closes the connection once it is told to stop.
        self.writer_task: asyncio.Task[None] | None = None

    def send(self, message: dict[str, Any]) -> None:
        self.outgoing.put_nowait(json.dumps(message).encode() + b"\n")


class RpcServer:
    """Serves the core to the window over a Unix socket."""

    def __init__(self, core: AtelierCore, info: DaemonInfo) -> None:
        self._core = core
        self._info = info
        self._connections: set[_Connection] = set()
        self._server: asyncio.Server | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._handlers: dict[str, tuple[RpcMethod[Any, Any], _Handler]] = {}
        self._register_handlers()

    async def start(self) -> None:
        """Listen on the socket.

        Raises:
            SocketInUseError: a daemon already answers on it.
        """
        socket_path = self._info.socket_path
        _claim_socket_path(socket_path)
        self._loop = asyncio.get_running_loop()
        # Created owner-only from the first instant, rather than chmod-ed after a window where anyone could connect.
        previous_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(
                self._serve_connection,
                path=str(socket_path),
                limit=MAX_LINE_BYTES,
            )
        finally:
            os.umask(previous_umask)
        socket_path.chmod(0o600)
        self._unsubscribe = self._core.subscribe(self._on_core_event)
        logger.info(f"[RPC] Listening on {socket_path}")

    async def close(self) -> None:
        """Stop listening, end every connection and remove the socket."""
        if self._unsubscribe:
            self._unsubscribe()
        if self._server:
            self._server.close()
        connections = list(self._connections)
        for connection in connections:
            connection.outgoing.put_nowait(None)
        # Each writer closes its connection once it has sent what was queued, so every client hears the end now.
        writer_tasks = [connection.writer_task for connection in connections if connection.writer_task]
        await asyncio.gather(*writer_tasks, return_exceptions=True)
        if self._server:
            # A client accepted a moment ago may have no handler running yet; its connection is closed here.
            self._server.close_clients()
            await self._server.wait_closed()
        self._info.socket_path.unlink(missing_ok=True)
        logger.info("[RPC] Closed")

    # -----------------------------------------------------------------------------
    # Connections
    # -----------------------------------------------------------------------------

    async def _serve_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = _Connection(writer)
        self._connections.add(connection)
        writer_task = asyncio.create_task(self._write_lines(connection))
        connection.writer_task = writer_task
        logger.debug(f"[RPC] A client connected ({len(self._connections)} now)")
        try:
            while line := await reader.readline():
                task = asyncio.create_task(self._handle_line(connection, line))
                connection.tasks.add(task)
                task.add_done_callback(connection.tasks.discard)
        except (ConnectionError, asyncio.LimitOverrunError, ValueError) as error:
            logger.warning(f"[RPC] Dropping a client: {error}")
        finally:
            self._connections.discard(connection)
            connection.outgoing.put_nowait(None)
            await writer_task
            logger.debug(f"[RPC] A client disconnected ({len(self._connections)} left)")

    async def _write_lines(self, connection: _Connection) -> None:
        try:
            while (line := await connection.outgoing.get()) is not None:
                connection.writer.write(line)
                await connection.writer.drain()
        except ConnectionError as error:
            logger.debug(f"[RPC] A client went away mid-write: {error}")
        finally:
            connection.writer.close()

    async def _handle_line(self, connection: _Connection, line: bytes) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            connection.send(_error_response(None, protocol.PARSE_ERROR, f"Not JSON: {error}"))
            return
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or "method" not in message:
            request_id = message.get("id") if isinstance(message, dict) else None
            connection.send(_error_response(request_id, protocol.INVALID_REQUEST, "Not a JSON-RPC 2.0 request"))
            return

        request_id = message.get("id")
        method_name = str(message["method"])
        params = message.get("params") or {}
        response = await self._dispatch(connection, method_name, params)
        if request_id is None:
            # A notification from the client: nobody waits for an answer.
            return
        connection.send({"jsonrpc": "2.0", "id": request_id, **response})

    async def _dispatch(self, connection: _Connection, method_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Run one method and return the `result` or `error` member of its response."""
        entry = self._handlers.get(method_name)
        if entry is None:
            return {"error": {"code": protocol.METHOD_NOT_FOUND, "message": f"No method {method_name}"}}
        method, handler = entry
        try:
            parsed = method.params.model_validate(params)
        except ValidationError as error:
            return {"error": {"code": protocol.INVALID_PARAMS, "message": str(error)}}

        if method is protocol.SUBSCRIBE:
            connection.is_subscribed = True
            connection.subscribed_project = parsed.project
            return {"result": None}

        try:
            result = await asyncio.to_thread(handler, parsed)
        except Exception as error:
            error_name = type(error).__name__
            if error_name not in ERROR_TYPE_BY_NAME:
                logger.exception(f"[RPC] {method_name} failed: {error}")
                return {"error": {"code": protocol.INTERNAL_ERROR, "message": f"{error_name}: {error}"}}
            # KeyError's str() is its repr; its argument is the message.
            message = str(error.args[0]) if isinstance(error, KeyError) and error.args else str(error)
            logger.info(f"[RPC] {method_name}: {error_name}: {message}")
            return {"error": {"code": protocol.APPLICATION_ERROR, "message": message, "data": {"type": error_name}}}
        return {"result": method.result.dump_python(result, mode="json")}

    # -----------------------------------------------------------------------------
    # Events
    # -----------------------------------------------------------------------------

    def _on_core_event(self, project_name: str, event: CoreEvent) -> None:
        """Called on a core thread; hop to the loop."""
        if self._loop is None or self._loop.is_closed():
            return
        self._loop.call_soon_threadsafe(self._broadcast, project_name, event)

    def _broadcast(self, project_name: str, event: CoreEvent) -> None:
        notification = EventNotification(
            project=project_name,
            kind=EVENT_KIND_BY_MODEL[type(event)],
            event=event.model_dump(mode="json"),
        )
        message = {"jsonrpc": "2.0", "method": EVENT_METHOD, "params": notification.model_dump(mode="json")}
        for connection in self._connections:
            wants_project = connection.subscribed_project in (None, project_name)
            if connection.is_subscribed and wants_project:
                connection.send(message)

    # -----------------------------------------------------------------------------
    # Handlers: each one a thin call into the core
    # -----------------------------------------------------------------------------

    def _register[ParamsType: BaseModel, ResultType](
        self,
        method: RpcMethod[ParamsType, ResultType],
        handler: Callable[[ParamsType], ResultType],
    ) -> None:
        self._handlers[method.name] = (method, handler)

    def _register_handlers(self) -> None:
        core = self._core
        register = self._register

        register(protocol.DAEMON_INFO, lambda params: self._info)
        register(protocol.SUBSCRIBE, lambda params: None)
        register(protocol.LIST_PROJECTS, lambda params: core.list_projects())
        register(protocol.OPEN_PROJECT, self._open_project)

        register(protocol.RESCAN, lambda params: core.project(params.project).catalog.scan())
        register(protocol.LIST_SOURCES, lambda params: core.project(params.project).catalog.list_sources())
        register(protocol.LIST_REFERENCES, lambda params: core.project(params.project).catalog.list_references())
        register(
            protocol.SET_STATUS,
            lambda params: core.project(params.project).catalog.set_status(params.source, params.status),
        )
        register(
            protocol.LIST_BOOKMARKS,
            lambda params: core.project(params.project).catalog.list_bookmarks(params.source),
        )
        register(
            protocol.SAVE_BOOKMARK,
            lambda params: core.project(params.project).catalog.save_bookmark(params.source, params.bookmark),
        )
        register(
            protocol.DELETE_BOOKMARK,
            lambda params: core.project(params.project).catalog.delete_bookmark(params.source, params.name),
        )
        register(
            protocol.ADD_REFERENCE,
            lambda params: core.project(params.project).catalog.add_reference(params.image),
        )
        register(
            protocol.CONVERT_EXPORT,
            lambda params: convert_export(core.project(params.project).project, params.export),
        )

        register(protocol.GET_HISTORY, lambda params: core.project(params.project).revisions.history(params.source))
        register(
            protocol.REQUEST_RENDERS,
            lambda params: core.project(params.project).revisions.request_standard_renders(
                params.revision_id, params.export
            ),
        )
        register(
            protocol.CONVERT_REVISION_EXPORT,
            lambda params: core.project(params.project).revisions.revisions.convert_revision_export(
                params.revision_id, params.export
            ),
        )
        register(
            protocol.RESTORE,
            lambda params: core.project(params.project).revisions.restore(params.source, params.revision_id),
        )

        register(protocol.GET_WORKER, lambda params: core.project(params.project).workers.snapshot(params.source))
        register(protocol.LIST_WORKERS, lambda params: core.project(params.project).workers.list_infos())
        register(
            protocol.SEND_PROMPT,
            lambda params: core.project(params.project).workers.send_prompt(
                params.source, params.prompt, params.feedback_id
            ),
        )
        register(protocol.INTERRUPT, lambda params: core.project(params.project).workers.get(params.source).interrupt())
        register(
            protocol.NEW_SESSION,
            lambda params: core.project(params.project).workers.get(params.source).new_session(),
        )
        register(protocol.STOP_WORKER, lambda params: core.project(params.project).workers.get(params.source).stop())
        register(
            protocol.PIN_REFERENCES,
            lambda params: list(
                core.project(params.project).workers.get(params.source).pin_references(params.references)
            ),
        )

        register(protocol.CREATE_FEEDBACK, lambda params: core.project(params.project).feedback.create(params.request))
        register(
            protocol.LIST_FEEDBACK,
            lambda params: core.project(params.project).feedback.list_feedback(params.source),
        )

        register(protocol.LIST_APPROVALS, lambda params: core.approvals.list_pending(params.project))
        register(
            protocol.ANSWER_APPROVAL,
            lambda params: core.approvals.answer(params.approval_id, params.is_allowed, params.message),
        )

    def _open_project(self, params: protocol.OpenProjectParams) -> OpenedProject:
        runtime = self._core.open_project(params.reference)
        project = runtime.project
        return OpenedProject(
            name=project.name,
            root=project.root,
            state_dir=project.state_dir,
            objects_dir=project.state_dir / OBJECTS_DIR_NAME,
            scan=runtime.catalog.scan(),
        )


def _claim_socket_path(socket_path: Path) -> None:
    """Make way for a new socket: refuse a live daemon's, remove a dead one's.

    Raises:
        SocketInUseError: a daemon answers on it.
    """
    if not socket_path.exists():
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        return
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        try:
            probe.connect(str(socket_path))
        except OSError:
            logger.info(f"[RPC] Removing {socket_path}, left behind by a daemon that is no longer running")
            socket_path.unlink()
            return
    raise SocketInUseError(f"A daemon is already serving on {socket_path}")


def _error_response(request_id: object, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
