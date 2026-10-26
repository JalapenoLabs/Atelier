"""Blender script that writes `SM_TestCrate.fbx`, the small real export the viewer's tests convert.

The crate is 100 x 50 x 30 cm (Unreal X x Y x Z), standing on the ground with its centre at X 0, Y 20, so the test
can tell Unreal's Y from Blender's (Unreal Y is Blender -Y). It has one material, `Body`, and one collision hull,
`UCX_SM_TestCrate_00`. Like the projects' exports, the geometry is in centimetres with `UnitScaleFactor` 1.

Regenerate with: `blender -b --factory-startup --python tests/fixtures/meshes/make_test_crate.py`
"""

from __future__ import annotations

from pathlib import Path

import bpy

OUTPUT = Path(__file__).with_name("SM_TestCrate.fbx")


def add_box(name: str, size: tuple[float, float, float], centre: tuple[float, float, float]) -> bpy.types.Object:
    """Add a box mesh object of `size` centimetres at `centre`, with its transform applied."""
    bpy.ops.mesh.primitive_cube_add(size=1, location=centre)
    box = bpy.context.active_object
    box.name = name
    box.data.name = name
    box.scale = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    return box


def main() -> None:
    """Build the crate and export it."""
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.context.scene.unit_settings.scale_length = 0.01

    # Blender Y is Unreal -Y, so Unreal Y 20 is Blender Y -20.
    crate = add_box("SM_TestCrate", (100, 50, 30), (0, -20, 15))
    body = bpy.data.materials.new("Body")
    crate.data.materials.append(body)
    add_box("UCX_SM_TestCrate_00", (100, 50, 30), (0, -20, 15))

    bpy.ops.export_scene.fbx(
        filepath=str(OUTPUT),
        apply_unit_scale=True,
        apply_scale_options="FBX_SCALE_NONE",
        object_types={"MESH"},
        bake_anim=False,
    )


main()
