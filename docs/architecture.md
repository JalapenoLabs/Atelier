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
- **The GUI is a client.** It renders state and sends commands; it holds no state of its own beyond window layout.
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
| `<project>/.atelier/state.db`           | SQLite (WAL): sources, statuses, revisions, feedback, sessions |
| `<project>/.atelier/objects/`           | Content-addressed snapshot bytes (see `revisions.md`)        |
| `<project>/.atelier/renders/`           | Standard renders per revision                                |
| `<project>/.atelier/feedback/`          | Markup captures and annotated images                         |
| `<project>/.atelier/cache/`             | Converted `.glb` files, keyed by source content hash         |
| `~/.local/share/atelier/projects.toml`  | The machine's registry of known projects                     |

Per-project state travels with the project's drive and never mixes facts from two projects. `.atelier/` is added to
the project's `.dvignore` (or `.gitignore`): it is review state, not source, and committing it would turn every click
into a version-control change.

## Stack

- Python, pinned and managed with `uv` (`pyproject.toml` plus `uv.lock`).
- PySide6 with Qt Quick and Qt Quick 3D for the GUI. All rendering runs in Qt's native RHI (Vulkan on Linux), so Python
  costs nothing in viewport speed. Python matches the `bpy` builder scripts the projects already use.
- Blender, headless, for conversion and standard renders, plus one background instance per worker.
- The Claude Code CLI, run as a child process with the host's own login. Atelier never calls the Anthropic API itself.
