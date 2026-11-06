"""A ticket's comments as focus mode shows them: Markdown Qt can render, and dates as people say them."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from atelier.gui.thread import date_labels, readable_markdown

STAGE = frozenset({"WAY-60_Turnaround.png", "a name.png"})
CAPTIONS = {"WAY-60_Turnaround.png": "Turnaround", "WAY-60_Old.png": "Old"}
TICKET_URL = "https://tracker.example/issue/WAY-60"


def readable(text: str) -> str:
    return readable_markdown(text, CAPTIONS, STAGE, TICKET_URL)


class TestMarkdown:
    def test_an_image_of_the_delivery_links_to_the_stage(self) -> None:
        assert readable("![](WAY-60_Turnaround.png){width=100%}") == (
            "[Turnaround](atelier-stage:WAY-60_Turnaround.png)"
        )

    def test_an_image_of_an_earlier_round_links_to_the_ticket(self) -> None:
        assert readable("![](WAY-60_Old.png)") == f"[Old]({TICKET_URL})"

    def test_a_remote_image_never_loads(self) -> None:
        assert readable("![Mock](https://example.test/a.png)") == "[Mock](https://example.test/a.png)"

    def test_a_row_of_images_is_one_line(self) -> None:
        text = "Before\n\n![](WAY-60_Turnaround.png)\n\n![](WAY-60_Old.png)\n\nAfter"
        assert readable(text) == (
            f"Before\n\n[Turnaround](atelier-stage:WAY-60_Turnaround.png) · [Old]({TICKET_URL})\n\nAfter"
        )

    def test_angle_brackets_are_text_outside_code_and_code_is_untouched(self) -> None:
        assert readable("SM_<Name> and `SM_<Name>` and <img src=x>") == (r"SM_\<Name> and `SM_<Name>` and \<img src=x>")

    def test_an_image_in_code_stays_code(self) -> None:
        assert readable("`![](x.png)`") == "`![](x.png)`"

    def test_a_tables_and_lists_pass_through(self) -> None:
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n\n- one\n- two"
        assert readable(text) == text


class TestDates:
    def test_the_date_with_its_ordinal_and_the_time_for_the_tooltip(self) -> None:
        # Local time, so build the timestamp from a local moment.
        moment = datetime(2026, 1, 1, 20, 56).astimezone()
        assert date_labels(moment.astimezone(UTC).isoformat()) == (
            "January 1st 2026",
            "January 1st 2026 at 8:56pm",
        )

    @pytest.mark.parametrize(
        ("day", "label"),
        [(2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"), (13, "13th"), (21, "21st"), (23, "23rd")],
    )
    def test_ordinals(self, day: int, label: str) -> None:
        moment = datetime(2026, 3, day, 12, 5).astimezone()
        assert date_labels(moment.isoformat()) == (f"March {label} 2026", f"March {label} 2026 at 12:05pm")

    def test_midnight_is_twelve(self) -> None:
        assert date_labels(datetime(2026, 3, 5, 0, 7).astimezone().isoformat())[1].endswith("at 12:07am")

    def test_an_unreadable_time_shows_as_it_is(self) -> None:
        assert date_labels("soon") == ("soon", "soon")
