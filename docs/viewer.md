# Viewer

The desktop app, `atelier` with no command. It is a PySide6 / Qt Quick window over the catalog, a client of the
daemon (`atelierd`, see `architecture.md`).

```
uv run atelier                                      # reopens the last project
uv run atelier --project /path/to/Project          # open any project folder by its path
uv run atelier --project Project                   # shortcut: the name of a project opened before on this machine
uv run atelier --socket <path>                      # a daemon on another socket than $XDG_RUNTIME_DIR/atelier.sock
uv run atelier --project <path> --state-root <dir>  # a daemon the window starts keeps state in <dir>/<name>/
```

Until the daemon answers, the window shows a page saying so, with **Start atelierd** and **Try again**.

## Window

Two modes, switched in the header: the **Studio**, a three-pane view of the catalog, and **Review**, a grid of the
project's review queue (see "Review mode" below). The Studio:

```
┌ Atelier  AnchorFall ▾  status message ────────────────────────────────────── [2 approvals] ┐
│ Sources      │ Carbine [In review ▾]          Export [SK_Carbine ▾] │ [Chat] [Refs] [README] │
│ filter…      │  ┌ stats ┐               [Studio Clay … Views ▾]     │                        │
│ All statuses │  │ 3D viewport, a render, compare, or markup       │ │ worker chat, composer  │
│ ▾ Weapons    │  └────────────────────────────────────────────────┘ │                        │
│  ▾● Carbine 2│  render gallery: standard renders, then by stem      │                        │
│     SK_Carb… │  revision filmstrip     [Newest] [Compare] [Restore] │                        │
└──────────────┴──────────────────────────────────────────────────────┴────────────────────────┘
```

- **Header**: the project picker (every project in the machine's registry), the mode (Studio, or Review with the number
  of tickets waiting), the latest status message, and the approvals badge: every project's worker tool calls waiting for you, listed as approval cards in its popup
  (see `workers.md`).
- **Sources** (left): the catalog as a file tree: folders, then sources, then each source's exports as its
  children. A top folder every source shares (`SourceArt/`) is left out, and a folder holding only a source of its
  own name (`Carbine/Carbine.blend`) shows as that source. A source row carries its status colour and export count.
  Folders start open and sources closed; what you open and close is remembered per project. Clicking an export
  selects its source and shows that export on the stage, as the Export picker does; clicking a source shows its first
  export; the arrow, or a double click on a source, opens and closes a row. The keyboard works as in any tree: Up,
  Down, Page Up, Page Down, Home and End move, Right opens a row or steps into it, Left closes it or steps out to its
  parent, and Enter or Space acts as a click. Filters by text and by status. The text matches anywhere in a source's
  path or in an export's name; while text is filtered every folder shows open, and a source whose exports match
  shows open with just those exports.
- **Stage** (centre): the selected source's name, its status (settable here) and the export picker, over the 3D
  viewport, a render full size, or two revisions compared side by side, with the render gallery and the revision
  filmstrip under it (see `revisions.md`).
- **Context** (right): tabs for the source's worker chat (see `workers.md`), the references rail, and its README
  rendered as Markdown.

Panel sizes, the selected tab, the last project, the mode, the viewport's display settings and each project's open tree
rows
(`sourceTree/<project>/`) are remembered per machine, in Qt's settings (`~/.config/JalapenoLabs/Atelier.conf`).
Everything else the window shows comes from the core, apart from its colours, which come from a theme file and
follow it live (see `theming.md`).

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

- **Grid**: a grid on the ground (Unreal Z = 0), every 10 cm for a model up to 3 m across and every 1 m beyond,
  with every tenth line stronger and the X (red) and Y (blue) axes, fading towards the horizon. It is drawn in a
  view of its own under the model's, in the theme's colours (see `theming.md`).
- **Scale reference**: a 180 cm figure standing on the ground off the model's +X -Y corner, about as far from the
  default camera as the model itself.

### Camera

The viewport navigates like Blender's default: a turntable orbit around an invisible pivot, with no roll. Yaw always
turns about the vertical and pitch stops at straight down and straight up, so the view never passes over a pole and
flips.

