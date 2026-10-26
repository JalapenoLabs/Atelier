"""Where the daemon lives on this machine, and the two ways it is started: the systemd user unit, or in the background.

`atelierd install` writes `$XDG_CONFIG_HOME/systemd/user/atelierd.service` (and reloads systemd), and enabling it is
left to the user: `systemctl --user enable --now atelierd.service`. The unit runs this installation's own Python with
`-m atelierd run`, with the installing shell's `PATH` baked in (minus any virtual environment), because a user
service's own `PATH` holds none of `~/.local/bin`, where `claude` and `blender-mcp` usually live.

When the window finds no daemon, it offers to start one: through the unit when it is installed and the window wants
the default socket and state, otherwise as a detached `python -m atelierd run` writing its log to
`$XDG_STATE_HOME/atelier/atelierd.log`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from atelierd.constants import (
    DAEMON_LOG_FILE_NAME,
    DEFAULT_HTTP_PORT,
    REGISTRY_DIR_NAME,
    SOCKET_FILE_NAME,
    SOCKET_PATH_ENV,
    SYSTEMD_UNIT_NAME,
)
from atelierd.workers.process import worker_environment

logger = logging.getLogger(__name__)

_UNIT_TEMPLATE = """\
[Unit]
Description=Atelier daemon: the catalog, revisions and Claude Code workers of every Atelier project

[Service]
Type=simple
ExecStart={exec_start}
Environment={path_environment}
Restart=on-failure
RestartSec=5
# SIGTERM to the daemon, which stops its workers' Claude Code and Blender itself; anything left is killed after.
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""


def default_socket_path() -> Path:
    """The GUI's socket: `ATELIER_SOCKET`, else `$XDG_RUNTIME_DIR/atelier.sock`."""
    configured = os.environ.get(SOCKET_PATH_ENV)
    if configured:
        return Path(configured)
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        runtime_dir = f"/run/user/{os.getuid()}"
        logger.warning(f"[System] XDG_RUNTIME_DIR is not set; using {runtime_dir}")
    return Path(runtime_dir) / SOCKET_FILE_NAME


def unit_path() -> Path:
    """Where `atelierd install` writes the systemd user unit."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "systemd" / "user" / SYSTEMD_UNIT_NAME


def log_path() -> Path:
    """Where a daemon started in the background writes its log."""
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / REGISTRY_DIR_NAME / DAEMON_LOG_FILE_NAME


def daemon_command(socket_path: Path | None, state_root: Path | None, http_port: int | None) -> list[str]:
    """The command that runs this installation's daemon in the foreground."""
    command = [sys.executable, "-m", "atelierd", "run"]
    if socket_path:
        command.extend(["--socket", str(socket_path)])
    if state_root:
        command.extend(["--state-root", str(state_root)])
    if http_port is not None and http_port != DEFAULT_HTTP_PORT:
        command.extend(["--http-port", str(http_port)])
    return command


def install_unit(state_root: Path | None, http_port: int | None, is_reload: bool = True) -> Path:
    """Write the systemd user unit and reload systemd's view of it. Enabling and starting it is the user's call.

    Raises:
        OSError: the unit file could not be written.
        subprocess.CalledProcessError: `systemctl --user daemon-reload` failed.
    """
    command = daemon_command(None, state_root, http_port)
    exec_start = " ".join(_systemd_quote(argument) for argument in command)
    path_environment = _systemd_quote(f"PATH={worker_environment()['PATH']}")
    unit = _UNIT_TEMPLATE.format(exec_start=exec_start, path_environment=path_environment)

    target = unit_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(unit, encoding="utf-8")
    logger.info(f"[System] Wrote {target}")
    if is_reload:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=30)
    return target


def start_in_background(socket_path: Path, state_root: Path | None) -> str:
    """Start a daemon that outlives the caller, and say how it was started.

    The unit is used when it is installed and the caller wants what it runs: the default socket and state.

    Raises:
        OSError: the daemon could not be started.
        subprocess.CalledProcessError: systemd refused to start the unit.
    """
    is_default = socket_path == default_socket_path() and state_root is None
    if is_default and unit_path().is_file():
        subprocess.run(["systemctl", "--user", "start", SYSTEMD_UNIT_NAME], check=True, timeout=30)
        logger.info(f"[System] Started {SYSTEMD_UNIT_NAME}")
        return f"Started {SYSTEMD_UNIT_NAME}"

    log_file = log_path()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("ab") as log_handle:
        subprocess.Popen(
            daemon_command(socket_path, state_root, None),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            # Its own session, so closing the window (or its terminal) never takes the daemon and its workers down.
            start_new_session=True,
        )
    logger.info(f"[System] Started atelierd in the background, logging to {log_file}")
    return f"Started atelierd in the background (log: {log_file})"


def _systemd_quote(argument: str) -> str:
    """Quote one argument for a unit file's ExecStart or Environment line."""
    escaped = argument.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'
