"""Desktop notifications for approvals, through `notify-send`.

A daemon with no desktop session (no `notify-send`, or no session bus) still works: the approval card waits in the
window either way, and the missing notification is logged.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import PurePosixPath

from atelierd.workers.models import Approval

logger = logging.getLogger(__name__)

_NOTIFY_TIMEOUT_S = 5


def notify_approval(approval: Approval) -> None:
    """Tell the reviewer a worker is waiting on an approval."""
    executable = shutil.which("notify-send")
    if not executable:
        logger.info(f"[Desktop] No notify-send; the approval for {approval.source} waits in the window only")
        return
    title = f"Atelier: {PurePosixPath(approval.source).stem} needs your approval"
    body = f"{approval.tool_name}: {approval.tool_summary}\n{approval.reason}"
    try:
        subprocess.run(
            [executable, "--app-name=Atelier", "--icon=dialog-question", title, body],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=_NOTIFY_TIMEOUT_S,
            check=True,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        logger.warning(f"[Desktop] notify-send failed for the approval on {approval.source}: {error}")
