"""Blender script: render one export's standard views, the same way for every source in every project.

Run by `atelierd.revisions.renders` in headless Blender:
`blender -b --factory-startup --python standard_renders.py -- <export> <out folder> <name> <texture folder>`.
It writes `<out folder>/<name>-<view>.png` for every view in `VIEWS`, each 1024 by 1024.

The export is read from Atelier's object store, so its file has no `.fbx` suffix and no textures beside it. A texture
the import cannot find is looked for by name in `<texture folder>`, the folder the export sits in within the project.

Every view is framed by the bounds of the export's meshes (collision hulls left out) and lit by the same three sun
lights fixed to the camera, a key from above left, a fill from the right and a rim from behind, so a model reads the
same from every side and at every size. The camera sees a neutral grey backdrop; the lighting sees a dimmer one.

Cameras are defined in Unreal's frame (X forward, Y right, Z up, centimetres), the frame the viewer and its camera
bookmarks use, and converted to Blender's, where an Unreal point (x, y, z) is the Blender point (x, -y, z).

This file runs inside Blender's own Python, so it imports only `bpy`, `mathutils` and the stdlib.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bpy
from mathutils import Matrix, Vector

COLLISION_PREFIX = "UCX_"
CENTIMETRE = 0.01
RESOLUTION = 1024
# Two renders may run at once beside the window and the user's own tools; each keeps to a few threads.
RENDER_THREADS = 4
RENDER_SAMPLES = 32

# Linear colours: a mid-grey backdrop the camera sees, a dim fill of ambient light, and the clay material.
BACKDROP_COLOUR = (0.22, 0.22, 0.23, 1.0)
AMBIENT_COLOUR = (0.05, 0.05, 0.055, 1.0)
CLAY_COLOUR = (0.48, 0.48, 0.48, 1.0)

# The hero camera matches the viewer's default framing: yaw 225 (the -X -Y side) and 22 degrees above the target.
HERO_YAW_DEGREES = 225.0
HERO_PITCH_DEGREES = 22.0
HERO_FIELD_OF_VIEW_DEGREES = 40.0
# How much room the model leaves around itself in the frame.
FRAME_MARGIN = 1.08


def _hero_direction() -> Vector:
    """Unit vector from the target to the hero camera, in Unreal's frame.

    Bookmark yaw is measured counterclockwise seen from above, with 0 on the target's +Y side and 90 on its +X side.
    """
    yaw = math.radians(HERO_YAW_DEGREES)
    pitch = math.radians(HERO_PITCH_DEGREES)
    return Vector((math.sin(yaw) * math.cos(pitch), math.cos(yaw) * math.cos(pitch), math.sin(pitch)))


# Each view: the direction from the target to the camera and the image's up direction, both in Unreal's frame,
# whether it is orthographic, and whether every material is swapped for clay.
# Unreal skeletal meshes (characters, Lyra's weapons) face +Y in mesh space, which is also Blender's front view.
#   front: from +Y, facing the asset.
#   side:  from +X, the asset's left side, with +Y (a face, a muzzle) on the image's left.
#   top:   from above, with -X up the image, so +Y is on the left as in the side view.
VIEWS = {
    "hero": (_hero_direction(), Vector((0, 0, 1)), False, False),
    "front": (Vector((0, 1, 0)), Vector((0, 0, 1)), True, False),
    "side": (Vector((1, 0, 0)), Vector((0, 0, 1)), True, False),
    "top": (Vector((0, 0, 1)), Vector((-1, 0, 0)), True, False),
    "clay": (_hero_direction(), Vector((0, 0, 1)), False, True),
}

# The three lights, as directions the light travels in camera space (X right, Y up, -Z into the image), and strengths.
LIGHTS = (
    ("Key", Vector((0.55, -0.65, -0.55)), 3.2),
    ("Fill", Vector((-0.8, -0.2, -0.45)), 1.1),
    ("Rim", Vector((0.1, -0.35, 1.0)), 2.4),
)


def main() -> None:
    """Import the export named on the command line and render every standard view of it."""
    export_path, output_folder, name, texture_folder = sys.argv[sys.argv.index("--") + 1 :]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.scale_length = CENTIMETRE
    # Unreal writes no `_end` leaf bones, so stripping leaves would drop real bones.
    bpy.ops.import_scene.fbx(filepath=export_path, ignore_leaf_bones=False)
    relink_missing_textures(Path(texture_folder))

    render_meshes = hide_collision_hulls(scene)
    if not render_meshes:
        raise RuntimeError(f"{export_path} holds no meshes to render")
    bounds_min, bounds_max = measure_bounds(render_meshes)

    configure_render(scene)
    clay_material = make_clay_material()
    camera = make_camera(scene)
    lights = [make_light(scene, light_name, strength) for light_name, _, strength in LIGHTS]

    for view_name, (unreal_direction, unreal_up, is_orthographic, is_clay) in VIEWS.items():
        direction = to_blender(unreal_direction).normalized()
        image_up = to_blender(unreal_up).normalized()
        frame_camera(camera, bounds_min, bounds_max, direction, image_up, is_orthographic)
        aim_lights(camera, lights)
        bpy.context.view_layer.material_override = clay_material if is_clay else None

        scene.render.filepath = str(Path(output_folder) / f"{name}-{view_name}.png")
        bpy.ops.render.render(write_still=True)
        print(f"[standard_renders] Rendered {name} {view_name}")


# -----------------------------------------------------------------------------
# The scene
# -----------------------------------------------------------------------------


def to_blender(unreal_vector: Vector) -> Vector:
    """Convert a direction from Unreal's frame to Blender's (Unreal Y is Blender -Y)."""
    return Vector((unreal_vector.x, -unreal_vector.y, unreal_vector.z))


def relink_missing_textures(texture_folder: Path) -> None:
    """Point every image the import could not find at a file of the same name in the export's project folder."""
    for image in bpy.data.images:
        if not image.filepath or Path(bpy.path.abspath(image.filepath)).is_file():
            continue
        candidate = texture_folder / Path(bpy.path.abspath(image.filepath)).name
        if candidate.is_file():
            image.filepath = str(candidate)
            image.reload()
            print(f"[standard_renders] Found texture {candidate.name} beside the export")
        else:
            print(f"[standard_renders] Texture {image.filepath} is missing")


