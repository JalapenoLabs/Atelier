# Atelier

A native review studio for AI-built 3D art. Point it at a game project and it shows every asset in the project's
`SourceArt/` folder with its exports, renders, references and AI history. You review, mark up and direct Claude Code
workers from one window instead of a file browser, an image previewer and a terminal.

Atelier is filesystem-first: the project folder is the source of truth, and Atelier never asks you to move or rename
anything. Its own state lives in a `.atelier/` folder inside the project, which version control ignores.

## Parts

- **`atelierd`**: a per-user daemon that owns the catalog, revisions, Claude Code workers and their Blender instances.
- **`atelier`**: the PySide6 / Qt Quick desktop app, a client of the daemon.
- **Manager MCP**: the daemon also serves MCP, so a managing Claude session can browse assets, look at renders and
  direct workers.

## Running

```
uv sync
uv run atelier scan /path/to/Project        # list every source with its exports and renders
uv run atelier scan Project --watch         # by name once scanned, and keep the catalog live
```

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
