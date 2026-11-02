"""Turning a decision into its ticket comment with one `claude -p` run: the prompt, the command, and the answer.

The prompt is a template, `prompts/decision.md`, so its wording can be tuned without touching code. It sends Claude to
the project's review rules first (`[review] rules`), then gives it the decision: the ticket, the verdict, the
reviewer's takes verbatim, the drawings by the file names they are attached under, and the latest delivery as
context.

Claude runs in the project root, so the project's `CLAUDE.md` and docs apply, with only the read-only file tools and
no MCP servers at all: it can read the rules and look at a drawing, and nothing else. It cannot post, attach or move
anything, because the daemon does that with the answer (`jobs.py`). Anything that would need a permission prompt is
refused, so a run never waits on a person.

The answer is checked strictly before anything is posted: the JSON envelope Claude Code prints must be a successful
result, its answer must match `WrittenDecision` exactly, every direction note must quote the reviewer's own words, and
every drawing must be referenced in the comment. Anything else raises `AnswerError`, and the job fails with what
Claude printed kept beside it.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from string import Template

from pydantic import ValidationError

from atelierd.config import ReviewConfig, ReviewVerdict
from atelierd.project import Project
from atelierd.review.models import Decision, ReviewItem, WrittenDecision

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "decision.md"
# Claude Code's own read-only file tools: enough to read the review rules and see a drawing or delivery image.
_READ_ONLY_TOOLS = "Read,Glob,Grep"
# An answer given as text rather than as structured output may arrive wrapped in one Markdown code fence.
_FENCED_ANSWER_PATTERN = re.compile(r"^```(?:json)?\s*\n(?P<answer>.*)\n```$", re.DOTALL)
_WHITESPACE_PATTERN = re.compile(r"\s+")


class AnswerError(Exception):
    """Claude's answer cannot be posted: its run failed, or its JSON is not the answer asked for."""


def build_prompt(decision: Decision, item: ReviewItem, review_config: ReviewConfig) -> str:
    """Fill the decision prompt template for one ticket."""
    if review_config.rules:
        rules = "\n".join(f"- `{rules_doc}`" for rules_doc in review_config.rules)
    else:
        rules = "The project's `CLAUDE.md`, and whatever doc it names for reviewing art or tickets."

    stage = review_config.stages[decision.verdict]
    verdict_by_kind = {
        ReviewVerdict.APPROVE: f"approved. Atelier moves the ticket to {stage}.",
        ReviewVerdict.CHANGES: f"changes requested. Atelier moves the ticket back to {stage}.",
    }

    if decision.is_bulk:
        other_tickets = ", ".join(ticket for ticket in decision.bulk_tickets if ticket != decision.ticket)
        bulk = (
            f"This is a bulk decision: the reviewer gave the words below once, for {len(decision.bulk_tickets)} "
            f"tickets at a time (this one and {other_tickets}). Tailor them to this ticket: keep every point that "
            "applies to it, in the reviewer's words, and leave out what plainly concerns only the others."
        )
    else:
        bulk = "The reviewer decided this ticket on its own."

    if decision.takes:
        takes = "\n\n".join(
            f"Take {number}:\n" + "\n".join(f"> {line}" for line in take.splitlines())
            for number, take in enumerate(decision.takes, start=1)
        )
    else:
        takes = "(none: the reviewer said nothing beyond the verdict)"

    if decision.drawings:
        drawings = "The reviewer drew these; each is attached to the comment under its file name:\n\n" + "\n".join(
            f"- `{drawing.name}` (to look at it: {drawing})" for drawing in decision.drawings
        )
    else:
        drawings = "(none)"

    if item.delivery:
        attachment_lines = "\n".join(f"- {attachment.path}" for attachment in item.delivery.attachments)
        delivery = (
            f"Posted by {item.delivery.author} at {item.delivery.created_at}:\n\n"
            f"<delivery>\n{item.delivery.text}\n</delivery>\n\nIts files:\n\n{attachment_lines or '(none)'}"
        )
    else:
        delivery = "(none found)"

    template = Template(_PROMPT_TEMPLATE_PATH.read_text(encoding="utf-8"))
    return template.substitute(
        rules=rules,
        ticket=decision.ticket,
        summary=item.summary,
        tags=", ".join(item.tags) or "(none)",
        url=item.url,
        verdict=verdict_by_kind[decision.verdict],
        bulk=bulk,
        takes=takes,
        drawings=drawings,
        delivery=delivery,
    )


def claude_command_line(claude_command: str, project: Project) -> list[str]:
    """Return the `claude -p` command line of a decision job; the prompt goes to its stdin."""
    command = [
        claude_command,
        "--print",
        "--output-format",
        "json",
        "--json-schema",
        json.dumps(WrittenDecision.model_json_schema()),
        f"--tools={_READ_ONLY_TOOLS}",
        # No MCP servers at all, so none of the project's or the user's (a tracker's among them) can be called.
        "--strict-mcp-config",
        # Anything that would ask for permission is refused: a job never waits on a person.
        "--permission-prompts",
        "none",
        "--no-session-persistence",
    ]
    if not project.is_state_in_project:
        # Drawings and delivery files live in the state folder, which Claude may only read when it is added.
        command.extend(["--add-dir", str(project.state_dir)])
    return command


def parse_answer(output: str, decision: Decision) -> WrittenDecision:
    """Read the answer out of the JSON Claude Code printed, refusing anything but exactly the answer asked for.

    Raises:
        AnswerError: the run failed, or the answer is malformed, misquotes the reviewer, or leaves out a drawing.
    """
    try:
        envelope = json.loads(output)
    except json.JSONDecodeError as error:
        raise AnswerError(f"Claude Code did not print JSON: {error}") from error
    if not isinstance(envelope, dict) or envelope.get("type") != "result":
        raise AnswerError("Claude Code printed JSON that is not a result")
    if envelope.get("is_error") or envelope.get("subtype") != "success":
        raise AnswerError(f"Claude's run failed ({envelope.get('subtype')}): {envelope.get('result', '')}")

    # Structured output is the answer when Claude Code validated it against the schema; otherwise it is the text.
    answer = envelope.get("structured_output")
    if answer is None:
        answer_text = str(envelope.get("result", "")).strip()
        fenced = _FENCED_ANSWER_PATTERN.match(answer_text)
        if fenced:
            answer_text = fenced.group("answer")
        try:
            answer = json.loads(answer_text)
        except json.JSONDecodeError as error:
            raise AnswerError(f"Claude's answer is not JSON: {error}") from error
    try:
        written = WrittenDecision.model_validate(answer)
    except ValidationError as error:
        raise AnswerError(f"Claude's answer is not the answer asked for: {error}") from error

    if not written.comment.strip():
        raise AnswerError("Claude's comment is empty")

    # Exact words, give or take spacing and case: a direction note is the reviewer's, never Claude's paraphrase.
    spoken = _WHITESPACE_PATTERN.sub(" ", " ".join(decision.takes)).casefold()
    misquoted = [
        note.words
        for note in written.direction_notes
        if _WHITESPACE_PATTERN.sub(" ", note.words.strip()).casefold() not in spoken
    ]
    if misquoted:
        raise AnswerError(f"These direction notes are not the reviewer's words: {misquoted}")

    unreferenced = [drawing.name for drawing in decision.drawings if drawing.name not in written.comment]
    if unreferenced:
        raise AnswerError(f"The comment does not reference these drawings: {unreferenced}")

    logger.debug(f"[Decisions] {decision.ticket}: the answer has {len(written.direction_notes)} direction notes")
    return written
