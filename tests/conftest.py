"""Shared fixtures: a disposable copy of the fixture project, Qt for the GUI tests, and a daemon run in-process.

The daemon fixture runs a real `atelierd` (core, JSON-RPC socket, worker-side MCP over HTTP) on a thread of its own,
with Claude Code replaced by `fixtures/fake_claude.py` and each worker's Blender by a process that only listens on its
port, so the worker tests run in CI with neither installed.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

from atelierd.daemon.runner import Daemon, DaemonOptions
from atelierd.rpc.client import RpcClient
from atelierd.workers import Approval

FIXTURE_PROJECT = Path(__file__).parent / "fixtures" / "project"
FAKE_CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"

# Listens on the port it is given until killed: all a worker needs of its Blender in these tests.
_FAKE_BLENDER = """
import socket, sys, time
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", int(sys.argv[1])))
server.listen()
time.sleep(3600)
"""


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Copy the fixture project into a temporary folder, so tests can scan and change it freely.

    `__pycache__` is created here rather than committed, because the repository ignores those folders.
    """
    root = tmp_path / "FixtureProject"
    shutil.copytree(FIXTURE_PROJECT, root)
    cache_folder = root / "SourceArt" / "Weapons" / "Carbine" / "__pycache__"
    cache_folder.mkdir()
    (cache_folder / "Cached.blend").touch()
    return root


@pytest.fixture(scope="session")
def qt_application() -> QGuiApplication:
    """One Qt application for every GUI test, on the `offscreen` platform so no display is needed.

    `offscreen` has no 3D graphics API, so Qt Quick 3D renders nothing there; GUI tests cover the view-model and
    that the QML loads, not pixels.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QGuiApplication.instance()
    if isinstance(existing, QGuiApplication):
        return existing
    return QGuiApplication([])


WaitUntil = Callable[[Callable[[], bool]], None]


@pytest.fixture
def wait_until(qt_application: QGuiApplication) -> WaitUntil:
    """Return a function that processes Qt events until a condition holds, failing the test after 10 s."""

    def wait(condition: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 10
        while not condition():
            if time.monotonic() > deadline:
                pytest.fail(f"Timed out waiting for {condition}")
            QCoreApplication.processEvents()
            time.sleep(0.005)

    return wait


def launch_fake_blender(blend_file: Path, port: int) -> subprocess.Popen[bytes]:
    """Stand in for a worker's background Blender: a process that serves nothing but listens on `port`."""
    return subprocess.Popen([sys.executable, "-c", _FAKE_BLENDER, str(port)])


class RunningDaemon:
    """A daemon on a thread of its own, with what the tests need to drive it."""

    def __init__(self, options: DaemonOptions) -> None:
        self.options = options
        self.notifications: list[Approval] = []
        self.blender_launches: list[tuple[Path, int]] = []
        self.daemon = Daemon(options, launch_blender=self._launch_blender, notify=self.notifications.append)
        self._loop = asyncio.new_event_loop()
        self._stop_event: asyncio.Event | None = None
        self._thread = threading.Thread(target=self._run, name="test-atelierd", daemon=True)

    @property
    def socket_path(self) -> Path:
        return self.options.socket_path

    def start(self) -> None:
        self._thread.start()
        if not self.daemon.is_ready.wait(30):
            raise TimeoutError("The test daemon did not start")

    def stop(self) -> None:
        if not self._thread.is_alive():
            return
        if self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        self._thread.join(30)

    def client(self, on_event: Callable[[str, object], None] | None = None) -> RpcClient:
        """A connected client; events go to `on_event`, or nowhere."""
        client = RpcClient(
            self.socket_path, on_event=on_event or (lambda project, event: None), on_disconnect=lambda: None
        )
        client.connect()
        return client

    def _launch_blender(self, blend_file: Path, port: int) -> subprocess.Popen[bytes]:
        self.blender_launches.append((blend_file, port))
        return launch_fake_blender(blend_file, port)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)

        async def main() -> None:
            self._stop_event = asyncio.Event()
            await self.daemon.run(self._stop_event)

        try:
            self._loop.run_until_complete(main())
        finally:
            self._loop.close()


@pytest.fixture
def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RunningDaemon]:
    """A running daemon with every project's state under `tmp_path`, its workers running the fake Claude Code.

    Its socket sits in a short folder under /tmp: a Unix socket's path is capped near 107 bytes, which a pytest
    `tmp_path` can pass.
    """
    monkeypatch.setenv("ATELIER_CLAUDE", str(FAKE_CLAUDE))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "fake-claude.log"))
    socket_folder = Path(tempfile.mkdtemp(prefix="atelier-", dir="/tmp"))
    options = DaemonOptions(
        socket_path=socket_folder / "atelier.sock",
        registry_path=tmp_path / "registry.toml",
        http_port=0,
        state_root=tmp_path / "state",
    )
    running = RunningDaemon(options)
    running.start()
    try:
        yield running
    finally:
        running.stop()
        shutil.rmtree(socket_folder, ignore_errors=True)
