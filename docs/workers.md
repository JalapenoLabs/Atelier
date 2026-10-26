# Workers

A **worker** is a Claude Code session working on one source, run by the daemon (`atelierd.workers`). Each source has
at most one; several sources' workers run at once, each with its own Blender.

## Running Claude Code

A worker runs one long-lived `claude` process while it is active:

```
claude --print --verbose --input-format stream-json --output-format stream-json \
  --mcp-config <state>/workers/<source>/mcp.json --strict-mcp-config \
  --permission-prompt-tool mcp__atelier__approve \
  --settings <state>/workers/<source>/settings.json \
  --session-id <new id> | --resume <id>
```

- **Credentials.** It is the real CLI (`claude` on `PATH`, or `ATELIER_CLAUDE`) using the host's own Claude Code
  login and settings. Atelier never calls the API itself.
- **Environment.** The daemon's own, minus its virtual environment (`VIRTUAL_ENV`, its `bin/` on `PATH`) and the
  variables of any Claude Code session that started the daemon, so a worker's `blender -b` never finds the daemon's
  Python and the worker is a session of its own. When the state folder is outside the project, it is added with
  `--add-dir`, so feedback images are readable.
- **Working directory.** The project root, so the project's `CLAUDE.md`, docs and art direction apply to every turn.
- **Turns.** A prompt is written to stdin as a stream-json user message, and the process stays up between turns.
  While a turn runs, another prompt is refused: interrupt it first. **Interrupt** sends Claude Code's
  `control_request` interrupt; the turn ends as interrupted. **Stop** ends Claude Code and Blender; the next prompt
  resumes the session. If Claude Code dies mid-turn, the turn ends there, the worker shows as failed, and the next
  prompt starts it again.
- **Sessions.** A source can have many sessions over time, recorded in `state.db` (`worker_sessions`). The composer
  continues the newest; **New session** ends the process and the next prompt opens a fresh one. A new session is
  opened with `--session-id <uuid>`, and resumed with `--resume` once Claude Code has stored it.
- **Tools.** `--strict-mcp-config` gives the worker exactly two MCP servers: its own Blender (`blender`), and the
  daemon's worker-side `atelier` server, which holds the approval tool.
- **The brief.** Every turn's prompt is the reviewer's words after a short brief built from the catalog as it stands:
  the source and its builder, README, exports and renders; where it may write freely; that its Blender is the
  `blender` MCP server with the `.blend` open; any references pinned to this turn; any feedback (the annotated
  image's path and the pins); and a restore since its last turn. Pinned references and the restore mark are cleared
  once a turn has carried them.
- **A revision after every turn.** When a turn ends (its `result`, an interrupt, a stop or a crash), Atelier records a
  `worker-turn` revision of the source (see `revisions.md`) before it marks the worker idle. Changes the file watcher
  sees while a turn runs are left to that revision, so a turn is one revision, and a restore is refused until the
  turn has ended.

## The chat

Claude Code's stream is turned into chat items (`ChatItem`) and kept, per session, as JSON lines in
`<state>/transcripts/<session id>.jsonl`, so the chat survives the daemon and needs no Claude Code process to read.
A later line with an item's index replaces it, so the file is only ever appended to. Images a tool returns (a
render from the worker's Blender, an image it read) are written beside it in `<state>/transcripts/<session id>/`.

| Item        | What the Chat tab draws                                                        |
|-------------|---------------------------------------------------------------------------------|
| `user`      | The prompt, its brief (folded), and the annotated image of feedback it carries  |
| `assistant` | The worker's text, as Markdown                                                  |
| `tool`      | The tool and a one-line summary (command, file, pattern), its result when it arrives (folded), any images |
| `approval`  | An approval card (see below), with how it was answered                          |
| `result`    | How the turn ended, how long it took, what it cost, and the revision it recorded |
| `notice`    | A new session                                                                    |

The tab's header shows the worker's state, its session and its Blender's port, with **Blender** (Show in Blender),
**New session** and **Stop**. The composer sends on Enter (Shift+Enter for a new line) and becomes **Interrupt**
while a turn runs. The chat keeps its newest item at the bottom while it streams, unless you scroll up.

## Blender per worker

Every worker gets its own Blender, so parallel workers never trample one scene. When a worker starts, the daemon
picks a free port and runs

```
nice -n 10 blender --background <source .blend> --online-mode --threads 4 --command blender_mcp --host 127.0.0.1 --port <n>
```

in the same cleaned environment as every Blender Atelier starts (see `viewer.md`), and waits until it listens. It is
not run with factory settings, because the MCP server is the Blender Lab MCP extension, a user extension; online mode
lets it open its port. The worker's `blender` MCP server runs `blender-mcp` (on `PATH`, or `ATELIER_BLENDER_MCP`)
with `BLENDER_MCP_HOST=127.0.0.1` and `BLENDER_MCP_PORT=<n>`. Background mode serves every MCP tool except
screenshots and `jump_to_*`. The Blender keeps running between turns and across sessions, until the worker stops.

**Show in Blender** opens a windowed Blender on the source's `.blend`. The window does this (it runs in the desktop
session, which a systemd service may not see): under Hyprland in the silent special workspace `special:atelier`, so
nothing appears on screen until you go to it, and elsewhere as an ordinary window.

