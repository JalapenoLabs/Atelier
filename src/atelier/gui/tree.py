"""The source tree as the left pane shows it: folders, sources and their exports flattened into rows.

The tree mirrors the project's folders, with each source's exports as its children, and two simplifications keep it
short:

- a top folder every source sits under (`SourceArt/`) is left out;
- a folder that holds nothing but one source of its own name (`Carbine/Carbine.blend`) is shown as that source.

Folders start open and sources start closed, because a source can hold dozens of exports; a `TreeExpansion` records
the reviewer's changes to both.

A filter keeps the sources that have the status and whose path, or one of whose export names, contains the text
(ignoring case), and the folders leading to them. While text is being filtered every folder shows open, so a match is
never hidden in a collapsed one, and a source whose exports match shows open with just those exports. A source that
matches by its path alone keeps its own open state and all its exports.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

from atelierd.catalog import Source, SourceStatus

logger = logging.getLogger(__name__)


class RowKind:
    """What a row of the tree stands for, as its delegate reads it."""

    FOLDER = "folder"
    SOURCE = "source"
    EXPORT = "export"


class SourceRow(BaseModel):
    """One row of the tree: a folder, a source or one of a source's exports."""

    model_config = ConfigDict(frozen=True)

    key: str = Field(description="The folder's, source's or export's project-relative path; unique per row.")
    kind: str = Field(description="A `RowKind`.")
    name: str
    depth: int
    source: str = Field(default="", description="The source a source or export row belongs to; empty for a folder.")
    is_expandable: bool = False
    is_expanded: bool = False
    status: str = Field(default="", description="The source's status, on its source row only.")
    export_count: int = 0


class TreeExpansion(BaseModel):
    """The reviewer's changes to the tree's open state: the folders closed and the sources opened."""

    model_config = ConfigDict(frozen=True)

    collapsed_folders: frozenset[str] = frozenset()
    expanded_sources: frozenset[str] = frozenset()

    def toggled(self, row: SourceRow) -> TreeExpansion:
        """Return the expansion with `row` opened if it was closed, or closed if it was open."""
        if row.kind == RowKind.FOLDER:
            return self.model_copy(update={"collapsed_folders": self.collapsed_folders ^ {row.key}})
        return self.model_copy(update={"expanded_sources": self.expanded_sources ^ {row.key}})


# Every folder open and every source closed.
DEFAULT_EXPANSION = TreeExpansion()


class _ShownSource:
    """A source that passed the filter, with the exports it lists and whether the filter holds it open."""

    def __init__(self, source: Source, exports: list[str], is_held_open: bool) -> None:
        self.source = source
        self.exports = exports
        self.is_held_open = is_held_open


class _Folder:
    """A folder while the tree is being built."""

    def __init__(self, path: str, name: str) -> None:
        self.path = path
        self.name = name
        self.folders: dict[str, _Folder] = {}
        self.sources: list[_ShownSource] = []


def build_source_rows(
    sources: Iterable[Source],
    filter_text: str = "",
    status: SourceStatus | None = None,
    expansion: TreeExpansion = DEFAULT_EXPANSION,
) -> list[SourceRow]:
    """Return the rows of the tree, in display order, for the sources that pass the filter."""
    all_sources = list(sources)
    needle = filter_text.strip().lower()

    root = _Folder(path="", name="")
    for source in all_sources:
        if status is not None and source.status is not status:
            continue
        matching_exports = [path for path in source.exports if needle and needle in PurePosixPath(path).name.lower()]
        if matching_exports:
            shown = _ShownSource(source, matching_exports, is_held_open=True)
        elif needle in source.path.lower():
            shown = _ShownSource(source, list(source.exports), is_held_open=False)
        else:
            continue

        folder = root
        for part in PurePosixPath(source.path).parent.parts:
            folder_path = f"{folder.path}/{part}" if folder.path else part
            folder = folder.folders.setdefault(part, _Folder(path=folder_path, name=part))
        folder.sources.append(shown)

    if not (root.folders or root.sources):
        logger.debug(f"[Tree] No source matches filter '{filter_text}' and status {status}")
        return []

    # Skip the top folders every source shares, such as `SourceArt`. They are taken from all the sources rather
    # than the matches, so the tree keeps its shape while it is filtered.
    for part in _shared_top_folders(all_sources):
        root = root.folders[part]

    if needle:
        expansion = expansion.model_copy(update={"collapsed_folders": frozenset()})
    rows: list[SourceRow] = []
    _append_folder_contents(rows, root, depth=0, expansion=expansion)
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


def _append_folder_contents(rows: list[SourceRow], folder: _Folder, depth: int, expansion: TreeExpansion) -> None:
    for child in sorted(folder.folders.values(), key=lambda child_folder: child_folder.name.lower()):
        only_source = child.sources[0] if len(child.sources) == 1 and not child.folders else None
        if only_source and PurePosixPath(only_source.source.path).stem == child.name:
            _append_source(rows, only_source, depth, expansion)
            continue

        is_expanded = child.path not in expansion.collapsed_folders
        rows.append(
            SourceRow(
                key=child.path,
                kind=RowKind.FOLDER,
                name=child.name,
                depth=depth,
                is_expandable=True,
                is_expanded=is_expanded,
            )
        )
        if is_expanded:
            _append_folder_contents(rows, child, depth + 1, expansion)

    for shown in sorted(folder.sources, key=lambda folder_source: folder_source.source.path.lower()):
        _append_source(rows, shown, depth, expansion)


def _append_source(rows: list[SourceRow], shown: _ShownSource, depth: int, expansion: TreeExpansion) -> None:
    """Add a source's row, then its exports' rows while it is open."""
    source = shown.source
    is_expandable = bool(shown.exports)
    is_expanded = is_expandable and (shown.is_held_open or source.path in expansion.expanded_sources)
    rows.append(
        SourceRow(
            key=source.path,
            kind=RowKind.SOURCE,
            name=PurePosixPath(source.path).stem,
            depth=depth,
            source=source.path,
            is_expandable=is_expandable,
            is_expanded=is_expanded,
            status=source.status.value,
            export_count=len(source.exports),
        )
    )
    if not is_expanded:
        return
    for export_path in shown.exports:
        rows.append(
            SourceRow(
                key=export_path,
                kind=RowKind.EXPORT,
                name=PurePosixPath(export_path).stem,
                depth=depth + 1,
                source=source.path,
            )
        )
