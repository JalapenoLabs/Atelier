"""The `atelierd` command line.

`atelierd run` runs the daemon in the foreground until SIGINT or SIGTERM: the GUI's JSON-RPC on
`$XDG_RUNTIME_DIR/atelier.sock` (`--socket`), the worker-side MCP on http://127.0.0.1:47817 (`--http-port`), and
every project's catalog, revisions and workers. `--state-root` keeps each project's state outside the projects.

`atelierd install` writes the systemd user unit that runs `atelierd run`, and reloads systemd. It never enables or
starts the unit; `systemctl --user enable --now atelierd.service` does that.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import subprocess
import sys
from pathlib import Path

from atelierd.constants import DEFAULT_HTTP_PORT, SYSTEMD_UNIT_NAME
from atelierd.daemon.runner import Daemon, DaemonOptions
from atelierd.daemon.system import default_socket_path, install_unit
from atelierd.registry import default_registry_path
from atelierd.rpc.server import SocketInUseError

logger = logging.getLogger(__name__)

_LOG_LEVEL_BY_VERBOSITY = {
    0: logging.INFO,
    1: logging.DEBUG,
}


def main(argv: list[str] | None = None) -> int:
    """Run the command line and return its exit code."""
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    log_level = _LOG_LEVEL_BY_VERBOSITY[min(arguments.verbose, 1)]
    logging.basicConfig(level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    command_by_name = {"run": _run, "install": _install}
    return command_by_name[arguments.command](arguments)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="atelierd", description="Atelier's daemon.")
    # On every subcommand, so `atelierd run -v` works as well as `atelierd -v run`.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="count", default=0, help="Log debug detail too.")
    common.add_argument(
        "--state-root",
        type=Path,
        help="Keep each project's state in <state-root>/<project name>/ instead of <project>/.atelier/.",
    )
    common.add_argument(
        "--http-port",
        type=int,
        default=DEFAULT_HTTP_PORT,
        help=f"The HTTP front door's port on 127.0.0.1 (default {DEFAULT_HTTP_PORT}).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", parents=[common], help="Run the daemon in the foreground.")
    run_parser.add_argument("--socket", type=Path, help="The JSON-RPC socket (default $XDG_RUNTIME_DIR/atelier.sock).")

    install_parser = subparsers.add_parser(
        "install",
        parents=[common],
        help=f"Write the systemd user unit {SYSTEMD_UNIT_NAME} (it is not enabled or started).",
    )
    install_parser.add_argument("--no-reload", action="store_true", help="Do not run systemctl --user daemon-reload.")
    return parser


def _run(arguments: argparse.Namespace) -> int:
    options = DaemonOptions(
        socket_path=arguments.socket or default_socket_path(),
        registry_path=default_registry_path(),
        http_port=arguments.http_port,
        state_root=arguments.state_root.resolve() if arguments.state_root else None,
    )

    async def serve() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_number in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signal_number, stop_event.set)
        await Daemon(options).run(stop_event)

    try:
        asyncio.run(serve())
    except SocketInUseError as error:
        print(f"atelierd: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        print(f"atelierd: could not start: {error}", file=sys.stderr)
        return 1
    return 0


def _install(arguments: argparse.Namespace) -> int:
    state_root = arguments.state_root.resolve() if arguments.state_root else None
    try:
        unit = install_unit(state_root, arguments.http_port, is_reload=not arguments.no_reload)
    except (OSError, subprocess.CalledProcessError) as error:
        print(f"atelierd: could not install the unit: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {unit}")
    print(f"Start it now and at every login with: systemctl --user enable --now {SYSTEMD_UNIT_NAME}")
    return 0
