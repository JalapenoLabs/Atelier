# Catalog

## Sources

The unit of review is a **source**: one `.blend` file, plus the builder script that generates it when there is one.
Everything else hangs off a source:

- **Exports**: the FBX files it produces (`SM_*`, `SK_*`), picked between in the viewer.
- **Renders**: images the project's own tooling writes (review shots, previews, contact sheets).
- **README**: shown beside the stage when present.

Status, worker sessions, feedback and revisions all attach to the source, because the source is what an agent edits.
Markup can still point at one export. A kit (one `.blend` that exports many meshes, like CruiseTycoon's `Rooms.blend`)
is one source with many exports.

Statuses are `draft`, `in-review`, `changes-requested` and `approved`. They live only in `state.db`. A new source
starts as `draft`. A source whose `.blend` disappears is marked missing rather than deleted, so its status survives a
file that is briefly gone during a save or a move.

## Discovery

Atelier finds sources by convention, with no config needed for the common layout of one folder per asset:

- Every `*.blend` under the configured roots (default `SourceArt/`) is a source. `*.blend1`, `*.blend2` and `*.blend@`
  are ignored.
- Folders starting with `_` (`_lib`, `_scripts`, `_tools`) and `__pycache__` are never walked into, at any depth, so
  nothing below them is a source.
- A source's files are those in its own folder plus the whole trees of `Export/`, `Exports/`, `Review/` and `Renders/`
  beside it. Other subfolders (`Source/`, `Textures/`, `scripts/`) are not part of it.
- Of those files, every `*.fbx` is an **export**. An image (`png`, `jpg`, `jpeg`, `webp`, `exr`) is a **render** when it
  sits in `Review/` or `Renders/` or is named `*_preview*` or `*_contact_sheet*`; textures beside the exports are not.
- A folder can hold several kits (CruiseTycoon's `Ship/` holds `DeckKit.blend` and `Hull.blend`). A file named after
  one of them (`Hull_preview_port.png`, `DeckKit_pieces.png`) belongs to that kit alone; the folder's other files,
  such as a shared `Export/`, belong to each kit in it. A `[sources."…"].outputs` override separates them exactly.
- Its builder is the one `build_*.py` in the same folder. Otherwise, as in a folder of several kits, each `.blend`
  pairs with the builder named after it: `DeckKit.blend` with `build_deck_kit.py`, or failing that its one
  `build_deck_kit_*.py` (`DiningSpa.blend` with `build_dining_spa_rooms.py`). A script goes to the longest kit name it
  matches, so `build_dining_spa_rooms.py` never goes to a `Dining.blend` beside it, and a kit no script is named
  after gets none of its neighbours'. Failing all that, the builder is the script its README's rebuild command runs
  with `--python` (AnchorFall's `SourceArt/_lib/run_asset.py`).
- Roots may overlap; each folder is still read once, so every source is listed once.
- Its README is the `README.md` in its folder.

A file watcher keeps the catalog live. It watches the roots, the folders `references.globs` and `outputs` overrides
draw from, and `atelier.toml`. A batch of changes is held until it has been quiet for 1.5 seconds (and at most 30
seconds), then the catalog rescans. Each settled batch reports the sources added, removed or changed, and the sources
one of whose files was written or deleted, which is what revisions are recorded from. Changes to
`.blend1`/`.blend2`/`.blend@` backups, `_` folders and `.atelier/` are ignored. Saving `atelier.toml`, in place or by
rename, rescans and restarts the watch on the current paths; every start of the watch rescans first, so nothing
changed in between is missed. A failed rescan is logged and the watch goes on. The watch ends when it is stopped or
interrupted (Ctrl+C).

## Scanning

`atelier scan <project>` scans a project into its catalog and lists every source with its builder, README, exports
and renders. `<project>` is the project's folder or the name of a project scanned before (see the registry in
`architecture.md`). `--watch` then keeps the catalog live and prints each settled change.

The scan writes `.atelier/state.db` and adds `.atelier/` to the project's ignore files; a `--state-dir` inside the
project is the folder added instead. To look at a project without writing anything into it, pass
`--state-dir <folder outside the project>`, which also leaves its ignore files alone,
and `--config <file>` to try an `atelier.toml` before committing it to the project.

## `atelier.toml`

The project config at the project root overrides conventions where a layout does not fit them. It is versioned with
the project.

```toml
roots = ["SourceArt"]

# Paths many sources build from; see workers.md for how writes to them are handled.
shared = ["SourceArt/_lib/**", "SourceArt/**/_scripts/*.py", "SourceArt/Palette/**"]

[references]
globs = ["SourceArt/References/**", "SourceArt/Concepts/**", "References/**", "docs/reference/**"]
drop_folder = "SourceArt/References"

# Kits whose outputs live outside the .blend's folder.
[sources."SourceArt/Rooms/Rooms.blend"]
outputs = ["SourceArt/Rooms/{Cabin,Buffet,Pool,Bar,KidsClub,Decoration,Common}/**", "SourceArt/Rooms/Rooms_contact_sheet*.png"]
```

Every key is optional. The values shown for `roots`, `shared`, `references.globs` and `references.drop_folder` are the
defaults. Unknown keys are an error, so a typo fails the scan instead of silently changing discovery. Globs are
project-relative and support `**` and `{a,b}`.

`outputs` **replaces** the conventional exports and renders of that source with the files its globs match, classified
by the same export and render rules. The source's `.blend`, builder and README are still found by convention. An
override naming a path that is not a source is reported by the scan as a warning.

## References

References are project-wide images an asset is built against: concept art, palettes, style targets. The references
rail lists every image matched by `references.globs`, tagged by the name of the folder it sits in (`Creatures`,
`Weapons`, `Palette`). Each scan stores them in `state.db`.

- Dragging an image into the rail copies it into `references.drop_folder`, so it is part of the project and versioned.
- A prompt can **pin** references; the worker receives their paths with the prompt.
- A manager can list, search and pin references through MCP (see `mcp.md`).
