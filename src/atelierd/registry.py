"""The machine's registry of known projects, `$XDG_DATA_HOME/atelier/projects.toml`.

Projects are keyed by name (their folder name), so `atelier scan AnchorFall` works from anywhere once a project has
been scanned by path. The registry holds no per-project state: that lives in each project's `.atelier/`.
"""

from __future__ import annotations

import logging
import os
import tomllib
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from atelierd.constants import REGISTRY_DIR_NAME, REGISTRY_FILE_NAME

logger = logging.getLogger(__name__)


class RegistryError(Exception):
    """A registry that cannot be read, or a project reference it cannot resolve."""


class RegisteredProject(BaseModel):
    """One registry entry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(description="Absolute path of the project root.")


class ProjectRegistry(BaseModel):
    """The whole registry file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    projects: dict[str, RegisteredProject] = Field(default_factory=dict)


def default_registry_path() -> Path:
    """Return the registry's path, honouring `XDG_DATA_HOME` (default `~/.local/share`)."""
    data_home = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data_home) / REGISTRY_DIR_NAME / REGISTRY_FILE_NAME


def load_registry(registry_path: Path) -> ProjectRegistry:
    """Load the registry, or an empty one when the file does not exist yet."""
    if not registry_path.is_file():
        logger.debug(f"[Registry] No registry at {registry_path}, starting empty")
        return ProjectRegistry()

    try:
        with registry_path.open("rb") as registry_file:
            return ProjectRegistry.model_validate(tomllib.load(registry_file))
    except (tomllib.TOMLDecodeError, ValidationError) as error:
        raise RegistryError(f"Invalid registry {registry_path}: {error}") from error


def register_project(project_root: Path, registry_path: Path) -> str:
    """Record a project in the registry under its folder name and return that name.

    Re-registering the same path is a no-op. A name already held by a different project is re-pointed only when that
    project's folder no longer exists (it moved); otherwise the clash is an error rather than a silent overwrite.
    """
    resolved_root = project_root.resolve()
    name = resolved_root.name
    registry = load_registry(registry_path)

    existing = registry.projects.get(name)
    if existing and Path(existing.path) == resolved_root:
        return name
    if existing and Path(existing.path).is_dir():
        raise RegistryError(f"Project name '{name}' is already registered for {existing.path}, not {resolved_root}")
    if existing:
        logger.info(f"[Registry] Re-pointing '{name}' from missing {existing.path} to {resolved_root}")

    projects = {**registry.projects, name: RegisteredProject(path=str(resolved_root))}
    updated_registry = ProjectRegistry(projects=projects)

    # Write beside the target and rename, so a crash mid-write never leaves a truncated registry.
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = registry_path.with_suffix(".toml.tmp")
    temporary_path.write_text(tomli_w.dumps(updated_registry.model_dump()), encoding="utf-8")
    temporary_path.replace(registry_path)
    logger.info(f"[Registry] Registered '{name}' at {resolved_root}")
    return name


def resolve_project_root(reference: str, registry_path: Path) -> Path:
    """Resolve a project given as a path to its folder or as a registered name.

    A path wins over a name, so a folder in the current directory is never shadowed by the registry.
    """
    candidate = Path(reference).expanduser()
    if candidate.is_dir():
        return candidate.resolve()

    registry = load_registry(registry_path)
    registered = registry.projects.get(reference)
    if not registered:
        known_names = ", ".join(sorted(registry.projects)) or "none"
        raise RegistryError(f"'{reference}' is neither a folder nor a registered project (registered: {known_names})")
    return Path(registered.path)
