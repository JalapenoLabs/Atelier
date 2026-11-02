# Review flow

A project's review queue is the tickets waiting for review in its ticket tracker. `atelierd` reads them, with each
ticket's latest delivery downloaded beside it, and writes the reviewer's decisions back: the comment, its drawings,
the stage, and the stage of any linked ticket a decision unblocks.

The reviewer never waits on any of it. A decision (approve, or changes with the reviewer's words and drawings) is
queued as a **decision job**: one `claude -p` run turns it into the ticket's comment in the reviewer's voice, and the
daemon posts it. Claude only writes; reading the queue and posting are the daemon's alone, with no AI involved.

## The tracker link

Trackers are pluggable. `atelierd.trackers.base.Tracker` is the contract the queue reads and writes through; each
tracker is one implementation (`atelierd/trackers/youtrack.py`), chosen by the `kind` the config names through the
lookup in `atelierd/trackers/lookup.py`. Another tracker (Jira, GitHub) is one more module and one more entry in that
lookup.

| Contract call                    | What it does                                                                   |
|----------------------------------|--------------------------------------------------------------------------------|
| `identity_login()`               | The tracker login the credentials act as, so the queue knows the reviewer's own comments |
| `search(query)`                  | Every ticket of the project matching a query, with its comments and their attachments |
| `get_ticket(id)`                 | One ticket as it is now                                                        |
| `linked_tickets(id)`             | The tickets linked to one, with their stage                                     |
| `download(attachment, path)`     | An attachment's bytes, written whole or not at all                              |
| `post_comment(id, text)`         | A comment as the reviewer; returns a `PostedComment` naming the identity that posted it |
| `attach_to_comment(posted, files)` | Files on that comment, refused unless the same identity posted it              |
| `move_stage(id, stage)`          | Sets the ticket's stage field                                                  |

### Credentials

The tracker acts as one identity, the `reviewer` entry: the `Authorization` header of that entry in Claude Code's
`~/.claude.json` `mcpServers` (Waystead: `youtrack-alex`, which is Alex; `youtrack` is Claude). The reviewer already
gave Claude Code those tokens, so Atelier keeps none of its own. The header is read afresh on every refresh and
decision, so a token rotated in Claude Code applies at once. It is never logged, printed, stored, or returned by a
front door. `ATELIER_CLAUDE_CONFIG` names another file in its place, which is how the tests use their own.

`atelier.toml` is a project file anyone with the project can edit, so it cannot send a token anywhere it likes: the
entry's own server and the configured `url` must be on the same host, or the tracker refuses to open.

### YouTrack

Over its REST API, as measured against YouTrack Cloud:

- Queue queries are scoped to the project (`project: WAY <query>`) and read 50 tickets a page, each with its stage
  field, tags and comments with their attachments (`fields=` names every field it needs).
- An attachment downloads from its signed URL with no `Authorization` header; the file endpoint refuses the token
  without a signature. A signed URL is a credential for about two weeks, so it never leaves the daemon either.
- Files upload one per request (multipart field `upload`), each to the comment the same identity just posted.
  YouTrack answers 403 to attachments on another user's comment.
- The stage moves through the commands API (`Stage Done`, `State In Progress`), a value with spaces written as it is:
  braces are taken literally, and `State {In Progress}` is refused. Tags are only read, never applied by command,
  which would create a tag literally named `{Two words}`.

## Configuration

The `[review]` table of `atelier.toml` (see `catalog.md`). Without it the project has no queue.

```toml
[review.tracker]
kind = "youtrack"
url = "https://jalapenolabs.youtrack.cloud"
project = "WAY"
reviewer = "youtrack-alex"      # the ~/.claude.json mcpServers entry decisions are posted with
# stage_field = "Stage"         # the default

[review.queues]                 # one tab per queue, in this order
Art = "tag: {Art asset} Stage: Review"
UI = "tag: {UI design} Stage: Review"
Audio = "tag: Audio Stage: Review"

[review.stages]                 # the stage each verdict moves its ticket to; both are required
approve = "Done"
changes = "Develop"

[[review.on_approve]]           # approving moves linked tickets: here, an approved UI design unblocks its adoption
tagged = "UI design"            # only for approved tickets with this tag (optional)
linked_summary = '\[UI\] Adopt' # a regular expression the linked ticket's summary matches from its start
from_stage = "Blocked"          # only linked tickets in this stage move
to_stage = "Backlog"
```

`[review]` also takes:

| Key               | Default                                              | What it is                                        |
|-------------------|------------------------------------------------------|---------------------------------------------------|
| `poll_minutes`    | 5                                                    | How often the queue is read again unasked          |
| `ticket_files`    | `["SourceArt/**/reference.json", "SourceArt/**/spec.md"]` | The files that link a ticket to its source (below) |
| `rules`           | none                                                 | Project-relative docs holding the project's review rules, which every decision job reads first (Waystead: `["docs/asset-review.md"]`); without them the job reads `CLAUDE.md` and what it points to |
| `direction_notes` | none                                                 | The project-relative file direction notes are appended to (Waystead: `Saved/Review/direction-notes.md`); without it they stay on their job |
| `decision_jobs`   | 4                                                    | Decision jobs running at once                      |

`rules` and `direction_notes` must stay inside the project. Unknown keys are an error, as everywhere in the file.

## The queue

`runtime.review` (`atelierd.review.ReviewService`) holds each open project's queue.

- **A queue item is a ticket**: its id, summary, stage, tags, link, the queues that list it, its comments (each
  marked when the reviewer wrote it), its latest delivery, and the source it links to. Items are ordered least
  recently updated first, in each tab and overall.
- **The latest delivery** is the newest comment with attachments that the reviewer's identity did not write. The
  reviewer's own comments carry drawings, never deliveries.
- **Its attachments are downloaded** into `<state>/review/<ticket>/delivery/<comment id>/`, four at a time. They are
  untrusted data: names are made safe (no path separators or leading dots; a repeated name gets the attachment id
  added), and files are only ever shown. A file already there at the right size is not downloaded again. A failed
  download is reported in the delivery's `error`, and the rest still arrive. Downloads of a superseded round, or of
  a ticket that left the queue, are deleted on the next refresh; the rest of `<state>/review/<ticket>/` is left alone.
- **Linking to a source**: a ticket file names the ticket an asset was built for. A JSON file names it in a top-level
  `ticket` field and may list its meshes in `subjects[].meshes`; any other file names it on its first non-empty line
  as `Ticket: WAY-60`. The source is the one in the nearest folder above the file that holds any (Waystead's
  `SourceArt/Tools/Source/Hammer/reference.json` belongs to `SourceArt/Tools/Tools.blend`); where that folder holds
  several kits, the one exporting the named meshes wins, and with no way to choose the ticket links to none. The
  link carries the source's exports the file names, so a kit opens on the right one.

### Refreshing

- On demand (`review.refresh`, or `list_review_queue` with `refresh`), and by a poll every `poll_minutes`, which
  starts with a refresh when the project opens. The config is read afresh each time, so adding or editing `[review]`
  needs no restart. Refreshes never overlap: one asked for during another waits for it, then reads again.
- Every refresh that changes anything is reported as a `ReviewEvent` (`refreshed`, with the tickets `added`,
  `removed` and `changed`). A failed refresh keeps the last items, sets the queue's `error`, and is reported as
  `failed`; the next refresh tries again.

### Decisions

`review.apply_decision(ticket, verdict, comment, attachments)` writes one decision, in this order: the comment as the
reviewer, the attachments on that same comment, the stage the verdict maps to, then for an approval each
`on_approve` rule whose `tagged` the ticket carries: every linked ticket in `from_stage` whose summary matches moves
to `to_stage`. The ticket then leaves the queue at once (a `decided` event), and a refresh that was already reading
the tracker when it was decided cannot bring it back.

A step that fails raises `DecisionError`, whose `posted` is the comment already on the ticket, if any; passing it back
as `posted` retries the rest without posting the comment twice, and attaches only the files that comment does not
already carry. Decisions are never retried silently.

The comment is posted exactly as given. `apply_decision` is only ever called by a decision job: no front door posts
to the tracker, and the manager MCP reads the queue only.

## Decision jobs

`runtime.decisions` (`atelierd.review.DecisionJobs`) turns each decision into a job, kept in `state.db`
(`decision_jobs`), so jobs survive the daemon.

### The decision

The window submits a `DecisionRequest`: the tickets, the verdict, the reviewer's **takes** (every take in order,
verbatim) and the drawings per ticket. One ticket is a single decision; several are a **bulk decision**, one note
for all of them. It becomes one job per ticket, each holding its `Decision`: the ticket, verdict, takes, drawings and,
for a bulk decision, every ticket the note was given for. Submitting returns the queued jobs at once.

