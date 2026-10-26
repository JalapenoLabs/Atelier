# Roadmap

v1 is the "Atelier v1" milestone: catalog, viewer, revisions, workers and the manager MCP. These follow it, in rough
order.

## Jobs and the board

- A job: one brief against one source (or a new source to create), with pinned references, acceptance notes and an
  optional parent job.
- States: queued → running → in review → changes requested ↺ → approved. A manager drives everything up to approved;
  only you approve.
- A Board view (sources by status) and a Queue view (jobs by state).

## Built-in manager

A persistent manager session per project, run by the daemon with the manager MCP and shown in its own panel. It runs
and requeues work within the queue you seeded or approved, and proposes new jobs as drafts you accept with one click.

## Parallel safety

- A source lock: at most one running job per source.
- A shared lease: one worker at a time may write `shared` paths, enforced by the scope policy.
- Stale tracking: when a shared file changes, every source whose builder imports it is marked stale and offered for
  rebuild. Dependencies come from a static scan of builder imports and `--scripts` arguments.
- A concurrency cap on running workers, set in `atelier.toml`.

## Viewer

- Skeleton and socket display: bones, named sockets from the FBX and `*_sockets.json`.

## Sessions

- Auto-link sessions started outside Atelier, such as in a terminal, by scanning `~/.claude/projects/<slug>/*.jsonl` for
  tool calls that wrote a source's files. Each source's chat then shows its whole history wherever it happened.
- A project-level chat for work that spans sources.

## Unreal

- Import approved exports into `Content/` through the Unreal MCP.
