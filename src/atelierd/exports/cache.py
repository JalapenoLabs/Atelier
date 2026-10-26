"""Converting exports to `.glb` with headless Blender, cached in `<state>/cache/` by the FBX's sha256.

Qt Quick 3D loads most FBX files directly, but its FBX importer (assimp) is weak on some skeletal meshes and
materials, and it reports no triangle count. So every export the viewer shows is also converted once: Blender writes a
`.glb` the viewer can fall back to and a stats file (`<sha256>.json`) the stats overlay reads. Keying by content means
an unchanged export, even one rewritten byte for byte or renamed, is never converted twice, and the same cache serves
any later job that needs the export in Blender's hands.

Both files are written under temporary names and renamed into place, stats last, so a reader that finds the stats file
always finds a whole `.glb` beside it. Conversions of the same file are serialised within the process.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from pathlib import Path

from pydantic import ValidationError

from atelierd.blender import BlenderError, run_blender_script
from atelierd.constants import CACHE_DIR_NAME
from atelierd.exports.models import ConvertedExport, ExportStats
from atelierd.project import Project

logger = logging.getLogger(__name__)

_CONVERSION_SCRIPT = Path(__file__).parent / "blender_scripts" / "fbx_to_glb.py"
# Bump with any change to what the conversion script writes; a cached result from another version is converted again.
_CONVERTER_VERSION = 1
_HASH_CHUNK_BYTES = 1 << 20

_locks_guard = threading.Lock()
_conversion_locks: dict[str, threading.Lock] = {}


class ExportConversionError(Exception):
    """An export that does not exist or that Blender could not convert."""


def convert_export(project: Project, export_path: str) -> ConvertedExport:
    """Return the export at `export_path` (project-relative) converted to `.glb`, converting it on a cache miss.

    Blocks for as long as Blender takes (about a second for a prop), so call it off the UI thread.

    Raises:
        ExportConversionError: the file is missing or Blender failed on it.
    """
    absolute_path = project.root / export_path
    if not absolute_path.is_file():
        raise ExportConversionError(f"Export {export_path} does not exist in {project.root}")

    sha256 = _hash_file(absolute_path)
    cache_dir = project.state_dir / CACHE_DIR_NAME
    glb_path = cache_dir / f"{sha256}.glb"
    stats_path = cache_dir / f"{sha256}.json"

    with _conversion_lock(sha256):
        stats = _read_cached_stats(stats_path, glb_path)
        if stats is None:
            stats = _convert(absolute_path, cache_dir, glb_path, stats_path)
        else:
            logger.debug(f"[Exports] Cache hit for {export_path} ({sha256[:12]})")

    return ConvertedExport(export=export_path, sha256=sha256, glb_path=glb_path, stats=stats)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _conversion_lock(sha256: str) -> threading.Lock:
    with _locks_guard:
        return _conversion_locks.setdefault(sha256, threading.Lock())


def _read_cached_stats(stats_path: Path, glb_path: Path) -> ExportStats | None:
    """Return the cached stats when a whole result from this converter version is cached, else None."""
    if not (stats_path.is_file() and glb_path.is_file()):
        return None
    try:
        stats = ExportStats.model_validate_json(stats_path.read_text(encoding="utf-8"))
    except ValidationError as error:
        logger.warning(f"[Exports] Unreadable cached stats {stats_path.name}, converting again: {error}")
        return None
    if stats.converter_version != _CONVERTER_VERSION:
        logger.info(f"[Exports] {stats_path.name} is from converter v{stats.converter_version}, converting again")
        return None
    return stats


def _convert(export_file: Path, cache_dir: Path, glb_path: Path, stats_path: Path) -> ExportStats:
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Unique per attempt, and ending in `.glb`, because the glTF exporter appends `.glb` to any other name.
    token = uuid.uuid4().hex
    partial_glb = cache_dir / f"{glb_path.stem}.{token}.partial.glb"
    partial_stats = cache_dir / f"{stats_path.stem}.{token}.partial.json"

    logger.info(f"[Exports] Converting {export_file.name} to .glb with Blender")
    try:
        run_blender_script(
            _CONVERSION_SCRIPT, [str(export_file), str(partial_glb), str(partial_stats), str(_CONVERTER_VERSION)]
        )
        stats = ExportStats.model_validate(json.loads(partial_stats.read_text(encoding="utf-8")))
        partial_glb.replace(glb_path)
        partial_stats.replace(stats_path)
    except (BlenderError, OSError, ValidationError) as error:
        raise ExportConversionError(f"Could not convert {export_file.name}: {error}") from error
    finally:
        partial_glb.unlink(missing_ok=True)
        partial_stats.unlink(missing_ok=True)

    logger.info(f"[Exports] Converted {export_file.name}: {stats.triangles} triangles")
    return stats