| Input                                     | Does                                                                          |
|-------------------------------------------|-------------------------------------------------------------------------------|
| Middle drag, or left drag                 | Orbit the pivot; the model turns with the mouse (0.4 degrees a pixel)         |
| Shift+middle drag, Shift+left, right drag | Pan: the pivot slides in the view plane, the model following the mouse        |
| Ctrl+middle drag, Ctrl+left, wheel        | Zoom: drag down or wheel up to dolly toward the pivot, never nearer than 1 cm |
| Numpad 1, 3, 7                            | Front, right and top; with Ctrl, back, left and bottom                        |
| Numpad 5                                  | Perspective or orthographic                                                   |
| Numpad 4 and 6, 8 and 2                   | Step the camera 15 degrees left and right, up and down                        |
| Numpad 9                                  | The opposite side (180 degrees about the vertical)                            |
| Numpad + and -                            | Zoom one wheel notch                                                          |
| Numpad ., Home                            | Frame the model from the current direction                                    |
| F, Shift+F                                | Frame the model (with the 180 cm figure) from the default three-quarter view  |

The left and right buttons are conveniences for trackpads and mice without an easy middle button; the viewer has no
selection for them to conflict with. A drag keeps the gesture it started with. Each numpad key also answers to the key
it sends with Num Lock off (End for 1, and so on), and the top-row digits are not views.

Front, right and top are exactly the `front`, `side` and `top` standard renders (see `revisions.md`): front from +Y,
right from +X with +Y on the image's left, top from above with -X up the image. As with Blender's Auto Perspective, an
axis view turns orthographic by itself and orbiting off the axis turns it back to perspective; orthographic chosen with
numpad 5 stays. The orthographic camera shows the height the perspective one sees at the pivot, so switching keeps the
model's size. The view's name ("Front orthographic", "User perspective") shows above the hint.

The default three-quarter view looks from the model's -X -Y side, 22 degrees above. The camera is framed that way
when an export is first shown and kept when the same export reloads. Compare's two 3D viewports look through one
shared camera, so navigating either moves both.

The camera is `atelier.gui.camera.ViewportCamera`, registered for QML as `import Atelier`: the pivot, yaw, pitch,
distance and projection, and the slots that move them, as plain math that is unit-tested. `ViewportNavigation.qml`
maps the mouse and keys onto it.

