"""Domain models of the catalog: sources, their files, references and what changed between scans.

Every path in these models is project-relative and POSIX-style, so the same values are valid on any machine the
project's drive is mounted on and can be stored in `state.db` as they are.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class SourceStatus(StrEnum):
    """A source's review status. It lives only in `state.db`."""

    DRAFT = "draft"
    IN_REVIEW = "in-review"
    CHANGES_REQUESTED = "changes-requested"
    APPROVED = "approved"


class DiscoveredSource(BaseModel):
    """A source as found on disk by one scan, before any stored state is attached."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(description="The .blend file; also the source's identity.")
    builder: str | None = Field(default=None, description="The script that generates the .blend, when known.")
    readme: str | None = None
    exports: tuple[str, ...] = Field(default=(), description="FBX files the source produces, sorted.")
    renders: tuple[str, ...] = Field(default=(), description="Images the project's tooling renders of it, sorted.")
    has_output_override: bool = Field(
        default=False,
        description='Whether `atelier.toml` replaced the conventional outputs with `[sources."…"].outputs`.',
    )


class Source(DiscoveredSource):
    """A source in the catalog: what is on disk plus its stored review state."""

    status: SourceStatus = SourceStatus.DRAFT


class Reference(BaseModel):
    """A project-wide reference image."""

    model_config = ConfigDict(frozen=True)

    path: str
    tag: str = Field(description="Name of the folder the image sits in, such as `Creatures` or `Palette`.")


class CameraBookmark(BaseModel):
    """A named viewpoint on a source in the viewer, an orbit around a target, in Unreal's frame (Z up, cm)."""

    model_config = ConfigDict(frozen=True)

    name: str
    target_cm: tuple[float, float, float] = Field(description="The point the camera orbits and looks at.")
    yaw_degrees: float = Field(
        description="Turn about the vertical, counterclockwise seen from above: at 0 the camera is on the target's "
        "+Y side, at 90 on its +X side.",
    )
    pitch_degrees: float = Field(description="Tilt; negative looks down on the target from above.")
    distance_cm: float = Field(description="Distance from the camera to the target.")


class CatalogChange(BaseModel):
    """What one scan changed relative to the catalog stored before it."""

    model_config = ConfigDict(frozen=True)

    added: tuple[str, ...] = Field(default=(), description="Sources that are new.")
    removed: tuple[str, ...] = Field(default=(), description="Sources whose .blend is gone.")
    changed: tuple[str, ...] = Field(
        default=(),
        description="Sources still present whose builder, README or output list differ.",
    )
    references_changed: bool = False

    @property
    def is_empty(self) -> bool:
        """Whether the scan found nothing new."""
        return not (self.added or self.removed or self.changed or self.references_changed)


class ScanResult(BaseModel):
    """The catalog after a scan, and how the scan changed it."""

    model_config = ConfigDict(frozen=True)

    sources: tuple[Source, ...]
    references: tuple[Reference, ...]
    change: CatalogChange
    unmatched_overrides: tuple[str, ...] = Field(
        default=(),
        description='`[sources."…"]` keys in `atelier.toml` that name no discovered source, usually a typo.',
    )
