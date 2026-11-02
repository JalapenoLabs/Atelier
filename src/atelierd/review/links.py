"""Linking tickets to catalog sources, through the files in the project that name the ticket an asset was built for.

`[review].ticket_files` globs those files (Waystead: `SourceArt/<Family>/Source/<Asset>/reference.json` and the
`spec.md` beside it). A JSON file names its ticket in a top-level `ticket` field and may list the meshes it covers in
`subjects[].meshes`; any other file names it on its first non-empty line, `Ticket: WAY-60`.

A ticket file sits below the folder of the source its asset is built in (`SourceArt/Tools/Tools.blend` builds
`SourceArt/Tools/Source/Hammer/`), so the source is the one in the nearest folder above the file that holds any. Where
that folder holds several kits, the one exporting the file's meshes wins.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path, PurePosixPath

from wcmatch import glob

from atelierd.catalog import Source
from atelierd.catalog.discovery import GLOB_FLAGS
from atelierd.review.models import ReviewLink

logger = logging.getLogger(__name__)

_TICKET_LINE_PATTERN = re.compile(r"^Ticket:\s*(\S+)\s*$")


def find_ticket_links(
    project_root: Path, ticket_file_globs: tuple[str, ...], sources: list[Source]
) -> dict[str, ReviewLink]:
    """Return the source each ticket named by a ticket file is built in, by ticket id."""
    sources_by_folder: dict[PurePosixPath, list[Source]] = {}
    for source in sources:
        sources_by_folder.setdefault(PurePosixPath(source.path).parent, []).append(source)

    links: dict[str, ReviewLink] = {}
    ticket_file_paths = sorted(glob.glob(list(ticket_file_globs), root_dir=project_root, flags=GLOB_FLAGS))
    for ticket_file in ticket_file_paths:
        ticket, mesh_names = _read_ticket_file(project_root / ticket_file)
        if ticket is None:
            continue
        owning_source = _owning_source(PurePosixPath(ticket_file), mesh_names, sources_by_folder)
        if owning_source is None:
            logger.debug(f"[Review] {ticket_file} names {ticket}, but no source's folder holds it")
            continue
        exports = tuple(export for export in owning_source.exports if PurePosixPath(export).stem in mesh_names)

        known_link = links.get(ticket)
        if known_link is None:
            links[ticket] = ReviewLink(source=owning_source.path, exports=exports, ticket_file=ticket_file)
            continue
        if known_link.source != owning_source.path:
            logger.warning(
                f"[Review] {ticket} is named by {known_link.ticket_file} ({known_link.source}) and by {ticket_file} "
                f"({owning_source.path}); keeping the first"
            )
            continue
        # Both files name the ticket in the same source (a reference.json and its spec.md): keep every export named.
        merged_exports = tuple(sorted({*known_link.exports, *exports}))
        links[ticket] = known_link.model_copy(update={"exports": merged_exports})

    return links


def _read_ticket_file(ticket_file: Path) -> tuple[str | None, set[str]]:
    """Return the ticket a file names, if any, and the mesh names it lists."""
    try:
        text = ticket_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        logger.warning(f"[Review] Could not read the ticket file {ticket_file}: {error}")
        return None, set()

    if ticket_file.suffix.lower() != ".json":
        first_line = next((line for line in text.splitlines() if line.strip()), "")
        match = _TICKET_LINE_PATTERN.match(first_line.strip())
        if match is None:
            logger.debug(f"[Review] {ticket_file} does not start with 'Ticket: <id>'")
            return None, set()
        return match.group(1), set()

    try:
        content = json.loads(text)
    except json.JSONDecodeError as error:
        logger.warning(f"[Review] The ticket file {ticket_file} is not valid JSON: {error}")
        return None, set()
    if not isinstance(content, dict) or not isinstance(content.get("ticket"), str):
        logger.debug(f"[Review] {ticket_file} has no 'ticket' field")
        return None, set()

    mesh_names: set[str] = set()
    subjects = content.get("subjects")
    for subject in subjects if isinstance(subjects, list) else []:
        meshes = subject.get("meshes") if isinstance(subject, dict) else None
        mesh_names.update(mesh for mesh in meshes or [] if isinstance(mesh, str))
    return content["ticket"], mesh_names


def _owning_source(
    ticket_file: PurePosixPath,
    mesh_names: set[str],
    sources_by_folder: dict[PurePosixPath, list[Source]],
) -> Source | None:
    """Return the source in the nearest folder above a ticket file, preferring the kit that exports its meshes."""
    for folder in ticket_file.parents:
        candidates = sources_by_folder.get(folder)
        if not candidates:
            continue
        if len(candidates) == 1:
            return candidates[0]
        exporting = [
            source
            for source in candidates
            if any(PurePosixPath(export).stem in mesh_names for export in source.exports)
        ]
        if len(exporting) == 1:
            return exporting[0]
        logger.debug(f"[Review] {ticket_file} sits beside {len(candidates)} kits and its meshes pick none of them")
        return None
    return None
