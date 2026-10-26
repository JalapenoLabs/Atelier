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

Statuses are `draft`, `in-review`, `changes-requested` and `approved`. They live only in `state.db`.

## Discovery

Atelier finds sources by convention, with no config needed for the common layout of one folder per asset:

- Every `*.blend` under the configured roots (default `SourceArt/`) is a source. `*.blend1`, `*.blend2` and `*.blend@`
  are ignored.
- A source's files are those in its own folder, plus `Export/**`, `Review/**`, `Renders/**`, `*_preview.png` and
  `*_contact_sheet*.png` beside it.
- Its builder is a `build_*.py` in the same folder, or one named in its README's rebuild command.
- Folders starting with `_` (`_lib`, `_scripts`, `_tools`) and `__pycache__` are never sources.

A file watcher keeps the catalog live. Changes settle for a short debounce before Atelier treats a source as changed.

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

Every key is optional. The values shown for `roots`, `shared` and `references.globs` are the defaults.

## References

References are project-wide images an asset is built against: concept art, palettes, style targets. The references
rail lists every image matched by `references.globs`, tagged by the folder it sits in (`Creatures`, `Weapons`,
`Palette`).

- Dragging an image into the rail copies it into `references.drop_folder`, so it is part of the project and versioned.
- A prompt can **pin** references; the worker receives their paths with the prompt.
- A manager can list, search and pin references through MCP (see `mcp.md`).
