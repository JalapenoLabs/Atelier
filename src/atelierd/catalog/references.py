"""Collecting a project's reference images from `references.globs`."""

from __future__ import annotations

import logging
from pathlib import Path, PurePosixPath

from wcmatch import glob

from atelierd.catalog.discovery import GLOB_FLAGS
from atelierd.catalog.models import Reference
from atelierd.config import AtelierConfig
from atelierd.constants import IMAGE_SUFFIXES

logger = logging.getLogger(__name__)


def collect_references(project_root: Path, config: AtelierConfig) -> list[Reference]:
    """Return every image matched by `references.globs`, sorted by path and tagged by its folder's name."""
    matched_paths = glob.glob(list(config.references.globs), root_dir=project_root, flags=GLOB_FLAGS)

    references: list[Reference] = []
    for matched_path in sorted(set(matched_paths)):
        posix_path = PurePosixPath(matched_path)
        if posix_path.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        references.append(Reference(path=matched_path, tag=posix_path.parent.name))

    logger.debug(f"[References] {len(references)} reference images in {project_root}")
    return references
