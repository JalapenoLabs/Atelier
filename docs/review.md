# Review flow

A project's review queue is the tickets waiting for review in its ticket tracker. `atelierd` reads them, with each
ticket's latest delivery downloaded beside it, and writes the reviewer's decisions back: the comment, its drawings,
the stage, and the stage of any linked ticket a decision unblocks. No AI is involved in reading the queue or posting
a decision.

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
- The stage moves through the commands API (`Stage Done`; a value with spaces in braces). Tags are only read, never
  applied by command, which would create a tag literally named `{Two words}`.

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

`[review]` also takes `poll_minutes` (default 5) and `ticket_files` (default
`["SourceArt/**/reference.json", "SourceArt/**/spec.md"]`). Unknown keys are an error, as everywhere in the file.

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
as `posted` retries the rest without posting the comment twice. Decisions are never retried silently.

The comment is posted exactly as given. No front door posts a decision: the window and the manager MCP read the
queue only.

## Front doors

| RPC method       | Params              | Result         |
|------------------|---------------------|----------------|
| `review.queue`   | `project`           | The queue as of the last refresh, at once |
| `review.refresh` | `project`           | The queue after reading the tracker again (a `TrackerError` crosses as itself) |
| `review.item`    | `project`, `ticket` | One item; `KeyError` when the ticket is not in the queue |

Review events reach a subscribed client as `event` notifications of kind `review`. The manager MCP reads the queue
with `list_review_queue` and `get_review_item` (see `mcp.md`).
