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
  `atelier.gui.backend.StudioBackend`, implemented by `RpcBackend` over the daemon's socket (see `viewer.md`).
- **One core, two thin adapters.** The GUI's RPC and the manager's MCP call the same core functions, so the two can
  never disagree about what a source or a worker is doing.

## Running the daemon

```
atelierd run                       # in the foreground until Ctrl+C or SIGTERM (or: uv run atelierd run)
atelierd run --state-root <dir>    # every project's state in <dir>/<name>/, nothing written into the projects
atelierd install                   # write the systemd user unit and reload systemd; it is not enabled or started
systemctl --user enable --now atelierd.service
```

`run` takes `--socket` (default `$XDG_RUNTIME_DIR/atelier.sock`, or `ATELIER_SOCKET`) and `--http-port` (default
47817; 0 picks a free one). It refuses to start while another daemon answers on the socket, and replaces a socket a
dead one left. Stopping it (SIGINT or SIGTERM) stops every worker's Claude Code and Blender first, then the HTTP
front door.

`atelierd install` writes `$XDG_CONFIG_HOME/systemd/user/atelierd.service`, which runs this installation's Python
with `-m atelierd run` (plus any `--state-root` or `--http-port` given to `install`), with the installing shell's
`PATH` baked in (minus the virtual environment), because a user service's own `PATH` holds none of `~/.local/bin`,
where `claude` and `blender-mcp` usually live. Its log goes to the journal (`journalctl --user -u atelierd`).

**When no daemon is running**, the window says so and offers **Start atelierd**: through the unit when it is installed
and the window wants the default socket and state, otherwise as a detached `python -m atelierd run` (in a session of
its own, so it outlives the window) logging to `$XDG_STATE_HOME/atelier/atelierd.log`. The window's `--state-root`
is passed to a daemon it starts this way. If the daemon goes away while the window is open, the window says so and
offers to start it again.

## Protocols

| Front door  | Transport                                   | Who                     | Why this transport                                    |
|-------------|---------------------------------------------|-------------------------|-------------------------------------------------------|
| GUI RPC     | JSON-RPC 2.0 over a Unix socket, mode 0600  | The `atelier` app       | No open TCP port; filesystem permissions are the auth; supports a push subscription for live events |
| Worker MCP  | MCP streamable HTTP on 127.0.0.1, `/worker/mcp` | Each worker's Claude Code | The permission-prompt tool must be an MCP tool; a per-worker token says who calls |
| Manager MCP | MCP streamable HTTP on 127.0.0.1, `/mcp`    | Any Claude Code session | The standard way Claude sessions consume tools        |

The RPC (`atelierd.rpc`) is one JSON object per line. `protocol.py` declares every method once, with its params
model and result type, and both the server and the client read it. Errors the core raises on purpose (`KeyError` for
an unknown source, `SourceBusyError`, `WorkerBusyError`) cross as themselves, so the window handles a daemon failure
the way it would the core's. After `events.subscribe` (to one project or all), a client receives `event`
notifications carrying the open projects' catalog changes, new revisions, worker output and approvals. Every call
runs in a thread off the daemon's event loop, and core events, which arrive on the core's threads, are hopped onto
the loop before they are written. The HTTP front door (`atelierd.mcp.app`) is one Starlette app served by uvicorn on
the same loop; the manager MCP mounts there beside the worker server.
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
| `<project>/.atelier/feedback/`          | Markup captures and annotated images (see `workers.md`)      |
| `<project>/.atelier/transcripts/`       | Worker chats, one JSON-lines file per session, with tool images |
| `<project>/.atelier/workers/`           | Each worker's policy, MCP config and hook settings           |
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
| `atelierd` | The core: `config` (`atelier.toml`), `registry`, `project` (paths, state folder, ignore entry), `catalog` (discovery, references, `state.db` store, the `Catalog` service, the watcher), `blender` (running a script in headless Blender), `exports` (converting exports to `.glb` and measuring them, cached), `revisions` (the object store, revision manifests, standard renders, restore and prune, and the `RevisionTracker` that records and renders in the background), `workers` (the worker, its processes, the scope policy and its hook, approvals, transcripts, feedback and their `state.db` tables). The daemon: `daemon` (the core every front door calls, running it, the command line, the systemd unit), `rpc` (the GUI's front door: protocol, server, client) and `mcp` (the HTTP front door and the worker-side MCP server). |
| `atelier`  | Front ends: the `atelier` command line (`atelier.cli`: `scan`, `prune`), and the desktop app (`atelier.gui`: the backend interface and its RPC implementation, the `Studio` view-model with the chat and markup editor, and the QML in `gui/qml/`). |

`atelier` imports `atelierd`; `atelierd` never imports `atelier`. The core neither prints nor parses arguments: its
functions take paths and models and return pydantic models, which is what keeps the CLI, the RPC and the MCP thin.

## The core every front door calls

`atelierd.daemon.core.AtelierCore` is what the RPC handlers (`rpc/server.py`) call, each one line, and what the
manager MCP calls the same way. Every call may block, so an asyncio front door runs it with `asyncio.to_thread`.

| Group      | Call                                                                                          |
|------------|-----------------------------------------------------------------------------------------------|
| Projects   | `core.list_projects()`, `core.open_project(name or path) -> ProjectRuntime`, `core.project(name)` |
| Catalog    | `runtime.catalog`: `scan()`, `list_sources()`, `list_references()`, `set_status()`, bookmarks, `add_reference()`; `exports.convert_export(runtime.project, path)` |
| Review     | `runtime.revisions` (`RevisionTracker`): `history(source)` (revisions with standard render paths), `request_standard_renders()`; `runtime.revisions.revisions.convert_revision_export()`; `runtime.feedback.list_feedback(source)` (with pins and image paths); `runtime.workers.snapshot(source)` (state, the current session's chat items, past sessions) |
| References | `runtime.catalog.list_references()`; `runtime.workers.get(source).pin_references(paths)` |
| Workers    | `runtime.workers.send_prompt(source, prompt, feedback_id=None)`, `.get(source).interrupt()`, `.new_session()`, `.stop()`, `runtime.workers.list_infos()` |
| Revisions  | `runtime.revisions.restore(source, revision_id)` (refused with `SourceBusyError` while a turn runs) |
| Approvals  | `core.approvals.list_pending(project)`, `core.approvals.answer(...)` (the GUI's alone)          |
| Events     | `core.subscribe(listener) -> unsubscribe`: `listener(project name, event)` on any thread         |

A worker's whole transcript is also on disk, `<state>/transcripts/<session id>.jsonl`, readable with
`atelierd.workers.transcript.read_transcript`.
