"""Blender script: convert one FBX export to `.glb` and measure it.

Run by `atelierd.exports.cache` in headless Blender:
`blender -b --factory-startup --python fbx_to_glb.py -- <export.fbx> <out.glb> <out.json> <converter version>`.
The version is recorded in the stats, so the cache can tell results of an older script apart.

The scene's unit scale is set to centimetres before the import, so an export in centimetres (as every Unreal-bound
export is) keeps its numbers, and the glTF exporter, which writes Blender units unchanged, writes centimetres too.
With glTF's Y-up conversion the `.glb` then lands in exactly the frame Qt Quick 3D gives the FBX loaded directly:
Y up, centimetres. The stats are measured in Unreal's frame instead (X forward, Y right, Z up, centimetres), where a
Blender point (x, y, z) is Unreal (x, -y, z).

This file runs inside Blender's own Python, so it imports only `bpy`, numpy (bundled with Blender) and the stdlib.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import bpy
import numpy

COLLISION_PREFIX = "UCX_"
CENTIMETRE = 0.01


def main() -> None:
    """Convert the FBX named on the command line and write the `.glb` and its stats."""
    export_path, glb_path, stats_path, converter_version = sys.argv[sys.argv.index("--") + 1 :]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.unit_settings.scale_length = CENTIMETRE
    # Unreal writes no `_end` leaf bones, so stripping leaves would drop real bones.
    bpy.ops.import_scene.fbx(filepath=export_path, ignore_leaf_bones=False)

    stats = {"converter_version": int(converter_version), **measure_scene()}
    bpy.ops.export_scene.gltf(filepath=glb_path, export_format="GLB", export_yup=True, export_animations=False)
    Path(stats_path).write_text(json.dumps(stats, indent=2), encoding="utf-8")
    print(f"[fbx_to_glb] Converted {export_path}: {stats}")


def measure_scene() -> dict[str, object]:
    """Count triangles and materials and take the bounds of every mesh that is not a collision hull."""
    depsgraph = bpy.context.evaluated_depsgraph_get()
    triangle_count = 0
    material_names: set[str] = set()
    mesh_names: list[str] = []
    collision_names: list[str] = []
    is_skinned = False
    corner_points: list[numpy.ndarray] = []

    for scene_object in bpy.context.scene.objects:
        if scene_object.type != "MESH":
            continue
        if scene_object.name.startswith(COLLISION_PREFIX):
            collision_names.append(scene_object.name)
            continue

        mesh_names.append(scene_object.name)
        is_skinned = is_skinned or any(modifier.type == "ARMATURE" for modifier in scene_object.modifiers)
        material_names.update(slot.material.name for slot in scene_object.material_slots if slot.material)

        evaluated_object = scene_object.evaluated_get(depsgraph)
        mesh = evaluated_object.to_mesh()
        try:
            mesh.calc_loop_triangles()
            triangle_count += len(mesh.loop_triangles)
            vertex_count = len(mesh.vertices)
            if vertex_count:
                coordinates = numpy.empty(vertex_count * 3, dtype=numpy.float64)
                mesh.vertices.foreach_get("co", coordinates)
                local_points = coordinates.reshape(-1, 3)
                world_matrix = numpy.array(evaluated_object.matrix_world)
                world_points = local_points @ world_matrix[:3, :3].T + world_matrix[:3, 3]
                corner_points.append(world_points.min(axis=0))
                corner_points.append(world_points.max(axis=0))
        finally:
            evaluated_object.to_mesh_clear()

    bounds_min = [0.0, 0.0, 0.0]
    bounds_max = [0.0, 0.0, 0.0]
    if corner_points:
        stacked_points = numpy.array(corner_points)
        blender_min = stacked_points.min(axis=0)
        blender_max = stacked_points.max(axis=0)
        # Unreal's Y is Blender's -Y, so the min and max swap on that axis.
        bounds_min = [float(blender_min[0]), float(-blender_max[1]), float(blender_min[2])]
        bounds_max = [float(blender_max[0]), float(-blender_min[1]), float(blender_max[2])]

    return {
        "triangles": triangle_count,
        "materials": sorted(material_names),
        "meshes": sorted(mesh_names),
        "collision_hulls": sorted(collision_names),
        "is_skinned": is_skinned,
        "bounds_min_cm": bounds_min,
        "bounds_max_cm": bounds_max,
    }


if __name__ == "__main__":
    main()
