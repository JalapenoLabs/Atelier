"""The source tree as the left pane shows it: folders and sources flattened into rows, filtered and collapsible.

The tree mirrors the project's folders with two simplifications that keep it short:

- a top folder every source sits under (`SourceArt/`) is left out;
- a folder that holds nothing but one source of its own name (`Carbine/Carbine.blend`) is shown as that source.

A filter keeps the sources whose path contains the text (ignoring case) and that have the status, and the folders
leading to them. While text is being filtered every folder shows open, so a match is never hidden in a collapsed one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from atelierd.catalog import Source, SourceStatus

logger = logging.getLogger(__name__)


class SourceRow(BaseModel):
    """One row of the tree: a folder or a source."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The folder's or the source's project-relative path; unique per row.")
    name: str
    depth: int
    is_folder: bool
    is_expanded: bool = False
    status: str = Field(default="", description="The source's status; empty for a folder.")
    export_count: int = 0


class _Folder:
    """A folder while the tree is being built."""

    def __init__(self, path: str, name: str) -> None:
        self.path = path
        self.name = name
        self.folders: dict[str, _Folder] = {}
        self.sources: list[Source] = []


def build_source_rows(
    sources: Iterable[Source],
    filter_text: str = "",
    status: SourceStatus | None = None,
    collapsed: frozenset[str] = frozenset(),
) -> list[SourceRow]:
    """Return the rows of the tree, in display order, for the sources that pass the filter."""
    all_sources = list(sources)
    needle = filter_text.strip().lower()
    matching_sources = [
        source
        for source in all_sources
        if needle in source.path.lower() and (status is None or source.status is status)
    ]
    if not matching_sources:
        logger.debug(f"[Tree] No source matches filter '{filter_text}' and status {status}")
        return []

    root = _Folder(path="", name="")
    for source in matching_sources:
        folder = root
        for part in PurePosixPath(source.path).parent.parts:
            folder_path = f"{folder.path}/{part}" if folder.path else part
            folder = folder.folders.setdefault(part, _Folder(path=folder_path, name=part))
        folder.sources.append(source)

    # Skip the top folders every source shares, such as `SourceArt`. They are taken from all the sources rather
    # than the matches, so the tree keeps its shape while it is filtered.
    for part in _shared_top_folders(all_sources):
        root = root.folders[part]

    rows: list[SourceRow] = []
    effective_collapsed = frozenset() if needle else collapsed
    _append_folder_contents(rows, root, depth=0, collapsed=effective_collapsed)
    return rows


def _shared_top_folders(sources: list[Source]) -> list[str]:
    """Return the leading folders that every source sits under, stopping above any folder that holds a source."""
    folder_parts = [PurePosixPath(source.path).parent.parts for source in sources]
    shared: list[str] = []
    for index, part in enumerate(folder_parts[0]):
        is_shared = all(len(parts) > index + 1 and parts[index] == part for parts in folder_parts)
        if not is_shared:
            break
        shared.append(part)
    return shared


def _append_folder_contents(rows: list[SourceRow], folder: _Folder, depth: int, collapsed: frozenset[str]) -> None:
    for child in sorted(folder.folders.values(), key=lambda child_folder: child_folder.name.lower()):
        only_source = child.sources[0] if len(child.sources) == 1 and not child.folders else None
        if only_source and PurePosixPath(only_source.path).stem == child.name:
            rows.append(_source_row(only_source, depth))
            continue

        is_expanded = child.path not in collapsed
        rows.append(SourceRow(key=child.path, name=child.name, depth=depth, is_folder=True, is_expanded=is_expanded))
        if is_expanded:
            _append_folder_contents(rows, child, depth + 1, collapsed)

    for source in sorted(folder.sources, key=lambda folder_source: folder_source.path.lower()):
        rows.append(_source_row(source, depth))


def _source_row(source: Source, depth: int) -> SourceRow:
    return SourceRow(
        key=source.path,
        name=PurePosixPath(source.path).stem,
        depth=depth,
        is_folder=False,
        status=source.status.value,
        export_count=len(source.exports),
    )
