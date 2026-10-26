# Viewer

The desktop app, `atelier` with no command. It is a PySide6 / Qt Quick window over the catalog.

```
uv run atelier                                      # reopens the last project
uv run atelier --project AnchorFall                 # a known project by name, or any project folder by path
uv run atelier --project <path> --state-root <dir>  # keep every project's state in <dir>/<name>/, not in the project
```

## Window

A three-pane studio:

```
┌ Atelier  AnchorFall ▾  status message ──────────────────────────────────────────────────┐
│ Sources      │ Carbine [In review ▾]          Export [SK_Carbine ▾] │ [Chat] [Refs] [README] │
│ filter…      │  ┌ stats ┐               [Studio Clay … Views ▾]     │                        │
│ All statuses │  │ 3D viewport, or a render full size              │ │ references rail,       │
│ ▾ Weapons    │  └────────────────────────────────────────────────┘ │ drop images here       │
│   ● Carbine  │  render gallery, grouped by stem                     │                        │
└──────────────┴──────────────────────────────────────────────────────┴────────────────────────┘
```

- **Header**: the project picker (every project in the machine's registry) and the latest status message. The
  approvals badge for worker tool calls joins it with workers (see `workers.md`).
- **Sources** (left): the catalog as a tree mirroring the folders, each source with its status colour and export
  count. A top folder every source shares (`SourceArt/`) is left out, and a folder holding only a source of its own
  name (`Carbine/Carbine.blend`) shows as that source. Filters by text (anywhere in the path) and by status; while
  text is filtered every folder shows open.
- **Stage** (centre): the selected source's name, its status (settable here) and the export picker, over the 3D
  viewport or a render full size, with the render gallery under it. The revision filmstrip and two-up compare join the
  stage with revisions (see `revisions.md`).
- **Context** (right): tabs for the source's worker chat (a placeholder until workers), the references rail, and its
  README rendered as Markdown.

Panel sizes, the selected tab, the last project and the viewport's display settings are remembered per machine, in
Qt's settings (`~/.config/JalapenoLabs/Atelier.conf`). Everything else the window shows comes from the core.

## 3D viewport

The viewport shows a source's **exports**, because those are exactly what Unreal imports: you review what ships. The
`.blend` itself is never opened by the viewer.

### Loading

Qt Quick 3D's `RuntimeLoader` reads FBX through assimp, and the pinned PySide6 wheels ship it (`pyside6-addons`
6.12.0 has `assetimporters/libassimp.so`). It loads static and skeletal exports alike, AnchorFall's `SK_Carbine`
included, at their true size: assimp applies the file's axis declaration and keeps centimetres, so the scene is Y up
in centimetres. But assimp reads a Blender-made FBX wrongly in two ways seen on the real projects: base colours come
out too dark (the file's linear colours are taken as sRGB), and a texture missing beside the FBX renders black
(CruiseTycoon's palette texture lives in `SourceArt/Palette/`, not beside the exports).

So every export is shown in two steps:

1. The FBX goes straight to `RuntimeLoader`, for a picture at once.
2. Meanwhile headless Blender converts it to `.glb` and measures it (see "Conversion" below). The `.glb` replaces the
   direct load as soon as it is ready, keeping the camera. On a cache hit that takes a fraction of a second.

A direct load that fails switches to the `.glb` too. **Direct FBX** in the toolbar keeps the direct load instead, to
see what assimp makes of a file. The stats overlay names the path that put the model on screen.

### Conversion

`atelierd.exports.convert_export` runs `exports/blender_scripts/fbx_to_glb.py` in headless Blender through
`atelierd.blender.run_blender_script`: factory settings, `nice`, a timeout, and a `PATH` cleared of every other Python
(Blender built on the system Python takes its standard library from the first `python3` on `PATH`). The script sets
the scene's unit scale to centimetres before importing, so the `.glb` stays in centimetres and, converted to glTF's
Y up, lands in exactly the frame of the direct load.

The result is cached in `<state>/cache/` as `<sha256>.glb` and `<sha256>.json`, named by the hash of the FBX's bytes:
an unchanged export is never converted twice, even renamed. The JSON holds the stats and the converter's version; a
result from another version is converted again. Both files are written under temporary names and renamed into place,
stats last. Conversions run off the UI thread, two at a time; a prop takes about 0.6 s.

### Frames and scale

Exports, the grid and the scale figure use Unreal's frame: Z up, centimetres. Qt's scene is Y up, so a Qt point
(x, y, z) is the Unreal point (x, z, y). The Blender model is already physically the Unreal one (Unreal reads
Blender's Y as -Y), so nothing is mirrored.

- **Grid**: Qt's infinite grid on the ground (Unreal Z = 0), every 10 cm for a model up to 3 m across and every
  1 m beyond, with the X (red) and Y (blue) axes.
- **Scale reference**: a 180 cm figure standing on the ground off the model's +X -Y corner, about as far from the
  default camera as the model itself.

### Camera

Orbit (drag), pan (Ctrl+drag) and zoom (wheel) around a target. **F** frames the model's bounds from a three-quarter
view on its -X -Y side, slightly above; **Shift+F** frames the model and the figure together. The camera is framed
when an export is first shown and kept when the same export reloads.

**Camera bookmarks** (**Views**) are saved per source, by name, in `state.db` (`camera_bookmarks`), so a manager can
use the same views later. Each holds the orbit target in Unreal centimetres, the yaw (about the vertical,
counterclockwise seen from above: 0 on the target's +Y side, 90 on its +X side), the pitch (negative looks down) and
the distance in cm.

### Display

- **Studio** lighting: three directional lights fixed to the camera (a key from above left, a fill from the right, a
  rim from behind), so a model reads the same from every side. **Clay** keeps the lights and swaps every material for
  a neutral grey.
- **Wireframe** over the shaded model.
- **Collision**: `UCX_*` hulls are hidden by default and shown as a translucent green overlay.
- **Stats** (top left): triangles and materials (collision hulls left out), the bounds in centimetres along Unreal's
  X, Y and Z, the number of collision hulls, whether the mesh is skinned, the load path and the grid spacing. They
  come from Blender's measurement, so they are the same whichever path loaded the model. The camera frames on the
  same bounds, using the loaded meshes' own bounds until the measurement arrives.

## Render gallery

Under the stage, every render of the source, grouped by **stem**, each group labelled. A render's stem is the longest
`_`-separated prefix of its file name, the whole name included, that another render's name equals or continues:
`Carbine_Front` and `Carbine_Side` group as `Carbine`; `CB_CargoBooster` and `CB_CargoBooster_2` as
`CB_CargoBooster`; `Hull_preview` and `Hull_preview_port` as `Hull_preview`; `Hair_Braid_Front` and
`Hair_Braid_Back` as `Hair_Braid`. A render sharing no prefix is a group of its own. Projects name renders too freely
for a fixed list of view words, so the groups come from the set itself.

Clicking a render shows it full size on the stage; **3D view** goes back. A source with renders but no exports opens
on its first render. The daemon's standard renders join as the first group with revisions, and markup of a render or
a frozen viewport frame with workers (see `workers.md`).

## References rail

The **Refs** tab lists every reference image of the project (see `catalog.md`), tagged by folder. Dropping image files
onto it copies them into `references.drop_folder` and rescans, so they appear at once. A file of the same name already
there is never overwritten: the same image is not copied again, and a different one gets the next free name
(`concept-2.png`).

## Between the window and the core

The window reaches the core through exactly one interface, `atelier.gui.backend.StudioBackend`: open a project and
follow its changes, list sources and references, set a status, keep bookmarks, add a reference, convert an export. It
speaks only in core models and project-relative paths. Today `InProcessBackend` runs the catalog in the app's process
and its watcher on a thread; the daemon's RPC client will implement the same protocol, so moving to the daemon changes
that one module. Images and models are read straight from disk by path either way.

Above it, `atelier.gui.studio.Studio` is the view-model QML binds to (the selection, the filter, the load path), and
plain functions build the tree (`gui/tree.py`) and the gallery groups (`gui/gallery.py`). Backend calls run off the UI
thread through `gui/tasks.py`: catalog work in order on one thread, conversions on their own.

## Development

`atelier --project <p> --source <path> --screenshot <out.png>` renders the window once and quits (also `--export`,
`--render`, `--lighting clay`, `--wireframe`, `--collision`, `--direct`, `--frame figure`, `--tab`, `--window-size`;
none of these are in `--help`). Qt's `offscreen` platform has no 3D graphics API, so Quick 3D draws nothing there; by
default the window stays hidden on the normal platform and is grabbed offscreen. A hidden window renders no frames,
so images inside the gallery and the rail mostly never arrive, and only its first grab is reliable. For a faithful
screenshot add `--screenshot-shown` and launch it where it cannot be seen, such as a silent Hyprland special
workspace:

```
hyprctl eval "hl.exec_cmd('<command>', { workspace = 'special:atelier silent', float = true, size = '1600 1000' })"
```

The tests run on `offscreen` without a display: the view-model against a fake backend, the tree and gallery
functions, and a check that every QML file loads without a warning.
