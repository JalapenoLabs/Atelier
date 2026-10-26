"""The child processes a worker runs: one long-lived stream-json `claude`, and one background Blender.

`ClaudeProcess` writes prompts and control requests to Claude Code's stdin as JSON lines and hands every JSON line
it prints to a callback, on a reader thread of its own; another thread keeps the end of its stderr for when it dies.

The background Blender serves the Blender Lab MCP extension on a port of its own
(`blender --background <blend> --online-mode --command blender_mcp --port <n>`). It runs at low priority, on a few
threads, in the same cleaned environment as every other Blender Atelier starts. It is not started with factory
settings: the MCP server is a user extension, which factory settings would leave out.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from atelierd.blender import blender_environment, find_blender
from atelierd.constants import BLENDER_NICENESS, WORKER_BLENDER_THREADS, WORKER_STOP_TIMEOUT_S

logger = logging.getLogger(__name__)

# Lines of a dead process's stderr kept for the reason shown in the chat.
_STDERR_TAIL_LINES = 30
# Variables Claude Code sets in its own children; a worker is a session of its own, not a child of one.
_PARENT_SESSION_VARIABLES = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID")
_PYTHON_ENVIRONMENT_VARIABLES = ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")

# Starts a worker's background Blender on a .blend and a port. Tests replace it with a stub that only listens.
type BlenderLauncher = Callable[[Path, int], subprocess.Popen[bytes]]


class ClaudeProcess:
    """One running `claude --print --input-format stream-json --output-format stream-json`."""

    def __init__(
        self,
        command: Sequence[str],
        cwd: Path,
        on_event: Callable[[dict[str, Any]], None],
        on_exit: Callable[[int, str], None],
    ) -> None:
        """Start the process.

        Args:
            command: the whole command line.
            cwd: its working directory, the project root.
            on_event: called with every JSON line it prints, on its reader thread.
            on_exit: called once with its exit code and the end of its stderr when its output ends.
        """
        self._on_event = on_event
        self._on_exit = on_exit
        self._write_guard = threading.Lock()
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env=worker_environment(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._stderr_thread = threading.Thread(target=self._read_stderr, name="claude-stderr", daemon=True)
        self._stderr_thread.start()
        self._stdout_thread = threading.Thread(target=self._read_stdout, name="claude-stdout", daemon=True)
        self._stdout_thread.start()
        logger.info(f"[Claude] Started pid {self._process.pid} in {cwd}")

    @property
    def is_running(self) -> bool:
        """Whether the process has not exited."""
        return self._process.poll() is None

    def send_prompt(self, text: str) -> None:
        """Start a turn with a user message.

        Raises:
            OSError: the process is gone.
        """
        self._write({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}})

    def interrupt(self) -> None:
        """Ask Claude Code to stop the current turn; it then ends the turn with an error result.

        Raises:
            OSError: the process is gone.
        """
        self._write({"type": "control_request", "request_id": uuid.uuid4().hex, "request": {"subtype": "interrupt"}})

    def stop(self, timeout_s: float = WORKER_STOP_TIMEOUT_S) -> None:
        """End the process: close its stdin, which it exits on once idle, then terminate it, then kill it."""
        with self._write_guard:
            try:
                if self._process.stdin:
                    self._process.stdin.close()
            except OSError as error:
                logger.debug(f"[Claude] Closing stdin of pid {self._process.pid} failed: {error}")
        try:
            self._process.wait(timeout=timeout_s / 2)
        except subprocess.TimeoutExpired:
            logger.info(f"[Claude] pid {self._process.pid} did not exit on a closed stdin; terminating it")
            stop_process(self._process, timeout_s / 2)

    def _write(self, message: dict[str, Any]) -> None:
        line = json.dumps(message) + "\n"
        with self._write_guard:
            if self._process.stdin is None or self._process.stdin.closed:
                raise OSError("Claude Code's stdin is closed")
            self._process.stdin.write(line)
            self._process.stdin.flush()

    def _read_stdout(self) -> None:
        stdout = self._process.stdout
        if stdout is None:
            raise RuntimeError("Claude Code was started without a stdout pipe")
        for line in stdout:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(f"[Claude] pid {self._process.pid} printed a line that is not JSON: {line[:200]!r}")
                continue
            try:
                self._on_event(event)
            except Exception as error:
                # One event the worker could not handle must not stop it hearing the rest of the turn.
                logger.exception(f"[Claude] Handling a {event.get('type')} event failed: {error}")
        return_code = self._process.wait()
        self._stderr_thread.join(timeout=2)
        logger.info(f"[Claude] pid {self._process.pid} exited with {return_code}")
        self._on_exit(return_code, "\n".join(self._stderr_tail))

    def _read_stderr(self) -> None:
        stderr = self._process.stderr
        if stderr is None:
            return
        for line in stderr:
            self._stderr_tail.append(line.rstrip())


def launch_background_blender(blend_file: Path, port: int) -> subprocess.Popen[bytes]:
    """Start the Blender a worker's `blender` MCP server talks to, serving MCP on `port`.

    Raises:
        BlenderMissingError: no Blender is installed.
    """
    blender = find_blender()
    command = [
        # Found here, because Blender's cleaned PATH may no longer hold the folder `nice` lives in.
        shutil.which("nice") or "nice",
        "-n",
        str(BLENDER_NICENESS),
        blender,
        "--background",
        str(blend_file),
        # The MCP extension declares network access, which Blender grants only with online access on.
        "--online-mode",
        "--threads",
        str(WORKER_BLENDER_THREADS),
        "--command",
        "blender_mcp",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    logger.info(f"[Blender] Starting a background Blender on {blend_file.name}, MCP on port {port}")
    return subprocess.Popen(
        command,
        env=blender_environment(Path(blender).parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def wait_for_port(port: int, process: subprocess.Popen[bytes], timeout_s: float) -> None:
    """Block until something listens on 127.0.0.1:`port`.

    Raises:
        RuntimeError: the process exited first.
        TimeoutError: nothing listened within `timeout_s`.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"Blender exited with {process.returncode} before it served MCP on port {port}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"Blender did not serve MCP on port {port} within {timeout_s:.0f} s")


def free_port() -> int:
    """Return a TCP port on 127.0.0.1 that nothing listens on right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def stop_process(process: subprocess.Popen[Any], timeout_s: float) -> None:
    """Terminate a process, and kill it if it is still running after `timeout_s`."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        logger.warning(f"[Process] pid {process.pid} ignored SIGTERM; killing it")
        process.kill()
        process.wait()


def worker_environment() -> dict[str, str]:
    """This daemon's environment, minus what would leak into a worker: its virtual environment and parent session.

    A worker's shell commands run the project's own tooling, such as a README's `blender -b` rebuild, which must
    never find the daemon's virtual environment's Python first (see `atelierd/blender.py`).
    """
    is_virtual_environment = sys.prefix != sys.base_prefix
    virtual_bin = str(Path(sys.prefix) / "bin") if is_virtual_environment else ""
    hidden = {*_PARENT_SESSION_VARIABLES, *_PYTHON_ENVIRONMENT_VARIABLES}
    environment = {name: value for name, value in os.environ.items() if name not in hidden}
    path_entries = environment.get("PATH", "").split(os.pathsep)
    environment["PATH"] = os.pathsep.join(entry for entry in path_entries if entry and entry != virtual_bin)
    return environment
