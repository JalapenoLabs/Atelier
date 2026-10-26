"""Loading a project's `atelier.toml`.

Every key is optional and falls back to the defaults documented in `docs/catalog.md`. Unknown keys are rejected
rather than ignored, so a typo in a hand-written config fails loudly instead of silently changing discovery.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from atelierd.constants import (
    DEFAULT_REFERENCE_DROP_FOLDER,
    DEFAULT_REFERENCE_GLOBS,
    DEFAULT_ROOTS,
    DEFAULT_SHARED,
)

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """An `atelier.toml` that cannot be read or does not match the schema."""


class ReferencesConfig(BaseModel):
    """The `[references]` table: where project-wide reference images live."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    globs: tuple[str, ...] = Field(
        default=DEFAULT_REFERENCE_GLOBS,
        description="Project-relative globs whose images are listed in the references rail.",
    )
    drop_folder: str = Field(
        default=DEFAULT_REFERENCE_DROP_FOLDER,
        description="Project-relative folder an image dragged into the rail is copied into.",
    )


class SourceOverride(BaseModel):
    """A `[sources."<path>"]` table: per-source exceptions to the discovery conventions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outputs: tuple[str, ...] | None = Field(
        default=None,
        description="Project-relative globs that replace the conventional output files of a kit whose exports and "
        "renders live outside its .blend's folder.",
    )


class AtelierConfig(BaseModel):
    """A project's `atelier.toml`, with defaults filled in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    roots: tuple[str, ...] = Field(default=DEFAULT_ROOTS, description="Project-relative folders sources live under.")
    shared: tuple[str, ...] = Field(
        default=DEFAULT_SHARED,
        description="Project-relative globs many sources build from.",
    )
    references: ReferencesConfig = Field(default_factory=ReferencesConfig)
    sources: dict[str, SourceOverride] = Field(
        default_factory=dict,
        description="Overrides keyed by the source's project-relative .blend path.",
    )


def load_config(config_path: Path) -> AtelierConfig:
    """Load `atelier.toml`, or the defaults when the file does not exist.

    Raises:
        ConfigError: the file exists but is not valid TOML or does not match the schema.
    """
    if not config_path.is_file():
        logger.debug(f"[Config] No config at {config_path}, using defaults")
        return AtelierConfig()

    try:
        with config_path.open("rb") as config_file:
            raw_config = tomllib.load(config_file)
        return AtelierConfig.model_validate(raw_config)
    except (tomllib.TOMLDecodeError, ValidationError) as error:
        raise ConfigError(f"Invalid config {config_path}: {error}") from error
