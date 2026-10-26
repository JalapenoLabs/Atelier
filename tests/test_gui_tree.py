"""The source tree's rows: folders, the shortcuts that keep it short, filtering and collapsing."""

from __future__ import annotations

from atelier.gui.tree import SourceRow, build_source_rows
from atelierd.catalog import Source, SourceStatus


def _source(path: str, status: SourceStatus = SourceStatus.DRAFT, exports: int = 0) -> Source:
    return Source(path=path, status=status, exports=tuple(f"{path}.fbx{index}" for index in range(exports)))


SOURCES = [
    _source("SourceArt/Weapons/Carbine/Carbine.blend", SourceStatus.APPROVED, exports=1),
    _source("SourceArt/Weapons/Charms/DogTag/DogTag.blend"),
    _source("SourceArt/Ship/DeckKit.blend", SourceStatus.IN_REVIEW, exports=3),
    _source("SourceArt/Ship/Hull.blend"),
]


def _shape(rows: list[SourceRow]) -> list[tuple[int, str, bool]]:
    return [(row.depth, row.name, row.is_folder) for row in rows]


class TestBuildSourceRows:
    def test_mirrors_the_folders_without_the_shared_top_folder(self) -> None:
        assert _shape(build_source_rows(SOURCES)) == [
            (0, "Ship", True),
            (1, "DeckKit", False),
            (1, "Hull", False),
            (0, "Weapons", True),
            (1, "Carbine", False),
            (1, "Charms", True),
            (2, "DogTag", False),
        ]

    def test_a_folder_holding_only_its_namesake_source_is_shown_as_the_source(self) -> None:
        carbine_row = next(row for row in build_source_rows(SOURCES) if row.name == "Carbine")
        assert carbine_row.key == "SourceArt/Weapons/Carbine/Carbine.blend"
        assert not carbine_row.is_folder
        assert carbine_row.status == "approved"
        assert carbine_row.export_count == 1

    def test_text_filter_keeps_matches_and_the_folders_leading_to_them(self) -> None:
        assert _shape(build_source_rows(SOURCES, filter_text="dog")) == [
            (0, "Weapons", True),
            (1, "Charms", True),
            (2, "DogTag", False),
        ]

    def test_status_filter(self) -> None:
        rows = build_source_rows(SOURCES, status=SourceStatus.IN_REVIEW)
        assert [row.name for row in rows if not row.is_folder] == ["DeckKit"]

    def test_collapsed_folders_hide_their_contents(self) -> None:
        rows = build_source_rows(SOURCES, collapsed=frozenset({"SourceArt/Weapons"}))
        weapons = next(row for row in rows if row.name == "Weapons")
        assert not weapons.is_expanded
        assert "Carbine" not in [row.name for row in rows]

    def test_filtering_text_opens_collapsed_folders(self) -> None:
        rows = build_source_rows(SOURCES, filter_text="carbine", collapsed=frozenset({"SourceArt/Weapons"}))
        assert "Carbine" in [row.name for row in rows]

    def test_no_match_gives_no_rows(self) -> None:
        assert build_source_rows(SOURCES, filter_text="nothing like this") == []
