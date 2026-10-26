# Revisions

Version control can't show turn-by-turn changes: Diversion only records commits, and some projects ignore their
render PNGs. Atelier therefore keeps its own history.

The code is `atelierd.revisions`: `Revisions` (the synchronous use cases), `RevisionTracker` (running them in the
background), the object store, the `state.db` tables and the standard render script.

## Snapshots

A **revision** is a manifest of one source's files at a moment: every path, with what part of the source it is
(`blend`, `builder`, `readme`, `export`, `render`), mapped to its sha256. The bytes live once each in
`.atelier/objects/<sha256>`, so an unchanged file costs nothing in a new revision. A source's revisions are numbered
r1, r2, ... in order.

The files snapshotted are the source's files as the catalog lists them: the `.blend`, builder, README, exports and the
project's own renders. A file is hashed while it is copied into the store, in one read, then renamed to its hash; an
object is never written twice and is made read-only. `state.db` remembers each file's hash with its size and
modification time, so an unchanged file is not read again.

A revision is recorded with a **trigger** saying why:

| Trigger       | When                                                                                         |
|---------------|----------------------------------------------------------------------------------------------|
| `baseline`    | A source's first revision, recorded when Atelier opens the project and the source has none    |
| `change`      | The watcher saw the source's files change and settle (a manual edit, a rebuild from the terminal), or they changed while the project was closed |
| `worker-turn` | A worker turn on the source ended                                                            |
| `pre-restore` | Just before a restore, so the restore can be undone                                          |
| `restore`     | The state a restore wrote, naming the revision it came from                                  |

Recording compares the files with the source's newest revision and records nothing when they are the same, inside
one SQLite transaction, so a settled change that left a source as it was (or two processes noticing the same change)
never adds a revision. Opening a project catches up in the background: every present source with no revision gets a
baseline, and one whose files differ from its newest revision gets a `change`.

The calls that record are `Revisions.record(source, trigger)` and, for the daemon, `RevisionTracker.record`, which
also queues the standard renders the new revision needs. Both return the new revision, or None when nothing changed.

The window records through the catalog watcher it runs, and so does `atelier scan <project> --watch`, which prints
each revision as it is recorded.

## Standard renders

Atelier renders a fixed set itself, the same way for every source in every project, so any two revisions can be
compared and a manager always has the same views to judge.

`revisions/blender_scripts/standard_renders.py` runs in headless Blender through `atelierd.blender.run_blender_script`
(factory settings, `nice`, a timeout), once per export, rendering every view in one run. It imports the export from
the object store, with the scene in centimetres; a texture the import cannot find is looked for by name in the
export's own folder in the project. Collision hulls (`UCX_*`) are hidden, and every view is framed by the bounds of
the remaining meshes:

