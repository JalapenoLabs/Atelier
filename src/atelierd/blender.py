"""Running a Python script in headless Blender.

Every headless Blender job the daemon owns goes through `run_blender_script`: converting exports for the viewer and
rendering standard views. It runs Blender with factory settings (so no user add-on or preference can change a result),
at a lower priority, with a timeout, and turns any failure into a `BlenderError` carrying the end of Blender's log.

Blender built against the system Python (the Arch package) resolves its standard library from the first `python3` on
`PATH`. Any other Python found first, such as mise's or this very virtual environment's under `uv run`, gives it the
wrong standard library and site-packages: `math` or numpy fail to import and the FBX importer breaks. The job's
environment therefore keeps on `PATH` only folders without a `python3`, plus Blender's own folder, and drops the
variables that point Python elsewhere. Every other Blender Atelier starts (a worker's background Blender, Show in
Blender) runs in the same `blender_environment()`.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from atelierd.constants import BLENDER_COMMAND, BLENDER_COMMAND_ENV, BLENDER_NICENESS, BLENDER_TIMEOUT_S

logger = logging.getLogger(__name__)

# How much of Blender's output a BlenderError carries: enough for a traceback, not a whole import log.
_LOG_TAIL_LINES = 40
# Variables that would point Blender's embedded Python at another installation.
_PYTHON_ENVIRONMENT_VARIABLES = ("VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH")


class BlenderError(Exception):
    """Blender is missing, failed, or did not finish in time."""


class BlenderMissingError(BlenderError):
    """No Blender is installed: a fact about the machine, not about the file it was asked to work on."""


def find_blender() -> str:
    """Return the Blender executable, honouring `ATELIER_BLENDER`.

    Raises:
        BlenderMissingError: no such command is installed.
    """
    command = os.environ.get(BLENDER_COMMAND_ENV) or BLENDER_COMMAND
    executable = shutil.which(command)
    if not executable:
        raise BlenderMissingError(f"Blender ('{command}') is not installed or not on PATH; set {BLENDER_COMMAND_ENV}")
    return executable


def run_blender_script(script: Path, script_arguments: list[str], timeout_s: float = BLENDER_TIMEOUT_S) -> str:
    """Run `script` in headless Blender with `script_arguments` after `--`, and return Blender's output.

    Raises:
        BlenderError: Blender is missing, the script raised, or the run timed out.
    """
    blender = find_blender()
    command = [
        # `nice` rather than a preexec hook, which is unsafe in a process that runs threads (the GUI does). Found here,
        # because Blender's cleaned PATH may no longer hold the folder it lives in.
        shutil.which("nice") or "nice",
        "-n",
        str(BLENDER_NICENESS),
        blender,
        "--background",
        "--factory-startup",
        # Without this, an exception in the script still exits 0.
        "--python-exit-code",
        "1",
        "--python",
        str(script),
        "--",
        *script_arguments,
    ]
    logger.debug(f"[Blender] Running {script.name} {' '.join(script_arguments)}")

    try:
        completed = subprocess.run(
            command,
            env=blender_environment(Path(blender).parent),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise BlenderError(f"Blender running {script.name} did not finish within {timeout_s:.0f} s") from error

    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        log_tail = "\n".join(output.splitlines()[-_LOG_TAIL_LINES:])
        raise BlenderError(f"Blender running {script.name} exited with {completed.returncode}:\n{log_tail}")
    return output


def blender_environment(blender_folder: Path) -> dict[str, str]:
    """Return this process's environment cleared of every other Python installation (see the module docstring)."""
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    kept_entries = [
        entry
        for entry in path_entries
        if entry and (Path(entry) == blender_folder or not (Path(entry) / "python3").exists())
    ]
    environment = {name: value for name, value in os.environ.items() if name not in _PYTHON_ENVIRONMENT_VARIABLES}
    environment["PATH"] = os.pathsep.join(kept_entries)
    return environment
