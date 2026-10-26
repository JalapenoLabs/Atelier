# Manager MCP

`atelierd` serves an MCP server over streamable HTTP on 127.0.0.1 for a **manager**: a Claude Code session, in a
terminal or anywhere else on the machine, that oversees a project's art. It calls the same core as the GUI, so it sees
exactly what you see.

```
claude mcp add --transport http atelier http://127.0.0.1:47817/mcp
```

It mounts at `/mcp` on the daemon's HTTP front door (`atelierd.mcp.app`, default port 47817, `--http-port`), beside
the worker-side `atelier` server each worker gets at `/worker/mcp` (see `workers.md`), which only carries the
approval tool. Its tools call the core the GUI's RPC calls (see "The core every front door calls" in
`architecture.md`).

## Tools

| Group      | Tools                                                                                     |
|------------|-------------------------------------------------------------------------------------------|
| Catalog    | List and search sources; get a source with its exports, stats, status and revisions       |
| Review     | Get standard renders for any revision as images; compare two revisions; read a worker's recent transcript; list feedback with its pins |
| References | List and search references; pin references to the next prompt for a source              |
| Workers    | Start or continue a worker turn on a source with a prompt; interrupt it; get its state    |
| Revisions  | Restore a source to an earlier revision (see `revisions.md`)                              |

## Boundaries

- A manager directs workers but never answers their approval requests. Escalations always come to you.
- A manager cannot set a source's status to `approved`. Final sign-off is yours, in the GUI.
- Renders come back as image content, so a manager can judge the work the same way you do.
