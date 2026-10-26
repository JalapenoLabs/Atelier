"""The source tree's rows: folders, sources and exports, the shortcuts that keep it short, filtering and expansion."""

from __future__ import annotations

from atelier.gui.tree import RowKind, SourceRow, TreeExpansion, build_source_rows
from atelierd.catalog import Source, SourceStatus

CARBINE = "SourceArt/Weapons/Carbine/Carbine.blend"
DECK_KIT = "SourceArt/Ship/DeckKit.blend"


def _source(path: str, status: SourceStatus = SourceStatus.DRAFT, exports: tuple[str, ...] = ()) -> Source:
    return Source(path=path, status=status, exports=exports)


SOURCES = [
    _source(CARBINE, SourceStatus.APPROVED, exports=("SourceArt/Weapons/Carbine/Export/SK_Carbine.fbx",)),
    _source("SourceArt/Weapons/Charms/DogTag/DogTag.blend"),
    _source(
        DECK_KIT,
        SourceStatus.IN_REVIEW,
        exports=(
            "SourceArt/Ship/Export/SM_Deck_Floor.fbx",
            "SourceArt/Ship/Export/SM_Deck_Rail.fbx",
            "SourceArt/Ship/Export/SM_Hatch.fbx",
        ),
    ),
    _source("SourceArt/Ship/Hull.blend"),
]


def _shape(rows: list[SourceRow]) -> list[tuple[int, str, str]]:
    return [(row.depth, row.name, row.kind) for row in rows]


class TestBuildSourceRows:
    def test_mirrors_the_folders_without_the_shared_top_folder(self) -> None:
        assert _shape(build_source_rows(SOURCES)) == [
            (0, "Ship", RowKind.FOLDER),
            (1, "DeckKit", RowKind.SOURCE),
            (1, "Hull", RowKind.SOURCE),
            (0, "Weapons", RowKind.FOLDER),
            (1, "Carbine", RowKind.SOURCE),
            (1, "Charms", RowKind.FOLDER),
            (2, "DogTag", RowKind.SOURCE),
        ]

    def test_a_folder_holding_only_its_namesake_source_is_shown_as_the_source(self) -> None:
        carbine_row = next(row for row in build_source_rows(SOURCES) if row.name == "Carbine")
        assert carbine_row.key == CARBINE
        assert carbine_row.kind == RowKind.SOURCE
        assert carbine_row.status == "approved"
        assert carbine_row.export_count == 1

    def test_text_filter_keeps_matches_and_the_folders_leading_to_them(self) -> None:
        assert _shape(build_source_rows(SOURCES, filter_text="dog")) == [
            (0, "Weapons", RowKind.FOLDER),
            (1, "Charms", RowKind.FOLDER),
            (2, "DogTag", RowKind.SOURCE),
        ]

    def test_status_filter(self) -> None:
        rows = build_source_rows(SOURCES, status=SourceStatus.IN_REVIEW)
        assert [row.name for row in rows if row.kind == RowKind.SOURCE] == ["DeckKit"]

    def test_collapsed_folders_hide_their_contents(self) -> None:
        expansion = TreeExpansion(collapsed_folders=frozenset({"SourceArt/Weapons"}))
        rows = build_source_rows(SOURCES, expansion=expansion)
        weapons = next(row for row in rows if row.name == "Weapons")
        assert not weapons.is_expanded
        assert "Carbine" not in [row.name for row in rows]

    def test_filtering_text_opens_collapsed_folders(self) -> None:
        expansion = TreeExpansion(collapsed_folders=frozenset({"SourceArt/Weapons"}))
        rows = build_source_rows(SOURCES, filter_text="carbine", expansion=expansion)
        assert "Carbine" in [row.name for row in rows]

    def test_no_match_gives_no_rows(self) -> None:
        assert build_source_rows(SOURCES, filter_text="nothing like this") == []


class TestExportRows:
    def test_sources_start_closed_and_only_those_with_exports_can_open(self) -> None:
        source_rows = [row for row in build_source_rows(SOURCES) if row.kind == RowKind.SOURCE]
        assert not any(row.is_expanded for row in source_rows)
        assert [row.name for row in source_rows if row.is_expandable] == ["DeckKit", "Carbine"]

    def test_an_open_source_lists_its_exports_as_children(self) -> None:
        rows = build_source_rows(SOURCES, expansion=TreeExpansion(expanded_sources=frozenset({DECK_KIT})))
        assert _shape(rows)[:5] == [
            (0, "Ship", RowKind.FOLDER),
            (1, "DeckKit", RowKind.SOURCE),
            (2, "SM_Deck_Floor", RowKind.EXPORT),
            (2, "SM_Deck_Rail", RowKind.EXPORT),
            (2, "SM_Hatch", RowKind.EXPORT),
        ]
        export_row = rows[2]
        assert export_row.key == "SourceArt/Ship/Export/SM_Deck_Floor.fbx"
        assert export_row.source == DECK_KIT
        # The status colour stays on the source row.
        assert export_row.status == ""

    def test_filtering_by_an_export_name_opens_its_source_with_just_the_matches(self) -> None:
        assert _shape(build_source_rows(SOURCES, filter_text="deck_")) == [
            (0, "Ship", RowKind.FOLDER),
            (1, "DeckKit", RowKind.SOURCE),
            (2, "SM_Deck_Floor", RowKind.EXPORT),
            (2, "SM_Deck_Rail", RowKind.EXPORT),
        ]

    def test_a_source_matching_by_path_keeps_its_own_open_state_and_every_export(self) -> None:
        closed_rows = build_source_rows(SOURCES, filter_text="deckkit")
        assert _shape(closed_rows) == [(0, "Ship", RowKind.FOLDER), (1, "DeckKit", RowKind.SOURCE)]

        expansion = TreeExpansion(expanded_sources=frozenset({DECK_KIT}))
        open_rows = build_source_rows(SOURCES, filter_text="deckkit", expansion=expansion)
        assert [row.name for row in open_rows if row.kind == RowKind.EXPORT] == [
            "SM_Deck_Floor",
            "SM_Deck_Rail",
            "SM_Hatch",
        ]

    def test_the_status_filter_applies_to_sources_not_export_names(self) -> None:
        assert build_source_rows(SOURCES, filter_text="sk_carbine", status=SourceStatus.DRAFT) == []


class TestTreeExpansion:
    def test_toggling_closes_a_folder_and_opens_a_source_then_undoes_both(self) -> None:
        rows = build_source_rows(SOURCES)
        ship = next(row for row in rows if row.name == "Ship")
        deck_kit = next(row for row in rows if row.name == "DeckKit")

        toggled = TreeExpansion().toggled(ship).toggled(deck_kit)
        assert toggled == TreeExpansion(
            collapsed_folders=frozenset({"SourceArt/Ship"}),
            expanded_sources=frozenset({DECK_KIT}),
        )
        assert toggled.toggled(ship).toggled(deck_kit) == TreeExpansion()
