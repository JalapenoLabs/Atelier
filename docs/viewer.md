# Viewer

## Window

A three-pane studio:

```
┌ Atelier ─ AnchorFall ▾ ─────────────────────────────────── ⚠ 2 approvals ┐
│ Sources      │  STAGE                      │ [Chat] [Refs] [README]      │
│ ▾ Weapons    │  ┌───────────────────────┐  │ worker: Carbine             │
│   ● Carbine  │  │ 3D viewport / render  │  │ > blender: build            │
│   ○ Sidearm  │  │ / side-by-side compare│  │ > standard renders done     │
│ ▸ DevRange   │  └───────────────────────┘  │                             │
│              │  [hero][front][side][top]   │ [ prompt…                 ] │
│ filter ▾     │  rev ◀ r12 r11 r10 ▶  cmp   │ 📌 refs   ✎ markup          │
└──────────────┴─────────────────────────────┴─────────────────────────────┘
```

- **Sources** (left): the catalog as a tree mirroring the folders, each row showing its status and whether a worker is
  running. Filters by status and by text.
- **Stage** (center): the 3D viewport, a render, or a two-up compare. Under it, the render strip for the current
  revision and the revision filmstrip (see `revisions.md`).
- **Context** (right): the source's worker chat, the references rail, and its README.
- **Approvals** (title bar): a badge for tool calls waiting on you, from any worker in any project (see `workers.md`).

Panel sizes and the selected tab are remembered per machine.

## 3D viewport

The viewport shows a source's **exports**, because those are exactly what Unreal imports: you review what ships. The
`.blend` itself is never opened by the viewer.

- **Loading.** Qt Quick 3D's `RuntimeLoader` loads the FBX directly. When it fails or a model comes out wrong
  (skeletal meshes and some materials are weak spots in assimp's FBX import), the daemon converts that FBX to `.glb`
  with headless Blender, caches it in `.atelier/cache/` keyed by the FBX's sha256, and the viewer loads that instead.
  Which path a given export used is shown in the stats overlay.
- **Camera.** Orbit, pan and zoom, framed on the export's bounds. Camera bookmarks are saved per source.
- **Lighting.** Studio (three-point) and clay (untextured, neutral grey).
- **Wireframe** overlay.
- **Collision.** `UCX_*` hulls are hidden by default and toggled as a translucent overlay.
- **Stats.** Triangles, materials, and bounds in centimetres.
- **Scale reference.** A 180 cm human silhouette and a ground grid in Unreal units (1 unit = 1 cm).

Exports and the scale reference use Unreal's frame: Z up, centimetres.

## Render gallery

Every image belonging to the source is listed, grouped by stem. The daemon's standard set comes first, then the
project's own renders (review shots, previews, contact sheets). Any image opens full-size on the stage, and any image
or a frozen viewport frame can be marked up (see `workers.md`).