def hide_collision_hulls(scene: bpy.types.Scene) -> list[bpy.types.Object]:
    """Hide `UCX_*` hulls from the render and return the meshes that remain."""
    render_meshes = []
    for scene_object in scene.objects:
        if scene_object.type != "MESH":
            continue
        if scene_object.name.startswith(COLLISION_PREFIX):
            scene_object.hide_render = True
            continue
        render_meshes.append(scene_object)
    return render_meshes


def measure_bounds(meshes: list[bpy.types.Object]) -> tuple[Vector, Vector]:
    """Return the world-space bounds of the evaluated meshes (a skinned mesh in its posed shape)."""
    bpy.context.view_layer.update()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    minimum = Vector((math.inf, math.inf, math.inf))
    maximum = Vector((-math.inf, -math.inf, -math.inf))
    for mesh_object in meshes:
        evaluated = mesh_object.evaluated_get(depsgraph)
        mesh = evaluated.to_mesh()
        try:
            for vertex in mesh.vertices:
                point = evaluated.matrix_world @ vertex.co
                minimum = Vector((min(minimum.x, point.x), min(minimum.y, point.y), min(minimum.z, point.z)))
                maximum = Vector((max(maximum.x, point.x), max(maximum.y, point.y), max(maximum.z, point.z)))
        finally:
            evaluated.to_mesh_clear()
    if minimum.x > maximum.x:
        raise RuntimeError("The export's meshes have no vertices")
    return minimum, maximum


def configure_render(scene: bpy.types.Scene) -> None:
    """EEVEE at 1024 square, a fixed thread count, a neutral view transform and the backdrop world."""
    scene.render.engine = "BLENDER_EEVEE"
    scene.eevee.taa_render_samples = RENDER_SAMPLES
    scene.render.resolution_x = RESOLUTION
    scene.render.resolution_y = RESOLUTION
    scene.render.resolution_percentage = 100
    scene.render.threads_mode = "FIXED"
    scene.render.threads = RENDER_THREADS
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"
    scene.render.image_settings.color_depth = "8"
    # Standard keeps base colours as authored, so a palette reads as the palette.
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"

    world = bpy.data.worlds.new("AtelierBackdrop")
    scene.world = world
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()
    light_path = nodes.new("ShaderNodeLightPath")
    ambient = nodes.new("ShaderNodeBackground")
    ambient.inputs["Color"].default_value = AMBIENT_COLOUR
    backdrop = nodes.new("ShaderNodeBackground")
    backdrop.inputs["Color"].default_value = BACKDROP_COLOUR
    mix = nodes.new("ShaderNodeMixShader")
    output = nodes.new("ShaderNodeOutputWorld")
    # The camera sees the backdrop; everything else (the light the model receives) sees the dim ambient.
    links.new(light_path.outputs["Is Camera Ray"], mix.inputs["Fac"])
    links.new(ambient.outputs["Background"], mix.inputs[1])
    links.new(backdrop.outputs["Background"], mix.inputs[2])
    links.new(mix.outputs["Shader"], output.inputs["Surface"])


