"""Tracker credentials, borrowed from Claude Code: the Authorization header of a named `mcpServers` entry.

The reviewer already gave Claude Code a token for each identity (Waystead: `youtrack` acts as Claude, `youtrack-alex`
as the reviewer), so Atelier reads that entry instead of keeping a token of its own. The header is passed through as
it is and never logged, printed or stored.

`atelier.toml` is a project file anyone with the project can edit, so it must not be able to send a token anywhere
it likes: the entry's own server and the configured tracker must be on the same host, or nothing is sent.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
from pathlib import Path

from atelierd.constants import CLAUDE_CONFIG_FILE_NAME, CLAUDE_CONFIG_PATH_ENV
from atelierd.trackers.models import TrackerError

logger = logging.getLogger(__name__)


def load_authorization(entry_name: str, tracker_url: str) -> str:
    """Return the Authorization header of `mcpServers.<entry_name>` in Claude Code's config.

    The config is read afresh on every call, so a token rotated in Claude Code applies on the next refresh.

    Raises:
        TrackerError: the config or the entry is missing, has no header, or serves another host than `tracker_url`.
    """
    config_path = Path(os.environ.get(CLAUDE_CONFIG_PATH_ENV) or Path.home() / CLAUDE_CONFIG_FILE_NAME)
    try:
        claude_config = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise TrackerError(f"{config_path} does not exist, so there are no tracker credentials to read") from error
    except json.JSONDecodeError as error:
        raise TrackerError(f"{config_path} is not valid JSON: {error}") from error

    entry = (claude_config.get("mcpServers") or {}).get(entry_name) or {}
    authorization = (entry.get("headers") or {}).get("Authorization")
    entry_url = entry.get("url") or ""
    if not authorization or not entry_url:
        raise TrackerError(f"mcpServers.{entry_name} in {config_path} needs a url and an Authorization header")

    entry_host = urllib.parse.urlsplit(entry_url).netloc.lower()
    tracker_host = urllib.parse.urlsplit(tracker_url).netloc.lower()
    if entry_host != tracker_host:
        raise TrackerError(
            f"mcpServers.{entry_name} serves {entry_host}, not the tracker's {tracker_host}; "
            "refusing to send its credentials to another host"
        )

    logger.debug(f"[Trackers] Read the credentials of mcpServers.{entry_name} for {tracker_host}")
    return str(authorization)
