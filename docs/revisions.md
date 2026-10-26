# Revisions

Version control can't show turn-by-turn changes: Diversion only records commits, and some projects ignore their
render PNGs. Atelier therefore keeps its own history.

## Snapshots

A **revision** is a manifest of one source's files at a moment: every path mapped to its sha256. The bytes live once
each in `.atelier/objects/<sha256>`, so an unchanged file costs nothing in a new revision.

The `.blend`, builder, README, exports and the project's own renders are all snapshotted. A revision is recorded:

- after every worker turn on the source;
- when the watcher sees the source's files change and settle outside a worker turn, for example after a manual edit or
  a rebuild from the terminal.

`.blend` files are large, so `.atelier/objects/` can be pruned to the last N revisions per source (`atelier prune`).
Objects still referenced by a kept revision are never removed.

## Standard renders

Atelier renders a fixed set itself, the same way for every source in every project, so any two revisions can be
compared and a manager always has the same views to judge.

After a revision whose exports changed, the daemon runs a fixed headless-Blender script per changed export. It imports
the export, frames it by its bounds and renders:

| View    | Camera                       |
|---------|------------------------------|
| `hero`  | Three-quarter, slightly high |
| `front` | Orthographic front           |
| `side`  | Orthographic side            |
| `top`   | Orthographic top             |
| `clay`  | Hero angle, clay material    |

Each is 1024 by 1024 on a neutral backdrop with fixed three-point lighting, written to
`.atelier/renders/<revision>/<export>-<view>.png`. Workers are never asked to produce these, so they cannot drift.

## Compare

The filmstrip lists a source's revisions, newest first. The stage follows the newest revision until an older one is
picked. Compare shows two revisions side by side: their matching standard renders, or two viewports with linked
cameras.

## Restore

Restore writes a whole earlier revision back into the project: the `.blend`, exports, README and builder together, so
a stale `.blend` is never paired with a newer builder. Restoring a single file would invite exactly that mismatch.

- It is allowed only while no worker is running on the source.
- It first records the current state as a revision, so a restore can itself be undone.
- It marks the source as restored, and the next worker turn on it is told which revision it was restored to.

You and the manager can both restore (see `mcp.md`).
