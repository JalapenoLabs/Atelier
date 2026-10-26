"""Grouping a source's renders by stem for the render gallery.

Projects name their renders `<subject>_<view>` (`Carbine_Front`, `Carbine_Side`), `<subject>_<n>`
(`CB_CargoBooster_2` beside `CB_CargoBooster`), or `<subject>_<kind>_<variant>` (`Hull_preview_port` beside
`Hull_preview`), and a subject's own name can hold underscores (`Hair_Braid_Front`, `Hair_Braid_Back`). No fixed list
of view words covers that, so the stem is found from the set itself: a render's stem is the longest `_`-separated
prefix of its name (the whole name included) that another render's name equals or continues. A render that shares no
prefix with another is a group of its own.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict


class RenderGroup(BaseModel):
    """Renders sharing a stem, in path order."""

    model_config = ConfigDict(frozen=True)

    stem: str
    renders: tuple[str, ...]


def group_renders_by_stem(render_paths: Iterable[str]) -> list[RenderGroup]:
    """Group project-relative render paths by stem; groups are sorted by stem."""
    names_by_path = {path: PurePosixPath(path).stem for path in render_paths}
    names = list(names_by_path.values())

    paths_by_stem: dict[str, list[str]] = {}
    for path, name in names_by_path.items():
        stem = _shared_stem(name, names)
        paths_by_stem.setdefault(stem, []).append(path)

    return [
        RenderGroup(stem=stem, renders=tuple(sorted(paths)))
        for stem, paths in sorted(paths_by_stem.items(), key=lambda item: item[0].lower())
    ]


def _shared_stem(name: str, names: list[str]) -> str:
    """Return the longest prefix of `name`, at a `_` boundary, that another name equals or continues."""
    parts = name.split("_")
    other_names = list(names)
    other_names.remove(name)

    for part_count in range(len(parts), 0, -1):
        prefix = "_".join(parts[:part_count])
        if any(other == prefix or other.startswith(f"{prefix}_") for other in other_names):
            return prefix
    return name
