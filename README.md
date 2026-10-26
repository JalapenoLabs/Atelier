# Atelier

A native review studio for AI-built 3D art. Point it at a game project and it shows every asset in the project's
`SourceArt/` folder with its exports, renders, references and AI history. You review, mark up and direct Claude Code
workers from one window instead of a file browser, an image previewer and a terminal.

Atelier is filesystem-first: the project folder is the source of truth, and Atelier never asks you to move or rename
anything. Its own state lives in a `.atelier/` folder inside the project, which version control ignores.

## Parts

- **`atelierd`**: a per-user daemon that owns the catalog, revisions, Claude Code workers and their Blender instances.
- **`atelier`**: the PySide6 / Qt Quick desktop app, a client of the daemon over its JSON-RPC socket.
- **Manager MCP**: the daemon also serves MCP, so a managing Claude session can browse assets, look at renders and
  direct workers.

## Running

```
uv sync
uv run atelierd run                         # the daemon, in the foreground (Ctrl+C stops it and its workers)
uv run atelier --project /path/to/Project   # open the review window on a project (later just `uv run atelier`)
uv run atelier scan /path/to/Project        # list every source with its exports and renders
uv run atelier scan Project --watch         # by name once scanned, and keep the catalog and revisions live
uv run atelier prune Project --keep 20      # delete all but each source's newest 20 revisions
```

The window is a client of the daemon; started without one, it offers to start it in the background. To run the
daemon as a systemd user service instead, `uv run atelierd install` writes the unit, and
`systemctl --user enable --now atelierd.service` starts it now and at every login.

The daemon converts exports and renders with Blender, so `blender` must be on `PATH` (or named by `ATELIER_BLENDER`).
Workers also need `claude` (Claude Code, logged in) and `blender-mcp` with the Blender Lab MCP extension
(`ATELIER_CLAUDE`, `ATELIER_BLENDER_MCP` override them). To look at a project without writing anything into it, run
the daemon with `--state-root <folder outside it>` (for `scan`, `--state-dir`).

Checks, as CI runs them: `uv run ruff format --check`, `uv run ruff check`, `uv run mypy src tests`, `uv run pytest`.

## Documentation

| Doc                                      | Covers                                                        |
|------------------------------------------|---------------------------------------------------------------|
| [architecture.md](docs/architecture.md)  | Processes, protocols, where state lives, stack                |
| [catalog.md](docs/catalog.md)            | What a source is, discovery conventions, `atelier.toml`, references |
| [viewer.md](docs/viewer.md)              | The window, the 3D viewport, the render gallery               |
| [revisions.md](docs/revisions.md)        | Snapshots, standard renders, compare, restore                 |
| [workers.md](docs/workers.md)            | Claude Code workers, per-worker Blender, approvals, markup    |
| [mcp.md](docs/mcp.md)                    | The manager-facing MCP tools                                  |
| [roadmap.md](docs/roadmap.md)            | What comes after v1                                           |
