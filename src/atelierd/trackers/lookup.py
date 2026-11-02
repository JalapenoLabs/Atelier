"""Opening a project's tracker by the `kind` its `[review.tracker]` table names."""

from __future__ import annotations

from collections.abc import Callable

from atelierd.config import TrackerConfig, TrackerKind
from atelierd.trackers.base import Tracker
from atelierd.trackers.youtrack import YouTrackTracker

# One implementation per kind; another tracker is one more module and one more line here.
TRACKER_BY_KIND: dict[TrackerKind, Callable[[TrackerConfig], Tracker]] = {
    TrackerKind.YOUTRACK: YouTrackTracker,
}
if set(TRACKER_BY_KIND) != set(TrackerKind):
    raise RuntimeError("Every tracker kind needs its implementation in TRACKER_BY_KIND")


def open_tracker(config: TrackerConfig) -> Tracker:
    """Return the tracker a project's config names, with its credentials read.

    Raises:
        TrackerError: its credentials cannot be read.
    """
    return TRACKER_BY_KIND[config.kind](config)
