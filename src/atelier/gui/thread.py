"""A ticket's comments as focus mode reads them: Markdown Qt can render, and dates as people say them.

A tracker's comment text is untrusted data written for the tracker's own renderer, so what reaches Qt's Markdown is
reworked in two ways. Images never load: each becomes a link by its caption, to the stage for a file of the latest
delivery and to the ticket for one from an earlier round, which is not on disk. And raw HTML is written out as text,
because it is either a name in angle brackets (`SM_<Name>`), which Markdown would swallow, or markup that has no
business in the window.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import datetime
from urllib.parse import quote

logger = logging.getLogger(__name__)

# The scheme of a link that shows one of the delivery's files on the stage.
STAGE_LINK_SCHEME = "atelier-stage"

# `![alt](target)` with the size YouTrack may put after it, `{width=100%}`.
_IMAGE_PATTERN = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)(?:\{[^}]*\})?")
_CODE_PATTERN = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)
# An angle bracket that is not a URL Markdown should link.
_RAW_HTML_PATTERN = re.compile(r"<(?!https?://)")
_PARAGRAPH_BREAK_PATTERN = re.compile(r"\n\s*\n")
# A paragraph of nothing but image links, which a comment's trailing images all become.
_LINK_ONLY_PATTERN = re.compile(r"^(?:\[[^\]]*\]\([^)\s]*\)\s*)+$")
_ORDINAL_SUFFIX_BY_DIGIT = {1: "st", 2: "nd", 3: "rd"}
_LINK_SEPARATOR = " · "


def readable_markdown(
    text: str,
    captions: Mapping[str, str],
    stage_names: frozenset[str],
    ticket_url: str,
) -> str:
    """Return a comment's text as Markdown Qt can show, with its images as links.

    Args:
        text: the comment as the tracker holds it.
        captions: what the comment's attachments are called, by file name.
        stage_names: the file names the stage can show.
        ticket_url: where an image that is not on disk can be seen.
    """

    def image_link(match: re.Match[str]) -> str:
        alt_text, target = match.groups()
        if target.startswith(("http://", "https://")):
            return f"[{alt_text or 'Image'}]({target})"
        caption = captions.get(target) or alt_text or target
        if target in stage_names:
            return f"[{caption}]({STAGE_LINK_SCHEME}:{quote(target)})"
        return f"[{caption}]({ticket_url})"

    segments = _CODE_PATTERN.split(text)
    # `split` with one group puts the code at the odd places, which stays exactly as it is.
    for position in range(0, len(segments), 2):
        linked = _IMAGE_PATTERN.sub(image_link, segments[position])
        segments[position] = _RAW_HTML_PATTERN.sub(r"\\<", linked)
    return _joined_link_paragraphs("".join(segments))


def date_labels(created_at: str) -> tuple[str, str]:
    """A timestamp as the date to show and the full time for its tooltip, in local time."""
    try:
        created = datetime.fromisoformat(created_at).astimezone()
    except ValueError:
        logger.debug(f"[Thread] Could not read the time {created_at!r}")
        return created_at, created_at
    day = created.day
    is_teen = 11 <= day % 100 <= 13
    suffix = "th" if is_teen else _ORDINAL_SUFFIX_BY_DIGIT.get(day % 10, "th")
    date = f"{created:%B} {day}{suffix} {created:%Y}"
    hour = created.hour % 12 or 12
    meridiem = "am" if created.hour < 12 else "pm"
    return date, f"{date} at {hour}:{created:%M}{meridiem}"


def _joined_link_paragraphs(text: str) -> str:
    """Run neighbouring paragraphs of image links into one line, so a dozen images do not fill the panel."""
    paragraphs: list[str] = []
    is_previous_links = False
    for paragraph in _PARAGRAPH_BREAK_PATTERN.split(text):
        is_links = _LINK_ONLY_PATTERN.match(paragraph.strip()) is not None
        if is_links and is_previous_links:
            paragraphs[-1] += _LINK_SEPARATOR + paragraph.strip()
        else:
            paragraphs.append(paragraph)
        is_previous_links = is_links
    return "\n\n".join(paragraphs)