## Approvals

With several workers at once, prompting you for every tool call would be unusable. A **scope policy**
(`workers/policy.py`) decides instead:

| Runs without asking                                                        | Escalated to you                          |
|----------------------------------------------------------------------------|-------------------------------------------|
| Reads anywhere in the project, in its state folder, and in Claude Code's own `~/.claude` | Reads elsewhere on the machine |
| Writes under the source's folder and to its discovered files               | Writes or deletes outside that scope, and to Atelier's state |
| Its own Blender MCP and Atelier's worker tools; `blender -b` runs          | Writes to `shared` paths, and to a builder outside the source's folder |
| Shell pipelines of reads, and `mkdir`/`cp`/`mv`/`rm`/`touch` or `>` inside the scope | Anything under the Unreal `Content/` tree |
| Claude Code's own tools that touch no files (todos, subagents, tool search) | Network access (`WebFetch`, `WebSearch`, `curl`, `git`, ...), other MCP servers, and any command or tool the policy cannot account for |

`~/.claude` is readable because the user's own instructions send every agent to read its docs and learnings. Shell
commands are parsed (`bashlex`), not matched as text: every simple command, command substitution and redirect in
them is checked, and loops, conditionals, a `cd` before a write, or a path computed from a variable escalate.

The policy runs in two places that share one `evaluate`, so they never disagree. Your allow rules in Claude Code
settings would let some calls skip the permission-prompt tool, so a `PreToolUse` hook (passed with `--settings`,
`python -m atelierd.workers.hook <policy.json>`) sees every call: it answers `allow` inside the scope and `ask`
outside it, and `ask` whenever it cannot check a call. Claude Code sends every `ask` to the permission-prompt tool,
`mcp__atelier__approve` on the worker-side MCP, which applies the policy again and turns an escalation into an
**approval card**: inline in the source's chat, in the header's approvals badge (every project's, oldest first), and
as a desktop notification (`notify-send`). The call waits until you **Allow** or **Deny** it, with an optional note
the worker hears with a denial. Interrupting or stopping the worker cancels its pending cards.

Only you answer approvals, in the window; a manager cannot grant permissions. Writes to `shared` paths escalate in v1.
Parallel-safe shared-library edits are on the roadmap.

The worker-side MCP lives on the daemon's HTTP front door (`http://127.0.0.1:<port>/worker/mcp`). Each worker sends
its own secret token in an `X-Atelier-Worker` header, and requests without a live worker's token are refused. It
answers over SSE, and each worker's config gives it a 24 hour call timeout (`timeout` in `mcp.json`): Claude Code
abandons a permission prompt whose answer has not started arriving after about a minute, and an approval waits as
long as you take.

## Markup

Feedback is drawn, not just typed. **Mark up** on a render shown full size, or on the 3D viewport's toolbar (which
freezes the frame and records its camera), opens the stage's markup editor:

- **Strokes**: pen, arrow and box, in four colours, with undo and clear.
- **Pins**: numbered markers, each with its own note.
- **Send to worker**, with an optional note of your own.

Marks are kept in coordinates from 0 to 1 across the image, so the editor's canvas and the flattened PNG agree at any
size. Sending flattens the strokes and pins onto the capture at its full resolution (in the window, with Qt), and
the daemon stores the feedback in `<state>/feedback/<source>/<id>/` (`<source>` is the source's path without
`.blend`): `capture.png`, `annotated.png` and `markup.json`. `state.db` (`feedback`) records the revision and export
it critiques, its pins and a frozen frame's camera. The worker's prompt names the annotated image's path, lists the
pins as `1: …`, `2: …`, and asks it to read the image first; the window switches to the chat, where the turn streams.
Feedback stays out of version control, because it is review state, not source.

## Pinned references

With a source selected, each image in the references rail has a **Pin** toggle. Pinned references are stored per
source (`state.db` `pinned_references`), named in the next turn's brief, and cleared once that turn has carried them.

## State

| Where                                         | What                                                       |
|-----------------------------------------------|------------------------------------------------------------|
| `state.db` `worker_sessions`                  | Every session per source; the newest is continued           |
| `state.db` `feedback`, `pinned_references`    | Feedback rows with their pins; references pinned to the next prompt |
| `<state>/transcripts/<session id>.jsonl`      | Each session's chat, and its tool images in a folder beside it |
| `<state>/feedback/<source>/<id>/`             | Each feedback's capture, annotated image and markup         |
| `<state>/workers/<source>/`                   | The worker's `policy.json`, `mcp.json` and `settings.json` (mode 0600: `mcp.json` holds its token) |
