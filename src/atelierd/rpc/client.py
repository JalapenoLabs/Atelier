"""A blocking JSON-RPC client of the daemon, for the window (and anything else on the machine that wants one).

Calls block the calling thread until the answer arrives, and may be made from many threads at once: a reader thread
of the client's own matches each response to its caller by id and hands every event notification to `on_event`.
When the daemon goes away, every waiting call fails with `DaemonUnavailableError` and `on_disconnect` runs once.
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from atelierd.daemon.core import CoreEvent
from atelierd.rpc import protocol
from atelierd.rpc.protocol import (
    ERROR_TYPE_BY_NAME,
    EVENT_METHOD,
    EVENT_MODEL_BY_KIND,
    MAX_LINE_BYTES,
    EventNotification,
    RpcError,
    RpcMethod,
)

logger = logging.getLogger(__name__)


class DaemonUnavailableError(ConnectionError):
    """No daemon answers on the socket, or the connection to it was lost."""


class RpcClient:
    """One connection to the daemon."""

    def __init__(
        self,
        socket_path: Path,
        on_event: Callable[[str, CoreEvent], None],
        on_disconnect: Callable[[], None],
    ) -> None:
        """Create the client; `connect` opens the connection.

        Args:
            socket_path: the daemon's socket.
            on_event: called with every event notification, on the client's reader thread.
            on_disconnect: called once, on the reader thread, when the connection ends without `close`.
        """
        self.socket_path = socket_path
        self._on_event = on_event
        self._on_disconnect = on_disconnect
        self._socket: socket.socket | None = None
        self._write_guard = threading.Lock()
        self._pending_guard = threading.Lock()
        self._pending: dict[int, Future[Any]] = {}
        self._request_ids = itertools.count(1)
        self._is_closing = False

    @property
    def is_connected(self) -> bool:
        """Whether the connection is open."""
        return self._socket is not None

    def connect(self) -> None:
        """Open the connection.

        Raises:
            DaemonUnavailableError: no daemon answers on the socket.
        """
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            connection.connect(str(self.socket_path))
        except OSError as error:
            connection.close()
            raise DaemonUnavailableError(f"No Atelier daemon answers on {self.socket_path}: {error}") from error
        self._is_closing = False
        self._socket = connection
        threading.Thread(target=self._read, args=(connection,), name="rpc-reader", daemon=True).start()
        logger.info(f"[RPC client] Connected to {self.socket_path}")

    def close(self) -> None:
        """Close the connection; waiting calls fail, and `on_disconnect` is not called."""
        self._is_closing = True
        connection = self._socket
        self._socket = None
        if connection:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError as error:
                logger.debug(f"[RPC client] Shutting the socket down: {error}")
            connection.close()

    def call[ParamsType: BaseModel, ResultType](
        self,
        method: RpcMethod[ParamsType, ResultType],
        params: ParamsType,
        timeout_s: float | None = None,
    ) -> ResultType:
        """Call one method and return its result.

        Raises:
            DaemonUnavailableError: there is no connection, or it was lost before the answer came.
            RpcError: the daemon failed in a way that is not one of the core's own exceptions.
            KeyError, ValueError, SourceBusyError, ...: the core raised it (see `protocol.ERROR_TYPE_BY_NAME`).
        """
        connection = self._socket
        if connection is None:
            raise DaemonUnavailableError("Not connected to the Atelier daemon")
        request_id = next(self._request_ids)
        future: Future[Any] = Future()
        with self._pending_guard:
            self._pending[request_id] = future
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method.name,
            "params": params.model_dump(mode="json"),
        }
        try:
            with self._write_guard:
                connection.sendall(json.dumps(request).encode() + b"\n")
        except OSError as error:
            with self._pending_guard:
                self._pending.pop(request_id, None)
            raise DaemonUnavailableError(f"Lost the Atelier daemon sending {method.name}: {error}") from error

        response: dict[str, Any] = future.result(timeout=timeout_s)
        if "error" in response:
            raise _error_from(response["error"])
        return method.result.validate_python(response.get("result"))

    # -----------------------------------------------------------------------------
    # Reading
    # -----------------------------------------------------------------------------

    def _read(self, connection: socket.socket) -> None:
        reader = connection.makefile("rb")
        try:
            while line := reader.readline(MAX_LINE_BYTES):
                self._handle_line(line)
        except OSError as error:
            logger.debug(f"[RPC client] Reading stopped: {error}")
        finally:
            reader.close()
            self._fail_pending()
            if self._socket is connection:
                self._socket = None
            if not self._is_closing:
                logger.warning(f"[RPC client] The Atelier daemon on {self.socket_path} went away")
                self._on_disconnect()

    def _handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            logger.warning(f"[RPC client] The daemon sent a line that is not JSON: {error}")
            return
        if message.get("method") == EVENT_METHOD:
            self._deliver_event(message.get("params") or {})
            return
        request_id = message.get("id")
        with self._pending_guard:
            future = self._pending.pop(request_id, None) if isinstance(request_id, int) else None
        if future is None:
            logger.warning(f"[RPC client] A response to no request ({request_id})")
            return
        future.set_result(message)

    def _deliver_event(self, params: dict[str, Any]) -> None:
        try:
            notification = EventNotification.model_validate(params)
            event = EVENT_MODEL_BY_KIND[notification.kind].model_validate(notification.event)
        except ValidationError as error:
            logger.warning(f"[RPC client] An event this client cannot read: {error}")
            return
        try:
            self._on_event(notification.project, event)
        except Exception as error:
            logger.exception(f"[RPC client] Handling a {notification.kind.value} event failed: {error}")

    def _fail_pending(self) -> None:
        with self._pending_guard:
            pending = list(self._pending.values())
            self._pending.clear()
        for future in pending:
            future.set_exception(DaemonUnavailableError("The connection to the Atelier daemon was lost"))


def _error_from(error: dict[str, Any]) -> Exception:
    """The exception a JSON-RPC error stands for."""
    message = str(error.get("message", "The daemon failed"))
    data = error.get("data") or {}
    error_type = ERROR_TYPE_BY_NAME.get(str(data.get("type", "")))
    if error_type:
        return error_type(message)
    return RpcError(int(error.get("code", protocol.INTERNAL_ERROR)), message)