**Camera bookmarks** (**Views**) are saved per source, by name, in `state.db` (`camera_bookmarks`), so a manager can
use the same views later. Each holds the orbit target in Unreal centimetres, the yaw (about the vertical,
counterclockwise seen from above: 0 on the target's +Y side, 90 on its +X side), the pitch (negative looks down) and
the distance in cm. A bookmark keeps no projection: applying one leaves an orthographic view chosen with numpad 5 as
it is, and an automatic one turns perspective unless the bookmark looks along an axis.

### Display

- **Studio** lighting: three directional lights fixed to the camera (a key from above left, a fill from the right, a
  rim from behind), so a model reads the same from every side. **Clay** keeps the lights and swaps every material for
  a neutral grey.
- **Wireframe**: every mesh drawn as its triangle edges only (Qt Quick 3D's wireframe debug mode), the figure too.
- **Collision**: `UCX_*` hulls are hidden by default and shown as a translucent green overlay.
- **Stats** (top left): triangles and materials (collision hulls left out), the bounds in centimetres along Unreal's
  X, Y and Z, the number of collision hulls, whether the mesh is skinned, the load path and the grid spacing. They
  come from Blender's measurement, so they are the same whichever path loaded the model. The camera frames on the
  same bounds, using the loaded meshes' own bounds until the measurement arrives.

## Render gallery

Under the stage, the stage revision's **standard renders** of the selected export come first, as the `Standard`
group (or a tile saying they are rendering, failed, or not rendered yet; see `revisions.md`). Then every render of the
source, grouped by **stem**, each group labelled. A render's stem is the longest
`_`-separated prefix of its file name, the whole name included, that another render's name equals or continues:
`Carbine_Front` and `Carbine_Side` group as `Carbine`; `CB_CargoBooster` and `CB_CargoBooster_2` as
`CB_CargoBooster`; `Hull_preview` and `Hull_preview_port` as `Hull_preview`; `Hair_Braid_Front` and
`Hair_Braid_Back` as `Hair_Braid`. A render sharing no prefix is a group of its own. Projects name renders too freely
for a fixed list of view words, so the groups come from the set itself.

Clicking a render shows it full size on the stage; **3D view** goes back. A source with renders but no exports opens
on its first render. **Mark up**, on a render shown full size or on the viewport's toolbar, opens the stage's markup
editor (its fifth page) on the render or a frozen frame of the viewport (see `workers.md`).

## Revision filmstrip

Under the gallery, in the same split, the selected source's revisions, newest first, and the buttons to go back to the
newest, compare and restore. Picking a revision, compare and restore are described in `revisions.md`. Compare is the
stage's fourth page: two images, or two more `Viewport`s that take their model, stats and display settings from the
compare state and the stage viewport instead of the studio, with their orbits kept equal.

## References rail

The **Refs** tab lists every reference image of the project (see `catalog.md`), tagged by folder. Dropping image files
onto it copies them into `references.drop_folder` and rescans, so they appear at once. A file of the same name already
there is never overwritten: the same image is not copied again, and a different one gets the next free name
(`concept-2.png`). With a source selected, **Pin** on an image pins it to that source's next worker prompt.

## Review mode

The project's review queue (see `review.md`) as a grid, one tile per ticket, with a tab per configured queue (Art, UI,
Audio) showing how many tickets it holds. It is built for working through a hundred tickets without the mouse.

```
┌ Atelier  Waystead ▾  Studio [Review 24]  status message ──────────────────────────── [0 approvals] ┐
│ [Art 24]  UI 0  Audio 1                       5 selected  [Approve] [Request changes] [Refresh] │
│ ┌────────────────┐ ┌────────────────┐ ┌────────────────┐ ┌────────────────┐                     │
│ │ Approve·Queued │ │                │ │ Changes·Failed │ │                │                     │
│ │   hero image   │ │   hero image   │ │   hero image   │ │ 1 file, no     │                     │
│ │                │ │                │ │                │ │ image          │                     │
│ │ WAY-100 Carbine│ │ WAY-101 Pistol │ │ WAY-102 Wall   │ │ WAY-103 Door   │                     │
│ │ [Art asset]    │ │ [Art asset]    │ │ [Art asset]    │ │ [Audio]        │                     │
│ └────────────────┘ └────────────────┘ └────────────────┘ └────────────────┘                     │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

- **A tile** shows the ticket's hero image, its id, summary and tags, and its decision job's state as a badge
  (queued, writing, posting, posted or failed, with the verdict: "Approve · Writing"). A failed badge says why on
  hover and offers **Retry** (`review.retry`). A job decided on an earlier delivery than the ticket's current one
  shows no badge.
- **The hero image** is the first image of the latest delivery (by its type, or by its name when the tracker gave no
  type). A ticket with no image in its delivery shows the newest standard `hero` render of the source it links to, if
  that has been rendered; the grid never starts a render. Otherwise the tile says what it has ("1 file, no image",
  "No delivery yet").
- **Thumbnails**: each hero is decoded once, straight to at most 480 pixels on its longest side (Qt's
  `QImageReader.setScaledSize`), and kept as a PNG in `<state>/cache/thumbnails/`, named by the image's path, size and
  modification time, so an unchanged image is never decoded again and a changed one gets a new thumbnail. Finding and
  making them runs on a pool of four threads of its own, so a queue of a hundred or more tickets fills in as they
  arrive without holding up the window, and the grid loads its images asynchronously. An image that cannot be decoded
  (deliveries are untrusted files) leaves its tile with a note instead.
- **Following the queue**: the grid reads the queue when the project opens and again on every queue event, and badges
  tiles as decision job events arrive. A ticket leaves the grid when its decision posts (the daemon's `decided`
  event). Tiles are removed, added and updated in place, never rebuilt, so the grid keeps its scroll, its selection
  and the cursor's place. A refresh that fails keeps the tiles and says why above them. **Refresh** (F5) reads the
  tracker again now. A project with no `[review]` table says so.

### Selecting and deciding

| Input                           | Does                                                                          |
|---------------------------------|-------------------------------------------------------------------------------|
| Arrows, Home, End               | Move the cursor (the outlined tile); the grid starts with it on the first tile |
| Shift+arrows                    | Select from the last tile picked to the cursor                                 |
| Space                           | Select or unselect the tile under the cursor                                   |
| Ctrl+A                          | Select every tile of the tab                                                   |
| Esc                             | Select nothing                                                                 |
| Click, Ctrl+click               | Select one tile; toggle one                                                    |
| Shift+click, Ctrl+Shift+click   | Select the range from the last tile clicked; add it to the selection           |
| Drag on the grid                | A selection box: what it covers is selected (with Ctrl, added), scrolled-off tiles included |
| A, **Approve**                  | Approve, with an optional short note                                           |
| C, **Request changes**          | Request changes, with one note for every ticket                                |
| Enter, double click             | Open the ticket in focus mode                                                  |
| F5, **Refresh**                 | Read the queue from the tracker again                                          |

A decision covers the selection, or the tile under the cursor when nothing is selected. A or C opens its **note**: the
reviewer's takes, verbatim and in order, above a field for typing, whose text is the last take. Enter sends it
(Shift+Enter starts a new line); Esc puts it away and keeps it for next time. An approval with no note posts
"Approved."; a request for changes needs words. Several tickets share one note, which makes it a bulk decision: one
`review.decide` call, and each ticket's comment is written for it by its own job. Tickets that already have a decision
on its way are left out, and the status message says how many. The decided tickets show their queued badge at once
and leave the selection. Switching tabs starts a fresh selection.

The note is `atelier.gui.review.DecisionNote`: `addTake(text)` adds a take exactly as given, and `removeLastTake()`
drops the newest. Voice notes add each transcribed recording through `addTake`, so typed and spoken words end up in
the same ordered takes.

### Focus mode

Opening a tile reviews its ticket alone, large, as fast as the reviewer can think. The board's `focusedTicket` is the
ticket shown; the page is `ReviewFocusPage.qml` over `atelier.gui.focus.ReviewFocus` (`studio.reviewFocus`).

```
┌ [Back to the grid]  WAY-60  [Art] Carbine     Changes · Queued   1 of 24  [Approve] [Finish with changes] ┐
│ (while drawing: Pen Highlighter Eraser Arrow Box · colours · S M L · Undo Redo Clear   [Discard] [Keep]) │
│ ┌ hero.png 1600 × 1200 ──────────────────────────────────────────┐ │ Notes                  2 takes │
│ │                                                                 │ │ [take 1, editable]             │
│ │   the stage: an image, the 3D view, a sound, or a drawing       │ │ [take 2, editable]             │
│ │                                                                 │ │ [the note being typed]         │
│ └─────────────────────────────────────────────────────────────────┘ │ Drawings  [▣ ×] [▣ ×]          │
│ [hero] [compare] [detail] [3D] [Sound] [File]                         │ The delivery says …            │
└───────────────────────────────────────────────────────────────────────┴───────────────────────────────┘
```

- **The stage** shows the latest delivery's images, the hero (its first image, as on the tile) first, then compare
  sheets (an image whose name says `compare`), then the rest; then the 3D view of the source the ticket links to;
  then its sounds, with a play button; then any other file by name. The filmstrip under it lists them all. The 3D
  view is the studio's own viewport state: showing it selects the linked source on the first export the ticket file
  names, so it loads, converts and caches exactly as in the Studio.
- **The note** beside the stage is the ticket's own `DecisionNote`, kept per ticket while the reviewer moves back and
  forth: the takes in order, each editable in place, and the field being typed, which is the last take. General
  notes ("overall", "for all the weapons") stay in the words; the decision job splits them out (see `review.md`).
  The panel also lists the drawings the decision will carry, each removable, and the delivery comment's own words.
- **Moving on never waits.** A and C submit the decision through the board (`ReviewBoard.submit`, one
  `review.decide` in the background) and open the next ticket of the tab with no decision on its way at once,
  wrapping around to the start; with none left, the grid comes back. The decided ticket counts as busy from that
  moment, so it is never decided twice, and its badge follows its job. Once the daemon has queued the job, the
  ticket's note is cleared and its draft folder deleted. A refused decision keeps both and says why in the status.
- **Ahead of the reviewer**, the images of the ticket shown and the next two are decoded by invisible images, a fixed
  set of slots (`prefetchSlots`) in which an image keeps its slot as long as it is wanted, so stepping on never lets
  go of an image about to be shown and the stage finds it in Qt's image cache. The filmstrip shows those same
  images, so nothing is decoded twice.

| Input                     | Does                                                                              |
|---------------------------|-----------------------------------------------------------------------------------|
| Left, Right               | The previous and next ticket of the tab                                            |
| Up, Down, a filmstrip click | The previous and next thing on the stage, wrapping around                       |
| A, **Approve**            | Approve, with the note and drawings if there are any, and open the next ticket     |
| C, **Finish with changes** | Request changes with the note and drawings, and open the next ticket; with neither, the note's field takes the keyboard |
| D                         | Draw on the image shown, or on a frame frozen from the 3D view; D, Enter or Esc keeps the drawing |
| B                         | Sketch on a blank canvas                                                           |
| N                         | Write in the note; Esc gives the keys back to the page                              |
| Space                     | Play or pause the sound shown                                                      |
| Esc                       | Keep an open drawing; otherwise back to the grid, the cursor on this ticket        |

While a drawing is open: P pen, H highlighter, E eraser, 1 to 3 the line size, Ctrl+Z and Ctrl+Shift+Z undo and redo.
A drawing opens on the pen. Moving to another ticket or entry, deciding, or going back to the grid keeps a drawing in
progress first, so nothing drawn is lost; only **Discard** drops one.

**Drawings** use the markup editor's drawing layer (`gui/markup.py`'s `Drawing`, `DrawingSurface.qml` and
`DrawingTools.qml`; see `workers.md`): pen, highlighter, eraser, arrow and box, six colours, three sizes, undo and
redo. A blank canvas is a 1920 × 1080 white sheet that starts in ink. Keeping a drawing flattens it onto its image at
full resolution and writes it as `drawing-<n>.png` into the ticket's **draft folder**,
`<state>/review/<ticket>/draft/<delivery comment id>/`, which holds what the window makes for the ticket's next
decision. The decision carries those files (`DecisionRequest.drawings`), and the daemon copies them into the job's own
folder. A draft made for an earlier delivery is deleted when the ticket opens on a newer one.

Voice notes are on the roadmap (#42): each transcribed recording becomes a take of the shown ticket's note through
`ReviewFocus.current_note.addTake`, the recordings go into `ReviewFocus.draft_folder(ticket)`, and the recording
indicator sits in the note panel's header (`focusNotesHeader`).

## Between the window and the core

The window reaches the core through exactly one interface, `atelier.gui.backend.StudioBackend`: reach the daemon and
follow its events (the open project's catalog changes, revisions and worker output, every project's approvals, and
losing the daemon, on one stream, with the open project's review queue and decision jobs), open a project, list sources and references, set a status, keep bookmarks, add a
reference, convert an export, read a source's revision history, ask for standard renders, convert a revision's
export, restore, drive a source's worker, pin references, store feedback, answer approvals, and read the review
queue, refresh it, submit decisions, list decision jobs and retry one. It speaks only in core
models and project-relative paths, plus absolute paths into the state folder (converted models, standard renders,
objects, transcript and feedback images, review deliveries), and it names the open project's state folder, where
the window keeps its review thumbnails. `RpcBackend` implements it over the daemon's socket (see
`architecture.md`). Images and models are read straight from disk by path, never sent over the socket.

Above it, `atelier.gui.studio.Studio` is the view-model QML binds to (the daemon's state, the selection, the filter,
the tree's open rows and the row the keyboard is on, the load path, the staged revision and compare, the approvals
list), with `gui/chat.py` (the selected source's worker and chat), `gui/markup.py` (the drawing layer, the markup
editor, focus mode's drawings, and flattening markup onto its image), `gui/review.py` (the Review mode's
`ReviewBoard` and its `DecisionNote`) and `gui/focus.py` (focus mode, `ReviewFocus`) beside it, and
plain functions build the tree (`gui/tree.py`), the gallery groups (`gui/gallery.py`) and the filmstrip rows
(`gui/revisions.py`). Backend calls run off the UI thread through `gui/tasks.py`: catalog work in order on one
thread, conversions and review thumbnails (`gui/thumbnails.py`) on pools of their own. Show in Blender is the
window's own (`gui/desktop.py`), and so is the theme (`gui/theme.py`).

## Development

`atelier --project <p> --source <path> --screenshot <out.png>` renders the window once and quits (also `--export`,
`--render`, `--lighting clay`, `--wireframe`, `--collision`, `--direct`, `--frame figure`,
`--view front|back|right|left|top|bottom` (a numpad view after framing), `--tab`, `--window-size`,
for revisions `--revision <n>`, `--compare renders|models`, `--compare-base <n>`, `--compare-view <view>`, and for
markup `--markup` with `--render <name>` and `--markup-file <markup.json>`; none of these are in `--help`). It needs a
running daemon; `--tab chat` waits for the source's chat. Qt's `offscreen` platform has no 3D graphics API, so Quick 3D draws nothing there; by
default the window stays hidden on the normal platform and is grabbed offscreen. A hidden window renders no frames,
so images inside the gallery and the rail mostly never arrive, and only its first grab is reliable. For a faithful
screenshot add `--screenshot-shown` and launch it where it cannot be seen, such as a silent Hyprland special
workspace:

```
hyprctl eval "hl.exec_cmd('<command>', { workspace = 'special:atelier silent', float = true, size = '1600 1000' })"
```

The tests run on `offscreen` without a display: the view-model against a fake backend, the tree and gallery
functions, the camera's math and the mouse and keys driving it (`tests/test_gui_camera.py`), the review grid's tiles,
thumbnails, selection and decisions, with its page driven by real keys and mouse events (`tests/test_gui_review.py`;
on `offscreen` a shown window renders 2D frames, though nothing appears on screen), focus mode's stage, moving on,
decisions, drawings and flattening, with its page drawn on with the mouse and driven by keys
(`tests/test_gui_focus.py`), and a check that every QML file
loads without a warning against a real daemon run in the test process.