def make_clay_material() -> bpy.types.Material:
    """A neutral, rough grey that replaces every material in the clay view."""
    material = bpy.data.materials.new("AtelierClay")
    principled = material.node_tree.nodes["Principled BSDF"]
    principled.inputs["Base Color"].default_value = CLAY_COLOUR
    principled.inputs["Roughness"].default_value = 0.75
    principled.inputs["Metallic"].default_value = 0.0
    return material


def make_camera(scene: bpy.types.Scene) -> bpy.types.Object:
    """Add the one camera every view is rendered through; `frame_camera` places it per view."""
    camera_data = bpy.data.cameras.new("AtelierCamera")
    camera_data.sensor_fit = "AUTO"
    camera_data.angle = math.radians(HERO_FIELD_OF_VIEW_DEGREES)
    camera = bpy.data.objects.new("AtelierCamera", camera_data)
    scene.collection.objects.link(camera)
    scene.camera = camera
    return camera


def make_light(scene: bpy.types.Scene, name: str, strength: float) -> bpy.types.Object:
    """Add a sun: its strength is independent of the model's size, which suits framing by bounds."""
    light_data = bpy.data.lights.new(name, type="SUN")
    light_data.energy = strength
    light_data.angle = math.radians(8)
    light = bpy.data.objects.new(name, light_data)
    scene.collection.objects.link(light)
    return light


# -----------------------------------------------------------------------------
# Framing
# -----------------------------------------------------------------------------


def frame_camera(
    camera: bpy.types.Object,
    bounds_min: Vector,
    bounds_max: Vector,
    direction: Vector,
    image_up: Vector,
    is_orthographic: bool,
) -> None:
    """Place the camera on `direction` from the bounds' centre, looking back at it, with `image_up` up the image."""
    centre = (bounds_min + bounds_max) / 2
    radius = max((bounds_max - bounds_min).length / 2, 1.0)

    # The camera looks along its -Z with +Y up, so its basis is (right, up, -forward).
    forward = -direction
    right = forward.cross(image_up).normalized()
    up = right.cross(forward).normalized()
    rotation = Matrix((right, up, -forward)).transposed()

    camera_data = camera.data
    if is_orthographic:
        corners = [
            Vector((x, y, z))
            for x in (bounds_min.x, bounds_max.x)
            for y in (bounds_min.y, bounds_max.y)
            for z in (bounds_min.z, bounds_max.z)
        ]
        across = [(corner - centre).dot(right) for corner in corners]
        along = [(corner - centre).dot(up) for corner in corners]
        width = max(across) - min(across)
        height = max(along) - min(along)
        camera_data.type = "ORTHO"
        camera_data.ortho_scale = max(width, height, 1.0) * FRAME_MARGIN
        # Centre the image on the projected bounds, which is the bounds' centre for a box seen square on.
        centre = centre + right * (max(across) + min(across)) / 2 + up * (max(along) + min(along)) / 2
        distance = radius * 3
    else:
        camera_data.type = "PERSP"
        distance = radius / math.sin(camera_data.angle / 2) * FRAME_MARGIN

    camera.matrix_world = Matrix.Translation(centre + direction * distance) @ rotation.to_4x4()
    camera_data.clip_start = max(distance - radius * 2, distance * 0.001)
    camera_data.clip_end = distance + radius * 2


def aim_lights(camera: bpy.types.Object, lights: list[bpy.types.Object]) -> None:
    """Turn the three suns so they keep the same place relative to the camera in every view."""
    camera_rotation = camera.matrix_world.to_3x3()
    for light, (_, camera_space_direction, _) in zip(lights, LIGHTS, strict=True):
        travel = (camera_rotation @ camera_space_direction).normalized()
        # A sun shines along its own -Z.
        light.rotation_euler = travel.to_track_quat("-Z", "Y").to_euler()


if __name__ == "__main__":
    main()
