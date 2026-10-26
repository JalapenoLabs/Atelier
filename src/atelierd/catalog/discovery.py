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
    # Roots may overlap (`SourceArt` and `SourceArt/Weapons`); each folder is still read once.
    seen_folders: set[Path] = set()

    for root in config.roots:
        root_folder = project_root / root
        if not root_folder.is_dir():
            logger.warning(f"[Discovery] Root '{root}' does not exist under {project_root}, skipping it")
            continue

        for folder, file_names in _walk_files(root_folder):
            resolved_folder = folder.resolve()
            if resolved_folder in seen_folders:
                continue
            seen_folders.add(resolved_folder)

            blend_names = sorted(name for name in file_names if name.endswith(SOURCE_SUFFIX))
            if not blend_names:
                continue

            # In a folder of several kits, a file named after one kit (`Hull_preview.png`) is that kit's alone, and
            # the longest name wins, so `Hull_Deck_preview.png` goes to `Hull_Deck.blend` rather than `Hull.blend`.
            stems = [blend_name.removesuffix(SOURCE_SUFFIX) for blend_name in blend_names]
            files_by_owner: dict[str | None, list[Path]] = {}
            for file in _collect_folder_files(folder, file_names):
                owners = [stem for stem in stems if file.name.startswith((f"{stem}_", f"{stem}."))]
                owner = max(owners, key=len, default=None)
                files_by_owner.setdefault(owner, []).append(file)

            builder_by_stem = _pair_builders(stems, file_names)
            for stem, blend_name in zip(stems, blend_names, strict=True):
                own_files = [*files_by_owner.get(stem, []), *files_by_owner.get(None, [])]
                builder_name = builder_by_stem.get(stem)
                source = _build_source(project_root, config, folder, blend_name, own_files, builder_name)
                sources.append(source)

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
    builder_name: str | None,
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
        builder=_find_builder(project_root, folder, blend_name, builder_name, readme),
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


def _pair_builders(stems: list[str], file_names: list[str]) -> dict[str, str]:
    """Pair the `.blend` stems of one folder with the `build_*.py` scripts beside them.

    One kit alone with one script pairs with it whatever its name. Otherwise each script goes to the kit it is named
    after, `build_deck_kit.py` or `build_deck_kit_*.py` to `DeckKit.blend`, and the longest kit name wins, so
    `build_dining_spa_rooms.py` goes to `DiningSpa.blend` rather than `Dining.blend`. A kit then takes its exactly
    named script, or its only script. A kit no script is named after gets none here.
    """
    builder_names = sorted(name for name in file_names if name.startswith(BUILDER_PREFIX) and name.endswith(".py"))
    if len(stems) == 1 and len(builder_names) == 1:
        return {stems[0]: builder_names[0]}

    snake_by_stem = {stem: _CAMEL_BOUNDARY_PATTERN.sub("_", stem).lower() for stem in stems}
    candidates_by_stem: dict[str, list[str]] = {}
    for builder_name in builder_names:
        script_name = builder_name.removeprefix(BUILDER_PREFIX).removesuffix(".py")
        owners = [
            stem
            for stem, snake_stem in snake_by_stem.items()
            if script_name == snake_stem or script_name.startswith(f"{snake_stem}_")
        ]
        owner = max(owners, key=lambda stem: len(snake_by_stem[stem]), default=None)
        if owner is None:
            logger.debug(f"[Discovery] Builder {builder_name} is named after none of {stems}")
            continue
        candidates_by_stem.setdefault(owner, []).append(builder_name)

    builder_by_stem: dict[str, str] = {}
    for stem, candidates in candidates_by_stem.items():
        exact_name = f"{BUILDER_PREFIX}{snake_by_stem[stem]}.py"
        if exact_name in candidates:
            builder_by_stem[stem] = exact_name
        elif len(candidates) == 1:
            builder_by_stem[stem] = candidates[0]
        else:
            logger.debug(f"[Discovery] Several builders could build {stem}.blend: {candidates}")
    return builder_by_stem


def _find_builder(
    project_root: Path,
    folder: Path,
    blend_name: str,
    builder_name: str | None,
    readme: Path,
) -> str | None:
    """Return a source's builder: the script paired with it in its folder, else its README's rebuild command."""
    if builder_name:
        return (folder / builder_name).relative_to(project_root).as_posix()

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
