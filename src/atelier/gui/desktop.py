"""Desktop actions the window takes itself: Show in Blender.

Show in Blender opens a windowed Blender on a source's `.blend`, in the cleaned environment every Blender Atelier starts
runs in (see `atelierd/blender.py`). Under Hyprland it opens in the silent special workspace `special:atelier`, so
nothing appears on screen until you go to it; elsewhere it opens as an ordinary window. The window does this rather than
the daemon because it runs in the desktop session (Hyprland's socket, the display), which a systemd user service may
not see.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from atelierd.blender import blender_environment, find_blender

logger = logging.getLogger(__name__)

HYPRLAND_WORKSPACE = "special:atelier"


def show_in_blender(blend_file: Path) -> str:
    """Open `blend_file` in a windowed Blender and say where it went.

    Raises:
        BlenderMissingError: no Blender is installed.
        OSError: Blender could not be started.
        subprocess.CalledProcessError: Hyprland refused the command.
    """
    blender = find_blender()
    environment = blender_environment(Path(blender).parent)
    hyprctl = shutil.which("hyprctl")
    if hyprctl and os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        # Hyprland starts the command in its own environment, so the cleaned PATH travels in the command itself.
        unset = ["-u", "VIRTUAL_ENV", "-u", "PYTHONHOME", "-u", "PYTHONPATH"]
        command = shlex.join(["env", *unset, f"PATH={environment['PATH']}", blender, str(blend_file)])
        lua_command = command.replace("\\", "\\\\").replace("'", "\\'")
        expression = f"hl.exec_cmd('{lua_command}', {{ workspace = '{HYPRLAND_WORKSPACE} silent' }})"
        subprocess.run([hyprctl, "eval", expression], check=True, capture_output=True, timeout=10)
        logger.info(f"[Desktop] Opened {blend_file.name} in Blender on {HYPRLAND_WORKSPACE}")
        return f"in Blender on Hyprland's {HYPRLAND_WORKSPACE} workspace"

    subprocess.Popen(
        [blender, str(blend_file)],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    logger.info(f"[Desktop] Opened {blend_file.name} in Blender")
    return "in Blender"
