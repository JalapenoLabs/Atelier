"""Grouping renders by stem, on render names the real projects use."""

from __future__ import annotations

from atelier.gui.gallery import group_renders_by_stem


def _groups(paths: list[str]) -> dict[str, list[str]]:
    return {group.stem: [path.rsplit("/", 1)[-1] for path in group.renders] for group in group_renders_by_stem(paths)}


class TestGroupRendersByStem:
    def test_views_of_one_subject_share_a_group(self) -> None:
        paths = ["Review/Carbine_Front.png", "Review/Carbine_Side.png", "Review/Detail/Carbine_Muzzle.jpg"]
        assert _groups(paths) == {"Carbine": ["Carbine_Front.png", "Carbine_Side.png", "Carbine_Muzzle.jpg"]}

    def test_numbered_shots_join_the_unnumbered_one(self) -> None:
        paths = ["CB_CargoBooster.png", "CB_CargoBooster_2.png", "CB_CargoBooster_3.png"]
        assert _groups(paths) == {"CB_CargoBooster": paths}

    def test_variants_of_a_preview_group_under_the_preview(self) -> None:
        paths = ["Hull_preview.png", "Hull_preview_port.png", "Hull_preview_stern.png", "DeckKit_preview.png"]
        assert _groups(paths) == {
            "DeckKit_preview": ["DeckKit_preview.png"],
            "Hull_preview": ["Hull_preview.png", "Hull_preview_port.png", "Hull_preview_stern.png"],
        }

    def test_subjects_with_underscores_keep_their_whole_name(self) -> None:
        paths = ["Hair_Braid_Front.png", "Hair_Braid_Back.png", "Hair_Bun_Front.png", "Hair_Bun_Back.png"]
        assert _groups(paths) == {
            "Hair_Braid": ["Hair_Braid_Back.png", "Hair_Braid_Front.png"],
            "Hair_Bun": ["Hair_Bun_Back.png", "Hair_Bun_Front.png"],
        }

    def test_a_render_sharing_nothing_is_its_own_group(self) -> None:
        assert _groups(["Pool_preview.png"]) == {"Pool_preview": ["Pool_preview.png"]}
