"""A delivery's files as the reviewer meets them: their order and captions, on names taken from a real queue."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelierd.config import ConfigError, ReviewImagesConfig, load_config
from atelierd.review import ReviewAttachment, arrange_attachments, attachment_captions

IMAGES = ReviewImagesConfig(
    hero=("*_Closeup.*", "*_Turnaround.*", "*_World.*", "*.gif", "*_IconSheet.*"),
    reference=("*_Before.*", "*_ReferenceSheet.*", "*_Compare_*", "*_Gate.*"),
    words={"negy": "side", "posx": "front", "Gate": "fit check", "BeforeAfter": "before and after"},
)


def arranged_captions(ticket: str, names: list[str]) -> list[str]:
    attachments = [ReviewAttachment(name=name, path=Path(name)) for name in names]
    return [attachment.caption for attachment in arrange_attachments(ticket, attachments, IMAGES)]


class TestOrder:
    def test_the_art_leads_and_measuring_material_goes_last(self) -> None:
        names = [
            "WAY-53_r2_Rifle_ReferenceSheet.png",
            "WAY-53_r2_Rifle_Compare_posx.png",
            "WAY-53_r2_Rifle_Gate.png",
            "WAY-53_r2_Rifle_Turnaround.png",
            "WAY-53_r2_Rifle_Filling.png",
            "WAY-53_r2_Rifle_IconSheet.png",
            "WAY-53_r2_Rifle_World.png",
        ]
        assert arranged_captions("WAY-53", names) == [
            "Turnaround",
            "World",
            "Icon sheet",
            "Filling",
            "Reference sheet",
            "Compare front",
            "Fit check",
        ]

    def test_files_that_rank_the_same_stay_in_upload_order(self) -> None:
        names = ["WAY-85_r2_Torch_Wall_Turnaround.png", "WAY-85_r2_Torch_Standing_Turnaround.png"]
        assert arranged_captions("WAY-85", names) == ["Wall turnaround", "Standing turnaround"]

    def test_reference_wins_over_a_broad_hero_glob(self) -> None:
        names = ["WAY-96_r2_Bear_Idle_Before.gif", "WAY-96_r2_Bear_Idle.gif"]
        attachments = [ReviewAttachment(name=name, path=Path(name)) for name in names]
        arranged = arrange_attachments("WAY-96", attachments, IMAGES)
        assert [(attachment.caption, attachment.is_reference) for attachment in arranged] == [
            ("Idle", False),
            ("Idle before", True),
        ]

    def test_globs_ignore_case(self) -> None:
        attachments = [ReviewAttachment(name="x_CLOSEUP.PNG", path=Path("x"))]
        images = ReviewImagesConfig(reference=("*_closeup.*",))
        assert arrange_attachments("WAY-1", attachments, images)[0].is_reference

    def test_a_project_with_no_config_keeps_the_upload_order(self) -> None:
        names = ["WAY-1-b.png", "WAY-1-a.png"]
        attachments = [ReviewAttachment(name=name, path=Path(name)) for name in names]
        arranged = arrange_attachments("WAY-1", attachments, ReviewImagesConfig())
        assert [attachment.name for attachment in arranged] == names


class TestCaptions:
    @pytest.mark.parametrize(
        ("ticket", "names", "expected"),
        [
            pytest.param(
                "WAY-64",
                ["WAY-64_r2_Coal_Gate.png", "WAY-64_r2_Coal_Stored_Gate.png", "WAY-64_r2_Coal_Compare_negy.png"],
                ["Fit check", "Stored fit check", "Compare side"],
                id="the-ticket-round-and-subject-go",
            ),
            pytest.param(
                "WAY-100",
                ["WAY-100_r2_FX_MuzzleFlash_Front_BeforeAfter.png", "WAY-100_r2_FX_MuzzleFlash_Side.gif"],
                ["Front before and after", "Side"],
                id="words-translate",
            ),
            pytest.param(
                "WAY-63",
                ["WAY-63_r2fix_Steel_ReferenceSheet.png", "WAY-63_r2fix_Steel_Gate.png"],
                ["Reference sheet", "Fit check"],
                id="a-round-with-a-suffix",
            ),
            pytest.param("WAY-125", ["WAY-125_r2_TargetCursor_r2.png"], ["Target cursor"], id="a-lone-file"),
            pytest.param(
                "WAY-294",
                ["WAY-294-forming-r2.jpg", "WAY-294-next-disabled-r2.jpg"],
                ["Forming", "Next disabled"],
                id="hyphens-and-a-trailing-round",
            ),
            pytest.param(
                "WAY-320",
                ["WAY-320-at-1280x720.jpg", "WAY-320-raid.jpg"],
                ["At 1280x720", "Raid"],
                id="a-size-stays-whole",
            ),
            pytest.param(
                "WAY-109",
                ["WAY-109_r2_Info_BeforeAfter.png", "WAY-109_r2_Family_Round2.png"],
                ["Info before and after", "Family round 2"],
                id="a-number-after-a-word",
            ),
            pytest.param(
                "WAY-122",
                ["WAY-122_r2_CutMarker_r2.png", "WAY-122_r2_CutMarker.png"],
                ["Cut marker r2", "Cut marker"],
                id="the-round-tells-two-apart",
            ),
            pytest.param(
                "WAY-96",
                ["WAY-96_r2_Bear_Idle.gif", "WAY-96_r2_Bear_Idle_Gameplay.gif"],
                ["Idle", "Idle gameplay"],
                id="each-keeps-at-least-one-word",
            ),
        ],
    )
    def test_a_caption_is_what_a_person_would_call_it(self, ticket: str, names: list[str], expected: list[str]) -> None:
        captions = attachment_captions(ticket, names, IMAGES.words)
        assert [captions[name] for name in names] == expected

    def test_nothing_to_caption(self) -> None:
        assert attachment_captions("WAY-1", [], {}) == {}


class TestConfig:
    def test_the_table_loads_and_its_keys_are_checked(self, tmp_path: Path) -> None:
        tracker = """
[review]
[review.tracker]
kind = "youtrack"
url = "https://tracker.example"
project = "WAY"
reviewer = "x"
[review.queues]
Art = "tag: Art"
[review.stages]
approve = "Done"
changes = "Develop"
"""
        config_path = tmp_path / "atelier.toml"
        config_path.write_text(tracker + '[review.images]\nhero = ["*_Day.*"]\n', encoding="utf-8")
        review = load_config(config_path).review
        assert review is not None
        assert review.images.hero == ("*_Day.*",)

        config_path.write_text(tracker + '[review.images]\nheroes = ["*_Day.*"]\n', encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(config_path)
