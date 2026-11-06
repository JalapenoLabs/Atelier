"""How a delivery's files are presented: which shows first, which is measuring material, and what each is called.

A delivery holds the art and the agent's measuring material side by side, in whatever order they were uploaded, under
file names written for tools. `[review.images]` says which is which, because only the project knows its own names:
`hero` globs rank the art, best first, and `reference` globs send measuring material to the end; a file matching
neither stays between them, in the order it was uploaded.

A caption is a file name with everything a reviewer has no use for taken out: the ticket, the round (`r2`, `r2fix`) and
the words every file of the delivery opens with, so `WAY-53_r2_Rifle_Stored_Compare_negy.png` beside the rest of its
delivery is "Stored compare side", with `negy` translated by `[review.images].words`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

from wcmatch import fnmatch

from atelierd.config import ReviewImagesConfig
from atelierd.review.models import ReviewAttachment

# A round marker, wherever a file name has it: `r2`, `r2b`, `r2fix`.
_ROUND_WORD_PATTERN = re.compile(r"^r\d+[a-z]*$", re.IGNORECASE)
_WORD_SEPARATOR_PATTERN = re.compile(r"[\s_.\-]+")
# The start of each word of a CamelCase name, and of a number after a word: `BeforeAfter`, `Round2`, but not `1280x720`.
_WORD_START_PATTERN = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[a-zA-Z]{2})(?=\d)")


def arrange_attachments(
    ticket: str,
    attachments: Sequence[ReviewAttachment],
    images: ReviewImagesConfig,
) -> tuple[ReviewAttachment, ...]:
    """Put a delivery's files in the order they are shown, each with its caption and marked when it is reference."""
    captions = attachment_captions(ticket, [attachment.name for attachment in attachments], images.words)
    ranked: list[tuple[tuple[int, int], ReviewAttachment]] = []
    for attachment in attachments:
        reference_rank = _glob_rank(attachment.name, images.reference)
        hero_rank = _glob_rank(attachment.name, images.hero)
        if reference_rank is not None:
            # Reference wins over hero, so a broad hero glob can never promote measuring material.
            display_rank = (2, reference_rank)
        elif hero_rank is not None:
            display_rank = (0, hero_rank)
        else:
            display_rank = (1, 0)
        update = {"caption": captions[attachment.name], "is_reference": reference_rank is not None}
        ranked.append((display_rank, attachment.model_copy(update=update)))

    # A stable sort: files that rank the same stay in the order they were uploaded.
    ranked.sort(key=lambda entry: entry[0])
    return tuple(attachment for _, attachment in ranked)


def attachment_captions(ticket: str, names: Sequence[str], words: Mapping[str, str]) -> dict[str, str]:
    """Return what each of one comment's files is called, by file name."""
    word_lists = [_name_words(ticket, name) for name in names]
    shared_count = _shared_word_count(word_lists)
    captions = [_caption(name_words[shared_count:], words) for name_words in word_lists]

    # Two names that differ only by their round (`CutMarker`, `CutMarker_r2`) are told apart by it.
    caption_counts = Counter(captions)
    told_apart: dict[str, str] = {}
    for name, caption in zip(names, captions, strict=True):
        last_word = _WORD_SEPARATOR_PATTERN.split(PurePosixPath(name).stem)[-1]
        is_round_last = _ROUND_WORD_PATTERN.match(last_word) is not None
        told_apart[name] = f"{caption} {last_word}" if caption_counts[caption] > 1 and is_round_last else caption
    return told_apart


def _glob_rank(name: str, globs: tuple[str, ...]) -> int | None:
    """The place of the first glob a file name matches, or None when it matches none."""
    return next(
        (rank for rank, pattern in enumerate(globs) if fnmatch.fnmatch(name, pattern, flags=fnmatch.IGNORECASE)),
        None,
    )


def _name_words(ticket: str, name: str) -> list[str]:
    """A file name's words, without its extension, the ticket it opens with, or any round marker."""
    stem = PurePosixPath(name).stem
    if stem.casefold().startswith(ticket.casefold()):
        stem = stem[len(ticket) :]
    return [word for word in _WORD_SEPARATOR_PATTERN.split(stem) if word and not _ROUND_WORD_PATTERN.match(word)]


def _shared_word_count(word_lists: list[list[str]]) -> int:
    """How many words every file opens with, always leaving each at least one: a lone file keeps its whole name."""
    if len(word_lists) < 2:
        return 0
    shared_count = 0
    for words_at_position in zip(*word_lists, strict=False):
        if len({word.casefold() for word in words_at_position}) > 1:
            break
        shared_count += 1
    shortest = min(len(words) for words in word_lists)
    return min(shared_count, max(shortest - 1, 0))


def _caption(name_words: list[str], words: Mapping[str, str]) -> str:
    """Turn a file name's words into a caption: translated, CamelCase split, and in sentence case."""
    translations = {word.casefold(): translation for word, translation in words.items()}
    spoken = [translations.get(word.casefold()) or _WORD_START_PATTERN.sub(" ", word).lower() for word in name_words]
    caption = " ".join(spoken)
    return caption[:1].upper() + caption[1:]