| View    | Camera (Unreal's frame: X forward, Y right, Z up)                                           |
|---------|----------------------------------------------------------------------------------------------|
| `hero`  | Perspective, 40 degree field of view, from the -X -Y side 22 degrees up: the viewer's default framing |
| `front` | Orthographic from +Y, facing the asset (Unreal skeletal meshes face +Y; Blender's front view) |
| `side`  | Orthographic from +X, the asset's left side, +Y on the image's left                          |
| `top`   | Orthographic from above, -X up the image, so +Y is on the left as in the side view           |
| `clay`  | The hero camera with every material replaced by a neutral grey                              |

Each is 1024 by 1024, rendered with EEVEE at 32 samples and Blender's Standard view transform, on a mid-grey backdrop
the camera sees while the model is lit by a dimmer ambient. Three sun lights keep fixed places relative to the camera
in every view: a key from above left, a fill from the right and a rim from behind. Suns light the same whatever the
model's size. A run is held to 4 render threads.

They are written to `.atelier/renders/<revision id>/<export>-<view>.png`, where `<export>` is the export's file stem
(with its folders prepended when two exports of the source share a stem). Beside them, `<export>.json` holds the
renderer's version and the hash of the export rendered, and is written last, after the PNGs are renamed into place
from a partial folder, so a set with its JSON is complete. A set from another renderer version counts as never
rendered. If Blender fails, the JSON records the error instead and those bytes are not rendered again until the
renderer changes; a machine with no Blender records nothing.

Which exports are rendered:

- Recording a revision first shares every set another revision of the source already has for the same export bytes:
  the files are hard-linked into the new revision's folder (copied where a link is not possible). An unchanged export,
  or one a restore brought back, is never rendered twice, and each revision's folder is whole on its own.
- Then the exports still without a set that changed since the previous revision are rendered. A baseline's exports
  are not rendered until something asks for them: the window asks for the revision and export it is showing.
- A set rendered for one revision is linked into every other revision holding the same bytes.

The `RevisionTracker` renders in the background, at most two at a time (`STANDARD_RENDER_WORKERS`), each Blender at
low priority; a set queued or running shows as pending. Workers are never asked to produce these, so they cannot
drift.

## Compare

The filmstrip under the render gallery lists the selected source's revisions, newest first, each with its number,
trigger, time and the hero render of the selected export. The stage follows the newest revision (the project's files
as they are) until an older one is clicked. An older revision is shown as snapshotted: its exports converted to
`.glb` from the object store through the export cache (by hash, so a revision's export is converted once), its renders
read from the store, and its standard renders as the gallery's first group. **Back to newest** follows the newest
again.

**Compare** shows two revisions side by side: A, by default the revision before the stage's, on the left, and B, the
stage's, on the right. Right-clicking a revision in the filmstrip makes it A. Compare shows either the same standard
view of both (`hero`, `front`, `side`, `top` or `clay`) or, in **3D**, both exports as models in two viewports whose
cameras move together, with the stage viewport's display settings.

## Restore

Restore writes a whole earlier revision back into the project: the `.blend`, exports, README, builder and renders
together, so a stale `.blend` is never paired with a newer builder. Restoring a single file would invite exactly that
mismatch. Each file is copied from the store to a temporary name beside its target and renamed over it, keeping the
replaced file's permissions. Files the revision did not have are deleted, so the source's files end up exactly the
revision's.

- Files shared with other sources are never written or deleted by a restore: anything matching `shared` in
  `atelier.toml`, and a builder outside the source's own folder (a README's rebuild command running a project-wide
  script, like AnchorFall's `SourceArt/_lib/run_asset.py`). They are still snapshotted, so a compare shows them, and
  the restore reports them as skipped.
- It is allowed only while no worker is running on the source: `Revisions` takes an `is_source_busy` check and
  refuses a busy source. Until workers exist nothing is ever busy.
- It first records the current state as a `pre-restore` revision (or reuses the newest when nothing changed since),
  so a restore can itself be undone by restoring that revision. It then records the restored state as a `restore`
  revision naming the revision it came from.
- It marks the source as restored (`restore_marks` in `state.db`). The mark names the revision restored to and stays
  until a worker turn has been told about it: `Revisions.get_restore_mark` reads it for the turn's brief and
  `clear_restore_mark` removes it.

In the window, **Restore rN…** in the filmstrip restores the revision on the stage after a confirmation. A manager
can restore through MCP (see `mcp.md`).

## Prune

`.blend` files are large, so `atelier prune <project> --keep N` (default 20) deletes all but each source's newest N
revisions. A revision a source is marked restored to is kept too. Objects no remaining revision names are deleted,
and so are the render folders of deleted revisions, which shared sets make safe. Recording holds a shared `flock` on
`.atelier/objects/.lock` from storing its objects until its manifest is written, and prune holds it exclusively, so a
prune never deletes an object a recording is about to name, in another process too.

## State

| Where                                  | What                                                              |
|----------------------------------------|-------------------------------------------------------------------|
| `state.db` `revisions`                 | One row per revision: source, number, time, trigger, restored-from |
| `state.db` `revision_files`            | Each revision's manifest: path, kind, sha256, size                |
| `state.db` `file_hashes`               | The last hash of each file with its size and modification time    |
| `state.db` `restore_marks`             | Sources restored since their last worker turn                     |
| `.atelier/objects/<sha256>`            | Snapshotted bytes, read-only                                      |
| `.atelier/renders/<revision id>/`      | Standard renders and their metadata                               |
