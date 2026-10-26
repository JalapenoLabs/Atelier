# Architecture

## Processes

```
                ┌────────────── atelierd (one per user, systemd user service) ──────────────┐
  atelier GUI ──┤ Unix socket JSON-RPC        core: catalog · revisions · workers · policy   │
                │ $XDG_RUNTIME_DIR/atelier.sock                                              │
  manager    ───┤ MCP, streamable HTTP        ├── worker: claude (stream-json) ── Blender :n │
  Claude        │ 127.0.0.1                   ├── worker: claude (stream-json) ── Blender :m │
                └────────────────────────────────────────────────────────────────────────────┘
```

- **`atelierd` owns everything that runs or persists**: the catalog and file watcher, the revision store, the
  standard-render pipeline, Claude Code worker processes and each worker's Blender. Workers keep running when the GUI
  closes, and a manager session can reach them whether or not a window is open.
- **The GUI is a client.** It renders state and sends commands; it holds no state of its own beyond window layout
  (panel sizes, display settings, the last project). It reaches the core through one interface,
  `atelier.gui.backend.StudioBackend` (see `viewer.md`). Today that runs the core inside the app's process; the RPC
  client takes its place with workers.
- **One core, two thin adapters.** The GUI's RPC and the manager's MCP call the same core functions, so the two can
  never disagree about what a source or a worker is doing.

## Protocols

| Front door  | Transport                                   | Who                     | Why this transport                                    |
|-------------|---------------------------------------------|-------------------------|-------------------------------------------------------|
| GUI RPC     | JSON-RPC 2.0 over a Unix socket, mode 0600  | The `atelier` app       | No open TCP port; filesystem permissions are the auth; supports a push subscription for live events |
| Manager MCP | MCP streamable HTTP on 127.0.0.1            | Any Claude Code session | The standard way Claude sessions consume tools        |

The RPC carries a subscription stream of events: worker output, approval requests, file changes and new revisions.
Images and models never travel over either front door to the GUI: daemon and GUI share a machine, so the GUI reads
files straight from disk by path. MCP returns renders as image content, because a manager session has no disk access
guarantee.

## State

| Where                                   | What                                                         |
|-----------------------------------------|--------------------------------------------------------------|
| `<project>/atelier.toml`                | Project config, versioned with the project (see `catalog.md`) |
| `<project>/.atelier/state.db`           | SQLite (WAL): sources, statuses, camera bookmarks, revisions, feedback, sessions |
| `<project>/.atelier/objects/`           | Content-addressed snapshot bytes (see `revisions.md`)        |
| `<project>/.atelier/renders/`           | Standard renders per revision (see `revisions.md`)           |
| `<project>/.atelier/feedback/`          | Markup captures and annotated images                         |
| `<project>/.atelier/cache/`             | Exports converted to `.glb` with their stats, named by the FBX's sha256 (see `viewer.md`) |
| `~/.local/share/atelier/projects.toml`  | The machine's registry of known projects                     |

Per-project state travels with the project's drive and never mixes facts from two projects, so every path stored in
`state.db` is project-relative. `.atelier/` is added to every ignore file the project has (`.dvignore`, `.gitignore`),
or to a new one for the version control it uses (`.diversion/` or `.git/`): it is review state, not source, and
committing it would turn every click into a version-control change.

`state.db` is versioned with `PRAGMA user_version`. Each part of the daemon adds its tables as a new migration; an
applied migration is never edited.

The registry lives under `$XDG_DATA_HOME` (default `~/.local/share`). It maps each project's folder name to its path,
so a project scanned once by path can be named from anywhere. It holds no per-project state.

## Stack

- Python 3.14, pinned exactly and managed with `uv` (`pyproject.toml` plus `uv.lock`, `.python-version`). Every
  dependency and tool is pinned to an exact version: pydantic for every model and config, `wcmatch` for the `**` and
  `{a,b}` globs in `atelier.toml`, `watchfiles` for the file watcher, `tomli-w` for writing the registry; ruff, mypy
  (strict) and pytest for checks, run in CI by `.github/workflows/ci.yml`.
- PySide6 with Qt Quick and Qt Quick 3D for the GUI, as `pyside6-essentials` and `pyside6-addons` (Qt Quick 3D and
  its assimp importer live in the add-ons; the `pyside6` meta-package would also pull in WebEngine, which Atelier does
  not use). All rendering runs in Qt's native RHI (Vulkan on Linux), so Python
  costs nothing in viewport speed. Python matches the `bpy` builder scripts the projects already use.
- Blender, headless, for conversion and standard renders, plus one background instance per worker.
- The Claude Code CLI, run as a child process with the host's own login. Atelier never calls the Anthropic API itself.

## Code layout

One `uv` project, two import packages under `src/`:

| Package    | Holds                                                                                          |
|------------|------------------------------------------------------------------------------------------------|
| `atelierd` | The core: `config` (`atelier.toml`), `registry`, `project` (paths, state folder, ignore entry), `catalog` (discovery, references, `state.db` store, the `Catalog` service, the watcher), `blender` (running a script in headless Blender), `exports` (converting exports to `.glb` and measuring them, cached), `revisions` (the object store, revision manifests, standard renders, restore and prune, and the `RevisionTracker` that records and renders in the background). The daemon, its RPC and the manager MCP adapter join it here. |
| `atelier`  | Front ends: the `atelier` command line (`atelier.cli`: `scan`, `prune`), and the desktop app (`atelier.gui`: the backend interface, the `Studio` view-model, and the QML in `gui/qml/`). |

`atelier` imports `atelierd`; `atelierd` never imports `atelier`. The core neither prints nor parses arguments: its
functions take paths and models and return pydantic models, which is what keeps the CLI, the RPC and the MCP thin.
