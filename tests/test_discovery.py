"""Discovery conventions and `atelier.toml` overrides, on the fixture project."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelierd.catalog.discovery import discover_sources
from atelierd.catalog.models import DiscoveredSource
from atelierd.config import AtelierConfig, SourceOverride, load_config

CARBINE = "SourceArt/Weapons/Carbine/Carbine.blend"
DOG_TAG = "SourceArt/Weapons/Charms/DogTag/DogTag.blend"
CREWMATE = "SourceArt/Crewmate/Crewmate_Base.blend"
DECK_KIT = "SourceArt/Ship/DeckKit.blend"
HULL = "SourceArt/Ship/Hull.blend"
ROOMS = "SourceArt/Rooms/Rooms.blend"
DINING_SPA = "SourceArt/Rooms/DiningSpa.blend"


def _discover(project_root: Path, config: AtelierConfig | None = None) -> dict[str, DiscoveredSource]:
    sources, _ = discover_sources(project_root, config or load_config(project_root / "atelier.toml"))
    return {source.path: source for source in sources}


class TestWhatIsASource:
    def test_every_blend_under_the_roots_is_a_source(self, project_root: Path) -> None:
        sources = _discover(project_root)
        assert sorted(sources) == sorted([CARBINE, DOG_TAG, CREWMATE, DECK_KIT, HULL, ROOMS, DINING_SPA])

    def test_private_folders_and_caches_are_never_sources(self, project_root: Path) -> None:
        # `_lib/test/TestCrate.blend` sits below a private folder, not directly in one.
        sources = _discover(project_root)
        assert not any("/_" in path or "__pycache__" in path for path in sources)

    def test_blender_backups_are_neither_sources_nor_files(self, project_root: Path) -> None:
        carbine = _discover(project_root)[CARBINE]
        all_paths = [carbine.path, *carbine.exports, *carbine.renders]
        assert not any(path.endswith((".blend1", ".blend@")) for path in all_paths)

    def test_sources_come_back_sorted(self, project_root: Path) -> None:
        sources, _ = discover_sources(project_root, AtelierConfig())
        paths = [source.path for source in sources]
        assert paths == sorted(paths)

    def test_overlapping_roots_find_each_source_once(self, project_root: Path) -> None:
        config = AtelierConfig(roots=("SourceArt", "SourceArt/Weapons", "SourceArt"))
        sources, _ = discover_sources(project_root, config)
        paths = [source.path for source in sources]
        assert len(paths) == len(set(paths)) == 7

    def test_a_missing_root_is_skipped(self, project_root: Path) -> None:
        config = AtelierConfig(roots=("SourceArt", "NoSuchFolder"))
        sources, _ = discover_sources(project_root, config)
        assert len(sources) == 7


class TestExportsAndRenders:
    def test_one_folder_per_asset(self, project_root: Path) -> None:
        carbine = _discover(project_root)[CARBINE]
        assert carbine.exports == ("SourceArt/Weapons/Carbine/Export/SK_Carbine.fbx",)
        assert carbine.renders == (
            "SourceArt/Weapons/Carbine/Carbine_preview.png",
            "SourceArt/Weapons/Carbine/Review/Carbine_Front.png",
            "SourceArt/Weapons/Carbine/Review/Detail/Carbine_Muzzle.jpg",
        )

    def test_textures_beside_exports_are_not_renders(self, project_root: Path) -> None:
        carbine = _discover(project_root)[CARBINE]
        assert "SourceArt/Weapons/Carbine/Export/T_Carbine_D.png" not in carbine.renders

    def test_exports_and_renders_folders_are_recognised(self, project_root: Path) -> None:
        crewmate = _discover(project_root)[CREWMATE]
        assert crewmate.exports == ("SourceArt/Crewmate/Exports/SK_Crewmate_Base.fbx",)
        assert crewmate.renders == ("SourceArt/Crewmate/Renders/Crewmate_Base_Front.png",)

    def test_other_subfolders_are_not_attached(self, project_root: Path) -> None:
        # Source/ holds an imported input mesh and Textures/ holds textures; neither is an output.
        sources = _discover(project_root)
        assert "SourceArt/Crewmate/Source/SKM_Manny_Simple.fbx" not in sources[CREWMATE].exports
        assert sources[DOG_TAG].renders == ()

    def test_kits_sharing_a_folder_keep_the_files_named_after_them(self, project_root: Path) -> None:
        sources = _discover(project_root)
        assert sources[DECK_KIT].renders == ("SourceArt/Ship/DeckKit_preview.png",)
        assert sources[HULL].renders == ("SourceArt/Ship/Hull_preview_port.png",)
        assert sources[DINING_SPA].renders == ("SourceArt/Rooms/DiningSpa_contact_sheet.png",)

    def test_the_longest_kit_name_owns_a_file(self, project_root: Path) -> None:
        (project_root / "SourceArt/Ship/Hull_Deck.blend").touch()
        (project_root / "SourceArt/Ship/Hull_Deck_preview.png").touch()
        sources = _discover(project_root)
        assert sources["SourceArt/Ship/Hull_Deck.blend"].renders == ("SourceArt/Ship/Hull_Deck_preview.png",)
        assert sources[HULL].renders == ("SourceArt/Ship/Hull_preview_port.png",)

    def test_kits_sharing_a_folder_share_its_unnamed_files(self, project_root: Path) -> None:
        sources = _discover(project_root)
        assert sources[DECK_KIT].exports == ("SourceArt/Ship/Export/SM_Wall.fbx",)
        assert sources[HULL].exports == ("SourceArt/Ship/Export/SM_Wall.fbx",)


class TestBuilderAndReadme:
    def test_single_builder_in_the_folder(self, project_root: Path) -> None:
        carbine = _discover(project_root)[CARBINE]
        assert carbine.builder == "SourceArt/Weapons/Carbine/build_carbine.py"
        assert carbine.readme == "SourceArt/Weapons/Carbine/README.md"

    def test_builders_in_a_shared_folder_pair_by_name(self, project_root: Path) -> None:
        sources = _discover(project_root)
        assert sources[DECK_KIT].builder == "SourceArt/Ship/build_deck_kit.py"
        assert sources[HULL].builder == "SourceArt/Ship/build_hull.py"
        assert sources[ROOMS].builder == "SourceArt/Rooms/build_rooms.py"

    def test_one_builder_in_a_shared_folder_is_not_every_kit_s(self, project_root: Path) -> None:
        (project_root / "SourceArt/Ship/build_deck_kit.py").unlink()
        sources = _discover(project_root)
        assert sources[DECK_KIT].builder is None
        assert sources[HULL].builder == "SourceArt/Ship/build_hull.py"

    def test_the_longest_kit_name_owns_a_builder(self, project_root: Path) -> None:
        (project_root / "SourceArt/Rooms/Dining.blend").touch()
        sources = _discover(project_root)
        assert sources["SourceArt/Rooms/Dining.blend"].builder is None
        assert sources[DINING_SPA].builder == "SourceArt/Rooms/build_dining_spa_rooms.py"

    def test_a_builder_may_extend_the_kit_name(self, project_root: Path) -> None:
        assert _discover(project_root)[DINING_SPA].builder == "SourceArt/Rooms/build_dining_spa_rooms.py"

    def test_readme_rebuild_command_names_the_builder(self, project_root: Path) -> None:
        dog_tag = _discover(project_root)[DOG_TAG]
        assert dog_tag.builder == "SourceArt/_lib/run_asset.py"
        assert dog_tag.readme == "SourceArt/Weapons/Charms/DogTag/README.md"

    def test_readme_naming_a_missing_script_gives_no_builder(self, project_root: Path) -> None:
        readme = project_root / "SourceArt/Weapons/Charms/DogTag/README.md"
        readme.write_text("blender -b --python SourceArt/_lib/gone.py -- DogTag.blend\n", encoding="utf-8")
        assert _discover(project_root)[DOG_TAG].builder is None

    def test_builder_in_a_subfolder_is_not_found_by_convention(self, project_root: Path) -> None:
        assert _discover(project_root)[CREWMATE].builder is None


class TestOutputOverrides:
    def test_override_replaces_the_conventional_outputs(self, project_root: Path) -> None:
        rooms = _discover(project_root)[ROOMS]
        assert rooms.has_output_override
        assert rooms.exports == ("SourceArt/Rooms/Cabin/Export/SM_Bed.fbx", "SourceArt/Rooms/Pool/Export/SM_Pool.fbx")
        assert rooms.renders == (
            "SourceArt/Rooms/Cabin/Cabin_preview.png",
            "SourceArt/Rooms/Pool/Pool_preview.png",
            "SourceArt/Rooms/Rooms_contact_sheet.png",
        )

    def test_without_the_override_the_kit_has_only_its_folder_files(self, project_root: Path) -> None:
        rooms = _discover(project_root, AtelierConfig())[ROOMS]
        assert not rooms.has_output_override
        assert rooms.exports == ()
        assert rooms.renders == ("SourceArt/Rooms/Rooms_contact_sheet.png",)

    def test_override_keeps_the_conventional_builder_and_readme(self, project_root: Path) -> None:
        config = AtelierConfig(sources={CARBINE: SourceOverride(outputs=("SourceArt/Rooms/Galley/**",))})
        carbine = _discover(project_root, config)[CARBINE]
        assert carbine.exports == ("SourceArt/Rooms/Galley/Export/SM_Stove.fbx",)
        assert carbine.renders == ()
        assert carbine.builder == "SourceArt/Weapons/Carbine/build_carbine.py"
        assert carbine.readme == "SourceArt/Weapons/Carbine/README.md"

    def test_an_override_without_outputs_changes_nothing(self, project_root: Path) -> None:
        config = AtelierConfig(sources={CARBINE: SourceOverride()})
        assert _discover(project_root, config)[CARBINE] == _discover(project_root, AtelierConfig())[CARBINE]

    @pytest.mark.parametrize("override_path", ["SourceArt/Rooms/Room.blend", "SourceArt/_lib/test/TestCrate.blend"])
    def test_overrides_naming_no_source_are_reported(self, project_root: Path, override_path: str) -> None:
        config = AtelierConfig(sources={override_path: SourceOverride(outputs=("SourceArt/Rooms/**",))})
        _, unmatched_overrides = discover_sources(project_root, config)
        assert unmatched_overrides == [override_path]
