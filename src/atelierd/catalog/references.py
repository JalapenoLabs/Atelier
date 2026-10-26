"""A project's reference images: collecting them from `references.globs`, and adding new ones to the drop folder."""

from __future__ import annotations

import filecmp
import logging
import shutil
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


def add_reference(project_root: Path, config: AtelierConfig, image: Path) -> str:
    """Copy an image into `references.drop_folder` and return its project-relative path.

    A file of the same name already there is never overwritten: the same image is not copied again, and a different
    one is stored under the next free name (`ref-2.png`, `ref-3.png`).

    Raises:
        ValueError: `image` is not a file with an image suffix.
    """
    if not image.is_file() or image.suffix.lower() not in IMAGE_SUFFIXES:
        raise ValueError(f"{image} is not an image Atelier can use as a reference")

    drop_folder = project_root / config.references.drop_folder
    drop_folder.mkdir(parents=True, exist_ok=True)

    target = drop_folder / image.name
    copy_number = 1
    while target.exists():
        if filecmp.cmp(target, image, shallow=False):
            logger.info(f"[References] {image.name} is already in {config.references.drop_folder}")
            return target.relative_to(project_root).as_posix()
        copy_number += 1
        target = drop_folder / f"{image.stem}-{copy_number}{image.suffix}"

    shutil.copy2(image, target)
    relative_path = target.relative_to(project_root).as_posix()
    logger.info(f"[References] Added {image} as {relative_path}")
    return relative_path