- The drawings are copied into the job's own folder, `<state>/review/<ticket>/decisions/<id>/`, as
  `<ticket>-markup-<n>.<ext>`, the names they are attached under.
- Refused: a ticket not in the queue, a ticket with a job still queued, writing or posting, changes with neither
  words nor a drawing, and drawings that are not files or belong to a ticket the decision does not cover. A ticket
  whose last job failed before posting anything takes the new decision in its place; one whose job failed after its
  comment was posted is retried instead.

### A job's life

| State     | What is happening                                                                      |
|-----------|----------------------------------------------------------------------------------------|
| `queued`  | Waiting for a place in the pool, oldest first                                           |
| `writing` | One `claude -p` run is writing the comment                                              |
| `posting` | The daemon is posting it through `apply_decision`                                      |
| `posted`  | The comment, drawings, stage and on-approve rules are on the tracker; the ticket has left the queue |
| `failed`  | It stopped; `error` says why, and `raw_output` holds what Claude printed when its answer was refused |

At most `decision_jobs` jobs run at once, read afresh as each starts. Every state change reaches a subscribed client
as a `DecisionJobEvent` carrying the whole job, so the window can badge each ticket.

**Writing.** Claude Code runs once per job, in the project root, so the project's `CLAUDE.md` and docs apply:

