"""Finding a project's sources and their files by convention.

The conventions are the design of record in `docs/catalog.md`:

- every `*.blend` under the configured roots is a source, outside `_`-prefixed folders and `__pycache__`;
- its files are those in its own folder plus the trees of `Export/`, `Exports/`, `Review/` and `Renders/` beside it,
  except, in a folder of several `.blend` files, the files named after one of the others;
- of those, `*.fbx` files are exports, and images in `Review/` or `Renders/` or named `*_preview*` or
  `*_contact_sheet*` are renders;
- `[sources."…"].outputs` in `atelier.toml` replaces that file set for a kit whose outputs live elsewhere.

Discovery only reads the filesystem. It never writes into the project.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

from wcmatch import glob

from atelierd.catalog.models import DiscoveredSource
from atelierd.config import AtelierConfig
from atelierd.constants import (
    BUILDER_PREFIX,
    EXPORT_SUFFIX,
    IMAGE_SUFFIXES,
    OUTPUT_FOLDER_NAMES,
    PRIVATE_FOLDER_PREFIX,
    README_FILE_NAME,
    RENDER_FOLDER_NAMES,
    RENDER_NAME_GLOBS,
    SOURCE_SUFFIX,
    STATE_DIR_NAME,
)

logger = logging.getLogger(__name__)

# `**` crosses folders and `{a,b}` expands, matching the globs documented for `atelier.toml`.
GLOB_FLAGS = glob.GLOBSTAR | glob.BRACE | glob.NODIR

# A rebuild command in a README names its script with `--python <path>` (or `-P <path>`).
_REBUILD_SCRIPT_PATTERN = re.compile(r"(?:--python|-P)\s+(\S+\.py)\b")
# Splits `DeckKit` into `Deck_Kit` so it can be compared with `build_deck_kit.py`.
_CAMEL_BOUNDARY_PATTERN = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def discover_sources(project_root: Path, config: AtelierConfig) -> tuple[list[DiscoveredSource], list[str]]:
    """Find every source under the configured roots.

    Returns:
        The sources sorted by path, and the `[sources."…"]` override keys that matched no source.
    """
    sources: list[DiscoveredSource] = []

    for root in config.roots:
        root_folder = project_root / root
        if not root_folder.is_dir():
            logger.warning(f"[Discovery] Root '{root}' does not exist under {project_root}, skipping it")
            continue

        for folder, file_names in _walk_files(root_folder):
            blend_names = sorted(name for name in file_names if name.endswith(SOURCE_SUFFIX))
            if not blend_names:
                continue

            folder_files = _collect_folder_files(folder, file_names)
            for blend_name in blend_names:
                # In a folder of several kits, a file named after one kit (`Hull_preview.png`) is that kit's alone.
                sibling_prefixes = tuple(
                    f"{sibling_name.removesuffix(SOURCE_SUFFIX)}{separator}"
                    for sibling_name in blend_names
                    if sibling_name != blend_name
                    for separator in ("_", ".")
                )
                own_files = [file for file in folder_files if not file.name.startswith(sibling_prefixes)]
                sources.append(_build_source(project_root, config, folder, blend_name, own_files))

    sources.sort(key=lambda source: source.path)
    discovered_paths = {source.path for source in sources}
    unmatched_overrides = sorted(path for path in config.sources if path not in discovered_paths)
    for override_path in unmatched_overrides:
        logger.warning(f"[Discovery] atelier.toml overrides '{override_path}', which is not a discovered source")

    return sources, unmatched_overrides


def is_walkable_folder(folder_name: str) -> bool:
    """Whether discovery descends into a folder of this name."""
    is_private = folder_name.startswith(PRIVATE_FOLDER_PREFIX)
    return not (is_private or folder_name == STATE_DIR_NAME)


# -----------------------------------------------------------------------------
# Walking the tree
# -----------------------------------------------------------------------------


def _walk_files(top: Path) -> Iterable[tuple[Path, list[str]]]:
    """Yield each walkable folder under `top` with the names of the files directly in it."""
    for folder, folder_names, file_names in os.walk(top):
        # Pruning in place stops os.walk from descending; `__pycache__` starts with `_`, so this covers it too.
        folder_names[:] = [name for name in folder_names if is_walkable_folder(name)]
        yield Path(folder), file_names


def _collect_folder_files(folder: Path, file_names: list[str]) -> list[Path]:
    """Return the conventional files of the sources in `folder`: its own files plus its output folders' trees.

    `.blend` files and Blender's `.blend1`, `.blend2` and `.blend@` backups are never part of a source's files.
    """
    files = [folder / name for name in file_names if SOURCE_SUFFIX not in name]

    for output_folder_name in OUTPUT_FOLDER_NAMES:
        output_folder = folder / output_folder_name
        if not output_folder.is_dir():
            continue
        for nested_folder, nested_file_names in _walk_files(output_folder):
            files.extend(nested_folder / name for name in nested_file_names)

    return files


# -----------------------------------------------------------------------------
# Building one source
# -----------------------------------------------------------------------------


def _build_source(
    project_root: Path,
    config: AtelierConfig,
    folder: Path,
    blend_name: str,
    folder_files: list[Path],
) -> DiscoveredSource:
    """Assemble one source from its folder's files, honouring any `atelier.toml` override."""
    blend_path = (folder / blend_name).relative_to(project_root).as_posix()
    override = config.sources.get(blend_path)
    override_outputs = override.outputs if override else None

    if override_outputs is not None:
        output_paths = glob.glob(list(override_outputs), root_dir=project_root, flags=GLOB_FLAGS)
        if not output_paths:
            logger.warning(f"[Discovery] Output override for '{blend_path}' matched no files")
    else:
        output_paths = [file.relative_to(project_root).as_posix() for file in folder_files]

    exports: list[str] = []
    renders: list[str] = []
    for output_path in output_paths:
        posix_path = PurePosixPath(output_path)
        suffix = posix_path.suffix.lower()
        if suffix == EXPORT_SUFFIX:
            exports.append(output_path)
        elif suffix in IMAGE_SUFFIXES and _is_render(posix_path):
            renders.append(output_path)

    readme = folder / README_FILE_NAME
    readme_path = readme.relative_to(project_root).as_posix() if readme.is_file() else None

    return DiscoveredSource(
        path=blend_path,
        builder=_find_builder(project_root, folder, blend_name, readme),
        readme=readme_path,
        exports=tuple(sorted(set(exports))),
        renders=tuple(sorted(set(renders))),
        has_output_override=override_outputs is not None,
    )


