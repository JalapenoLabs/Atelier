"""Standard renders: one export's fixed set of views, rendered by headless Blender into a revision's render folder.

A revision's renders live in `<state>/renders/<revision id>/`: `<name>-<view>.png` for each view, and `<name>.json`
holding the renderer's version and the hash of the export it rendered (or why Blender failed). `<name>` is the
export's file stem. The JSON is written last, so a set with its JSON is complete; one from another renderer version
counts as never rendered.

Renders follow the export cache's pattern: rendered under temporary names in a partial folder, then renamed into
place, metadata last. An export whose bytes a set already exists for (it did not change, or a restore brought it
back) is not rendered again: its files are hard-linked into the new revision's folder, so every revision's folder is
whole on its own and a prune can delete folders without looking inside them.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, ValidationError

from atelierd.blender import BlenderError, run_blender_script
from atelierd.revisions.models import RenderState, StandardRenderSet

logger = logging.getLogger(__name__)

RENDERS_DIR_NAME = "renders"
STANDARD_VIEWS = ("hero", "front", "side", "top", "clay")

_RENDER_SCRIPT = Path(__file__).parent / "blender_scripts" / "standard_renders.py"
# Bump with any change to how the script renders; sets from another version count as never rendered.
RENDERER_VERSION = 1


class _RenderMetadata(BaseModel):
    """`<name>.json`, beside a set's PNGs."""

    model_config = ConfigDict(frozen=True)

    renderer_version: int
    export: str
    export_sha256: str
    error: str = ""


def render_names(export_paths: list[str]) -> dict[str, str]:
    """Name each export's render files by its stem, adding the folder for stems two exports share."""
    stems = [PurePosixPath(path).stem for path in export_paths]
    names: dict[str, str] = {}
    for path, stem in zip(export_paths, stems, strict=True):
        if stems.count(stem) > 1:
            names[path] = "_".join([*PurePosixPath(path).parent.parts, stem])
        else:
            names[path] = stem
    return names


def read_render_set(folder: Path, revision_id: int, export: str, export_sha256: str, name: str) -> StandardRenderSet:
    """Return the set in a revision's render folder: done, failed, or missing when absent, stale or for other bytes."""
    missing = StandardRenderSet(
        revision_id=revision_id,
        export=export,
        export_sha256=export_sha256,
        state=RenderState.MISSING,
    )
    metadata_path = folder / f"{name}.json"
    if not metadata_path.is_file():
        return missing
    try:
        metadata = _RenderMetadata.model_validate_json(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as error:
        logger.warning(f"[Renders] Unreadable {metadata_path}, treating the set as missing: {error}")
        return missing
    if metadata.renderer_version != RENDERER_VERSION or metadata.export_sha256 != export_sha256:
        logger.debug(f"[Renders] {metadata_path} is from another renderer version or export")
        return missing

    if metadata.error:
        return missing.model_copy(update={"state": RenderState.FAILED, "error": metadata.error})
    images = {view: folder / f"{name}-{view}.png" for view in STANDARD_VIEWS}
    return missing.model_copy(update={"state": RenderState.DONE, "images": images})


def render_set(
    export_file: Path,
    texture_folder: Path,
    folder: Path,
    revision_id: int,
    export: str,
    export_sha256: str,
    name: str,
) -> StandardRenderSet:
    """Render every standard view of one export into a revision's folder. Blocks for as long as Blender takes.

    A Blender failure is recorded in the set's metadata and returned as a failed set rather than raised: the same
    bytes would fail the same way again, so they are not retried until the renderer changes.
    """
    folder.mkdir(parents=True, exist_ok=True)
    partial_folder = folder / f".partial-{uuid.uuid4().hex}"
    partial_folder.mkdir()
    error_text = ""
    logger.info(f"[Renders] Rendering {export} for revision {revision_id}")
    try:
        try:
            run_blender_script(_RENDER_SCRIPT, [str(export_file), str(partial_folder), name, str(texture_folder)])
            for view in STANDARD_VIEWS:
                (partial_folder / f"{name}-{view}.png").replace(folder / f"{name}-{view}.png")
        except (BlenderError, OSError) as error:
            logger.error(f"[Renders] Could not render {export} for revision {revision_id}: {error}")
            error_text = str(error)
        _write_metadata(folder / f"{name}.json", export, export_sha256, error_text)
    finally:
        shutil.rmtree(partial_folder, ignore_errors=True)
    return read_render_set(folder, revision_id, export, export_sha256, name)


def link_render_set(source_folder: Path, source_name: str, target_folder: Path, target_name: str) -> None:
    """Hard-link a complete set into another revision's folder (copying where links are not possible), JSON last.

    The names differ when a namesake export came or went between the two revisions (see `render_names`).
    """
    target_folder.mkdir(parents=True, exist_ok=True)
    file_suffixes = [f"-{view}.png" for view in STANDARD_VIEWS]
    file_suffixes.append(".json")
    for file_suffix in file_suffixes:
        source_file = source_folder / f"{source_name}{file_suffix}"
        if not source_file.is_file():
            # A failed set has only its JSON.
            continue
        target_file = target_folder / f"{target_name}{file_suffix}"
        target_file.unlink(missing_ok=True)
        try:
            os.link(source_file, target_file)
        except OSError:
            shutil.copyfile(source_file, target_file)


def _write_metadata(metadata_path: Path, export: str, export_sha256: str, error_text: str) -> None:
    metadata = _RenderMetadata(
        renderer_version=RENDERER_VERSION,
        export=export,
        export_sha256=export_sha256,
        error=error_text,
    )
    partial_path = metadata_path.with_name(f".{metadata_path.name}.{uuid.uuid4().hex}.partial")
    partial_path.write_text(json.dumps(metadata.model_dump(), indent=2), encoding="utf-8")
    partial_path.replace(metadata_path)
