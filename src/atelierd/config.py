"""Loading a project's `atelier.toml`.

Every key is optional and falls back to the defaults documented in `docs/catalog.md`. Unknown keys are rejected
rather than ignored, so a typo in a hand-written config fails loudly instead of silently changing discovery.

The `[review]` table (`docs/review.md`) names the project's ticket tracker and its review queues. Its vocabulary,
`TrackerKind` and `ReviewVerdict`, is defined here rather than in `atelierd.trackers` or `atelierd.review`, because
both of those read this module: the config is the layer everything else builds on.
"""

from __future__ import annotations

import logging
import re
import tomllib
import urllib.parse
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from atelierd.constants import (
    DEFAULT_CHECKPOINT_KEY,
    DEFAULT_DECISION_JOBS,
    DEFAULT_REFERENCE_DROP_FOLDER,
    DEFAULT_REFERENCE_GLOBS,
    DEFAULT_RETAKE_KEY,
    DEFAULT_REVIEW_POLL_MINUTES,
    DEFAULT_ROOTS,
    DEFAULT_SHARED,
    DEFAULT_TALK_KEY,
    DEFAULT_TICKET_FILE_GLOBS,
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


class TrackerKind(StrEnum):
    """The ticket trackers Atelier can read a review queue from."""

    YOUTRACK = "youtrack"


class ReviewVerdict(StrEnum):
    """What the reviewer decided about one ticket."""

    APPROVE = "approve"
    CHANGES = "changes"


class TrackerConfig(BaseModel):
    """The `[review.tracker]` table: which tracker holds the project's tickets, and who Atelier acts as there."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: TrackerKind
    url: str = Field(description="The tracker's address, such as https://example.youtrack.cloud.")
    project: str = Field(description="The project key every queue query is scoped to, such as WAY.")
    reviewer: str = Field(
        description="The `mcpServers` entry in Claude Code's ~/.claude.json whose Authorization header Atelier reads "
        "and posts decisions with. Its server must be on the same host as `url`.",
    )
    stage_field: str = Field(default="Stage", description="The ticket field that holds its stage.")

    @field_validator("url")
    @classmethod
    def _url_is_http(cls, url: str) -> str:
        parsed_url = urllib.parse.urlsplit(url)
        if parsed_url.scheme not in ("https", "http") or not parsed_url.netloc:
            raise ValueError(f"'{url}' is not an http(s) address")
        return url.rstrip("/")


class LinkedStageRule(BaseModel):
    """A `[[review.on_approve]]` entry: approving a ticket moves the tickets linked to it from one stage to another."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tagged: str | None = Field(default=None, description="Only for approved tickets carrying this tag.")
    linked_summary: str = Field(
        description="A regular expression the linked ticket's summary must match from its start, such as "
        "'\\[UI\\] Adopt'.",
    )
    from_stage: str = Field(description="Only linked tickets in this stage move; the rest are left alone.")
    to_stage: str

    @field_validator("linked_summary")
    @classmethod
    def _summary_pattern_compiles(cls, pattern: str) -> str:
        try:
            re.compile(pattern)
        except re.error as error:
            raise ValueError(f"'{pattern}' is not a regular expression: {error}") from error
        return pattern


class TalkMode(StrEnum):
    """How the talk key records a voice note."""

    # Recording while the key is held; letting go ends the take.
    HOLD = "hold"
    # One press starts recording, the next ends the take.
    TOGGLE = "toggle"


class VoiceConfig(BaseModel):
    """The `[review.voice]` table: push-to-talk voice notes in the review window, and how they are transcribed.

    Keys are named as Qt names them (`` ` ``, `R`, `F8`), one key each with no modifier; the window reads them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    transcribe: tuple[str, ...] | None = Field(
        default=None,
        min_length=1,
        description="The command, and its arguments, that prints a take's words; the WAV's path is its last argument. "
        "Without it, `voxtype transcribe` when voxtype is on PATH.",
    )
    talk_key: str = Field(default=DEFAULT_TALK_KEY, min_length=1, description="The key that records a take.")
    talk_mode: TalkMode = TalkMode.HOLD
    retake_key: str = Field(
        default=DEFAULT_RETAKE_KEY,
        min_length=1,
        description="The key that discards the newest take after the checkpoint, and its recording.",
    )
    checkpoint_key: str = Field(
        default=DEFAULT_CHECKPOINT_KEY,
        min_length=1,
        description="The key that locks the takes so far, so a retake cannot remove them.",
    )

    @model_validator(mode="after")
    def _keys_differ(self) -> VoiceConfig:
        keys = [self.talk_key, self.retake_key, self.checkpoint_key]
        if len({key.casefold() for key in keys}) < len(keys):
            raise ValueError(f"[review.voice] needs three different keys, not {', '.join(keys)}")
        return self


class ReviewConfig(BaseModel):
    """The `[review]` table: the project's review queue and what each decision does to a ticket."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tracker: TrackerConfig
    queues: dict[str, str] = Field(
        min_length=1,
        description="The queue's tabs in order, each a name and the tracker query that lists its tickets.",
    )
    stages: dict[ReviewVerdict, str] = Field(description="The stage each verdict moves its ticket to.")
    on_approve: tuple[LinkedStageRule, ...] = ()
    poll_minutes: float = Field(
        default=DEFAULT_REVIEW_POLL_MINUTES,
        gt=0,
        description="How often the queue is read again when nobody asks for it sooner.",
    )
    ticket_files: tuple[str, ...] = Field(
        default=DEFAULT_TICKET_FILE_GLOBS,
        description="Project-relative globs of the files that name the ticket an asset was built for.",
    )
    rules: tuple[str, ...] = Field(
        default=(),
        description="Project-relative docs holding the project's review rules, which every decision job reads first.",
    )
    direction_notes: str | None = Field(
        default=None,
        description="The project-relative file each decision's direction notes (feedback for all the art, or a whole "
        "category) are appended to. Without it they are kept on the decision job only.",
    )
    decision_jobs: int = Field(
        default=DEFAULT_DECISION_JOBS,
        gt=0,
        description="How many decision jobs, each one `claude -p`, run at once.",
    )
    voice: VoiceConfig = Field(default_factory=VoiceConfig)

    @field_validator("rules", "direction_notes")
    @classmethod
    def _paths_stay_in_the_project(cls, paths: tuple[str, ...] | str | None) -> tuple[str, ...] | str | None:
        # The daemon appends to the direction notes file, so neither it nor a rules doc may point outside the project.
        for path in (paths,) if isinstance(paths, str) else paths or ():
            pure_path = PurePosixPath(path)
            if pure_path.is_absolute() or ".." in pure_path.parts or not pure_path.parts:
                raise ValueError(f"'{path}' must be a path inside the project, relative to its root")
        return paths

    @field_validator("stages")
    @classmethod
    def _every_verdict_has_a_stage(cls, stages: dict[ReviewVerdict, str]) -> dict[ReviewVerdict, str]:
        missing = [verdict.value for verdict in ReviewVerdict if verdict not in stages]
        if missing:
            raise ValueError(f"[review.stages] needs a stage for {', '.join(missing)}")
        return stages


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
    review: ReviewConfig | None = Field(default=None, description="The review queue, when the project has one.")


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
