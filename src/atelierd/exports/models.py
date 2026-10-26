"""What the viewer knows about one export: its converted `.glb` and its stats."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ExportStats(BaseModel):
    """Measurements of one export, taken by Blender when it converts the export.

    Collision hulls (`UCX_*`) are counted apart: they are not part of what a player sees.
    """

    model_config = ConfigDict(frozen=True)

    converter_version: int = Field(description="Version of the conversion script that measured this.")
    triangles: int = Field(description="Triangles of every mesh except collision hulls.")
    materials: tuple[str, ...] = Field(default=(), description="Names of the materials those meshes use, sorted.")
    meshes: tuple[str, ...] = Field(default=(), description="Names of the meshes, sorted.")
    collision_hulls: tuple[str, ...] = Field(default=(), description="Names of the `UCX_*` hulls, sorted.")
    is_skinned: bool = Field(default=False, description="Whether a mesh is bound to an armature.")
    bounds_min_cm: tuple[float, float, float] = Field(description="Bounds minimum in Unreal's frame (Z up, cm).")
    bounds_max_cm: tuple[float, float, float] = Field(description="Bounds maximum in Unreal's frame (Z up, cm).")

    @property
    def size_cm(self) -> tuple[float, float, float]:
        """The bounds' extent along Unreal's X, Y and Z."""
        minimum_x, minimum_y, minimum_z = self.bounds_min_cm
        maximum_x, maximum_y, maximum_z = self.bounds_max_cm
        return (maximum_x - minimum_x, maximum_y - minimum_y, maximum_z - minimum_z)


class ConvertedExport(BaseModel):
    """An export converted to `.glb`, with its stats, as cached under `<state>/cache/`."""

    model_config = ConfigDict(frozen=True)

    export: str = Field(description="The export's project-relative path.")
    sha256: str = Field(description="Hash of the FBX's bytes, which names both cache files.")
    glb_path: Path
    stats: ExportStats