def _is_render(image_path: PurePosixPath) -> bool:
    """Whether an image is a render rather than a texture, mask or other input."""
    if RENDER_FOLDER_NAMES.intersection(image_path.parent.parts):
        return True
    return any(fnmatch.fnmatchcase(image_path.stem, pattern) for pattern in RENDER_NAME_GLOBS)


def _find_builder(project_root: Path, folder: Path, blend_name: str, readme: Path) -> str | None:
    """Find the script that generates a .blend.

    A folder with one `build_*.py` has one builder. A folder holding several kits pairs each `.blend` with the
    builder named after it: `DeckKit.blend` with `build_deck_kit.py`, or, when no exact name exists, the one
    `build_deck_kit_*.py`. Failing both, the README's rebuild command names the script.
    """
    builder_names = sorted(
        name
        for name in os.listdir(folder)
        if name.startswith(BUILDER_PREFIX) and name.endswith(".py") and (folder / name).is_file()
    )

    if len(builder_names) == 1:
        return (folder / builder_names[0]).relative_to(project_root).as_posix()

    if builder_names:
        stem = blend_name.removesuffix(SOURCE_SUFFIX)
        snake_stem = _CAMEL_BOUNDARY_PATTERN.sub("_", stem).lower()
        exact_name = f"{BUILDER_PREFIX}{snake_stem}.py"
        prefixed_names = [name for name in builder_names if name.startswith(f"{BUILDER_PREFIX}{snake_stem}_")]
        if exact_name in builder_names:
            return (folder / exact_name).relative_to(project_root).as_posix()
        if len(prefixed_names) == 1:
            return (folder / prefixed_names[0]).relative_to(project_root).as_posix()
        logger.debug(f"[Discovery] No builder in {folder} is named after {blend_name}: {builder_names}")

    if readme.is_file():
        return _builder_from_readme(project_root, readme)

    logger.debug(f"[Discovery] No builder found for {folder / blend_name}")
    return None


def _builder_from_readme(project_root: Path, readme: Path) -> str | None:
    """Return the script a README's rebuild command runs, when it names one that exists in the project."""
    match = _REBUILD_SCRIPT_PATTERN.search(readme.read_text(encoding="utf-8", errors="replace"))
    if not match:
        logger.debug(f"[Discovery] {readme} has no rebuild command")
        return None

    # Rebuild commands are written to run from the project root; a path relative to the README is the fallback.
    script_name = match.group(1)
    for base in (project_root, readme.parent):
        script = (base / script_name).resolve()
        if script.is_file() and script.is_relative_to(project_root.resolve()):
            return script.relative_to(project_root.resolve()).as_posix()

    logger.warning(f"[Discovery] {readme} names builder '{script_name}', which does not exist in the project")
    return None
