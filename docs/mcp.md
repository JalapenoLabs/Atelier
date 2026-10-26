# Manager MCP

`atelierd` serves an MCP server over streamable HTTP on 127.0.0.1 for a **manager**: a Claude Code session, in a
terminal or anywhere else on the machine, that oversees a project's art. It calls the same core as the GUI, so it sees
exactly what you see.

```
claude mcp add --transport http atelier http://127.0.0.1:47817/mcp
```

It lives at `/mcp` on the daemon's HTTP front door (`atelierd.mcp.app`, default port 47817, `--http-port`), beside
the worker-side `atelier` server each worker gets at `/worker/mcp` (see `workers.md`), which only carries the
approval tool. Its tools (`atelierd.mcp.manager`) call the core the GUI's RPC calls (see "The core every front door
calls" in `architecture.md`), each in a thread off the daemon's event loop.

## Transport

- Stateless streamable HTTP, answering over SSE, so a tool that waits (on a turn, on renders) starts its response at
  once and is kept alive; Claude Code abandons a response with no first byte after about a minute.
- No token: the port is bound to 127.0.0.1, and the MCP SDK's DNS rebinding protection refuses any `Host` or `Origin`
  that is not loopback, so a web page cannot reach it. What a manager can do is bounded by the tools, not by who
  calls (see Boundaries).
- The server's instructions describe the review loop, and Claude Code puts them in the manager's system prompt.

## Arguments every tool shares

- `project`: a project by name (from the registry) or by folder path, opened on first use like the window opens it.
  It may be left out while the daemon has exactly one project open, or, with none open, while the machine knows
  exactly one.
- `source`: a source's `.blend` path relative to the project, or a part of its name that matches only it (`Carbine`,
  `dogtag`). A name matching several sources is refused with the candidates.
- Revisions are named by number, the N of rN, as the filmstrip shows them, never by database id.

## Tools

| Group      | Tool                | What it does                                                                     |
|------------|---------------------|----------------------------------------------------------------------------------|
| Catalog    | `list_projects`     | The projects this machine knows, and which the daemon has open                   |
|            | `list_sources`      | Sources with status, export count and worker state; `query` (every word must appear in the path) and `status` filter |
|            | `get_source`        | One source: its files, each export's stats (triangles, materials, meshes, size), its revisions with each export's render state, a restore mark, its worker and how much feedback it has |
| Review     | `get_renders`       | The standard renders of one export of a revision (default the newest, and its first export) as images, for the `views` asked for (default `hero`) |
|            | `compare_revisions` | Two revisions (default the newest, B, against the one before it, A): one image per view with A on the left and B on the right, each half labelled |
|            | `get_transcript`    | The newest `last` items (default 20) of the worker's chat in its current session, one line each |
|            | `list_feedback`     | The reviewer's marked-up feedback on a source, newest first, with its pins, the revision and export it critiques, a frozen frame's camera, and with `include_images` its annotated image |
| References | `list_references`   | The project's reference images with their tags; `query` filters by path and tag |
|            | `pin_references`    | Pin references to a source's next prompt, replacing those pinned; paths that are not references are refused |
| Workers    | `send_prompt`       | Start a turn on a source with a prompt, optionally with a `feedback_id`, continuing its session or, with `new_session`, starting a fresh one. Returns while the turn runs |
|            | `get_worker`        | A source's worker: state, session, Blender port, pinned references, the approvals it waits on, and its last turn's result and revision. `wait_seconds` waits for a running turn to end |
|            | `list_workers`      | Every worker of the project that has run since the daemon started, and every pending approval in it |
|            | `interrupt_worker`  | Interrupt the running turn; it ends as interrupted and records a revision      |
| Revisions  | `restore_revision`  | Restore a source to a revision (see `revisions.md`); returns the revisions it recorded and the files written, deleted and skipped |

Failures the core raises on purpose (an unknown source, a turn already running, a restore while one runs) come back as
tool errors with the core's message, so a manager can act on them.

### What a manager waits for

- `get_worker` with `wait_seconds` (at most 1800) returns when the turn ends, or as soon as the turn is waiting on an
  approval, since only you can move it on. Its `note` says which.
- `get_renders` and `compare_revisions` ask for any render set not made yet (a baseline's are made only when asked
  for) and wait up to `wait_seconds` (default 120) while sets are pending. A set still pending, failed or impossible
  (no Blender) comes back as text saying so.

### Keeping a manager's context small

- Every image is a JPEG scaled to `size` pixels on its longer edge (default 512, 128 to 1024); renders are 1024. A
  compare is one image per view, `size` high and twice as wide.
- `views` picks which of `hero`, `front`, `side`, `top` and `clay` come back; one by default.
- Structured results are compact JSON, with fields that have no value (null) left out. Approvals carry no ids, and transcripts leave out
  the brief Atelier sends with each prompt and cut long text short (400 characters for text, 160 for a tool's
  summary).

## Boundaries

Enforced by what `manager.py` calls, and tested in `tests/test_manager_mcp.py`:

- A manager directs workers but never answers their approval requests. It only lists pending approvals, and what it
  receives has no approval id. Escalations always come to you, in the window.
- A manager cannot set a source's status, `approved` or any other: there is no status tool. Final sign-off is yours,
  in the GUI.
- Renders come back as image content, so a manager can judge the work the same way you do, without disk access.