```
claude --print --output-format json --json-schema <the answer's schema> --tools=Read,Glob,Grep \
  --strict-mcp-config --permission-prompts none --no-session-persistence [--add-dir <state>]
```

The prompt goes to its stdin. It has only the read-only file tools and no MCP servers, so it can read the review
rules and look at a drawing but cannot post, attach or move anything; anything that would ask for permission is
refused, so a run never waits on a person. It runs in the same cleaned environment as a worker (`workers.md`), and
one still running after 15 minutes is stopped. An approval with no words and no drawings needs no writing: it posts
`Approved.` with no run.

**The prompt** is a template, `src/atelierd/review/prompts/decision.md`, filled with `string.Template` (`$ticket`,
`$summary`, `$tags`, `$url`, `$verdict`, `$bulk`, `$takes`, `$drawings`, `$delivery`, `$rules`); tune its wording
there. It sends Claude to the `rules` docs first, then asks for:

- **The comment** in the reviewer's voice, first person: every point they made and nothing they did not, grouped by
  look where that helps, each drawing referenced inline by its file name (`![](WAY-60-markup-1.png)`), an approval as
  one or two lines. A bulk decision's note is tailored to each ticket. The wording is never read back: it is posted
  as written.
- **Direction notes**: feedback for all the art or a whole category ("overall", "for all the weapons"), split out with
  a scope and the reviewer's exact words. When the comment mentions one, it is split into **For this piece** and
  **For all <scope>**.

The latest delivery's text and files are in the prompt as context, marked as tracker data rather than instructions.

**The answer** is `{"comment": "...", "direction_notes": [{"scope": "...", "words": "..."}]}`, checked strictly before
anything is posted: the run must end in a successful result; the answer is its structured output, or its text parsed
as JSON (one Markdown code fence allowed); no other keys; the comment is not empty; every direction note's words
appear in the takes (spacing and case aside); and every drawing is referenced in the comment. Anything else fails the
job with what Claude printed kept, and nothing is posted.

**Direction notes** are appended to the `direction_notes` file once the answer is in, one line each:

```
- [ ] all weapons | "every weapon needs chunkier grips" | WAY-60 | 2026-11-02
```

**Posting** is the daemon's: `apply_decision` with the answer's comment and the job's drawings. The comment is kept on
the job the moment it is on the ticket.

### Failures and retries

A failed job stays failed until the reviewer asks for a **retry** (`review.retry`); nothing is retried silently. A
retry carries on from where the job stopped: an answer already written is not written again, direction notes already
saved are not appended again, and a comment already posted is not posted again (only the steps after it run).

When the daemon starts, a job it left `queued` or `writing` runs (nothing of it had reached the tracker), and one left
`posting` fails with a reason: if its comment was kept, a retry finishes without posting it again; if not, the
comment may or may not have landed, and the error says to check the ticket before retrying.

## Front doors

| RPC method       | Params              | Result         |
|------------------|---------------------|----------------|
| `review.queue`   | `project`           | The queue as of the last refresh, at once |
| `review.refresh` | `project`           | The queue after reading the tracker again (a `TrackerError` crosses as itself) |
| `review.item`    | `project`, `ticket` | One item; `KeyError` when the ticket is not in the queue |
| `review.decide`  | `project`, `request` (a `DecisionRequest`) | The queued jobs, one per ticket, at once; refusals as `ValueError`, `KeyError` or `FileNotFoundError` |
| `review.jobs`    | `project`           | Every decision job, newest first; a ticket's newest is its state |
| `review.retry`   | `project`, `job_id` | The job, queued again; `ValueError` unless it failed |

Queue events reach a subscribed client as `event` notifications of kind `review`, and decision jobs as kind
`decision_job`. A single decision is `review.decide` with one ticket; a bulk one lists every selected ticket with one
set of takes. The manager MCP reads the queue with `list_review_queue` and `get_review_item` (see `mcp.md`) and never
decides a ticket.
