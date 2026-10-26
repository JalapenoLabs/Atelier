# Workers

A **worker** is a Claude Code session working on one source, run by the daemon.

## Running Claude Code

Each active source has one long-lived `claude` process:

```
claude --print --verbose --input-format stream-json --output-format stream-json \
  --resume <session-id> \
  --mcp-config <worker-mcp.json> --strict-mcp-config \
  --permission-prompt-tool mcp__atelier__approve \
  --settings <worker-settings.json>
```

- **Credentials.** It is the real CLI using the host's own Claude Code login. Atelier never calls the API itself.
- **Working directory.** The project root, so the project's `CLAUDE.md`, docs and art direction apply to every turn.
- **Turns.** Prompts are written to its stdin as stream-json, and its event stream is rendered natively in the chat
  panel: text, tool calls, images. Interrupt stops the current turn.
- **Sessions.** A source can have many sessions over time. The composer continues the latest one; New session starts
  fresh. Session ids are recorded in `state.db`.
- **Tools.** `--strict-mcp-config` gives the worker exactly two MCP servers: its own Blender, and the daemon's
  worker-side `atelier` server, which holds the approval tool.
- **Instructions.** Every turn's prompt carries a short brief: the source, its files, any pinned references, any
  feedback, and whether it was just restored.

## Blender per worker

Every worker gets its own Blender, so parallel workers never trample one scene. The daemon starts
`blender --background --command blender_mcp --host 127.0.0.1 --port <n>` and sets `BLENDER_MCP_PORT=<n>` for that
worker's `blender` MCP server. Background mode serves every MCP tool except screenshots and `jump_to_*`, at a fraction
of a windowed instance's memory and GPU.

**Show in Blender** opens a windowed Blender on the worker's `.blend` in a silent Hyprland special workspace, so
nothing appears on screen until you go to it.

## Approvals

With several workers at once, prompting you for every tool call would be unusable. A **scope policy** decides instead:

| Allowed without asking                                                     | Escalated to you                         |
|---------------------------------------------------------------------------|------------------------------------------|
| Reads anywhere in the project                                              | Writes or deletes outside the scope      |
| Writes under the source's folder and its discovered outputs                | Network access                           |
| Its own Blender MCP; `blender -b` runs                                     | Anything under the Unreal `Content/` tree |

Your allow rules in Claude Code settings would let some calls skip the permission-prompt tool, so the scope check also
runs as a `PreToolUse` hook (passed with `--settings`), which sees every call. The prompt tool turns escalations into
an approval card in the GUI plus a desktop notification. Only you answer them; a manager cannot grant permissions.

Writes to `shared` paths (see `catalog.md`) escalate in v1. Parallel-safe shared-library edits are on the roadmap.

## Markup

Feedback is drawn, not just typed. Any render, or a frozen viewport frame (with its camera recorded), opens under a
drawing layer:

- **Strokes**: pen, arrow and box, in a few theme colours, with undo and clear.
- **Pins**: numbered markers, each with its own note.

Sending writes the clean capture and the flattened annotated PNG to `.atelier/feedback/<source>/<id>/` and records the
revision it critiques. The worker's prompt names the annotated image's path and lists the pins as `1: …`, `2: …`.
Feedback stays out of version control, because it is review state, not source.
