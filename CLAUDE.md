# Atelier

A native review studio for AI-built 3D art: `atelierd` (daemon), `atelier` (PySide6 / Qt Quick GUI) and a manager
MCP. Start with `README.md`, then the doc for the area you are touching in `docs/`.

- The docs in `docs/` are the design of record. Keep them current with every change; if code and docs disagree, report
  it.
- The project folder Atelier points at is the source of truth. Atelier never moves, renames or rewrites a project's
  files except through a worker, a restore, or a reference dropped into the rail.
- All of Atelier's own per-project state lives under `<project>/.atelier/` and is never committed to the project.
- v1 scope is the "Atelier v1" milestone. Anything in `docs/roadmap.md` is out of scope until it is filed as an issue.
