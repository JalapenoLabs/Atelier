"""A project Atelier points at: its root, its config file and where its state lives.

State normally lives in `<project>/.atelier/`, which is then added to the project's version-control ignore file.
The state folder can be put elsewhere (`atelier scan --state-dir`), which leaves the project completely untouched:
Atelier writes nothing into a project except its own state folder and that one ignore entry.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from atelierd.constants import (
    CONFIG_FILE_NAME,
    DIVERSION_IGNORE_FILE,
    GIT_IGNORE_FILE,
    STATE_DB_NAME,
    STATE_DIR_NAME,
    STATE_IGNORE_ENTRY,
)

logger = logging.getLogger(__name__)

# Ignore-file lines that already cover the state folder.
_EQUIVALENT_IGNORE_ENTRIES = frozenset(
    {STATE_DIR_NAME, STATE_IGNORE_ENTRY, f"/{STATE_DIR_NAME}", f"/{STATE_IGNORE_ENTRY}"}
)

# The ignore file to create for a project that has none yet, keyed by the folder that marks its version control.
_IGNORE_FILE_BY_VCS_FOLDER = {
    ".diversion": DIVERSION_IGNORE_FILE,
    ".git": GIT_IGNORE_FILE,
}


class Project(BaseModel):
    """Where one project's files, config and state are."""

    model_config = ConfigDict(frozen=True)

    root: Path
    config_path: Path
    state_dir: Path

    @property
    def name(self) -> str:
        """The project's name, its folder name."""
        return self.root.name

    @property
    def database_path(self) -> Path:
        """The project's `state.db`."""
        return self.state_dir / STATE_DB_NAME

    @property
    def is_state_in_project(self) -> bool:
        """Whether the state folder sits inside the project, and so must be kept out of its version control."""
        return self.state_dir.is_relative_to(self.root)


def open_project(root: Path, config_path: Path | None = None, state_dir: Path | None = None) -> Project:
    """Describe a project, defaulting to `<root>/atelier.toml` and `<root>/.atelier/`. Writes nothing.

    Raises:
        NotADirectoryError: `root` is not a folder.
    """
    resolved_root = root.resolve()
    if not resolved_root.is_dir():
        raise NotADirectoryError(f"Project root {resolved_root} is not a folder")

    return Project(
        root=resolved_root,
        config_path=(config_path or resolved_root / CONFIG_FILE_NAME).resolve(),
        state_dir=(state_dir or resolved_root / STATE_DIR_NAME).resolve(),
    )


def ensure_state_ignored(project: Project) -> list[Path]:
    """Make sure the project's version control ignores its state folder, and return the ignore files changed.

    The entry goes into every ignore file the project already has (`.dvignore`, `.gitignore`). A project with none
    gets one for the version control it uses. A state folder outside the project needs no entry at all.
    """
    if not project.is_state_in_project:
        logger.info(f"[Project] State lives outside {project.root}, leaving its ignore files alone")
        return []

    ignore_files = [project.root / name for name in (DIVERSION_IGNORE_FILE, GIT_IGNORE_FILE)]
    existing_ignore_files = [ignore_file for ignore_file in ignore_files if ignore_file.is_file()]
    if not existing_ignore_files:
        existing_ignore_files = [
            project.root / ignore_file_name
            for vcs_folder, ignore_file_name in _IGNORE_FILE_BY_VCS_FOLDER.items()
            if (project.root / vcs_folder).is_dir()
        ]
    if not existing_ignore_files:
        logger.warning(f"[Project] {project.root} has no version control; nothing to add '{STATE_IGNORE_ENTRY}' to")
        return []

    changed_files: list[Path] = []
    for ignore_file in existing_ignore_files:
        content = ignore_file.read_text(encoding="utf-8") if ignore_file.is_file() else ""
        if any(line.strip() in _EQUIVALENT_IGNORE_ENTRIES for line in content.splitlines()):
            continue

        # Appending, rather than rewriting the file, can never disturb the project's own entries.
        leading_break = ""
        if content:
            leading_break = "\n\n" if not content.endswith("\n") else "\n"
        with ignore_file.open("a", encoding="utf-8") as ignore_handle:
            ignore_handle.write(f"{leading_break}# Atelier's review state\n{STATE_IGNORE_ENTRY}\n")
        logger.info(f"[Project] Added '{STATE_IGNORE_ENTRY}' to {ignore_file}")
        changed_files.append(ignore_file)

    return changed_files
