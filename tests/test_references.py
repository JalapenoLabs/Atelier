"""Collecting reference images."""

from __future__ import annotations

from pathlib import Path

from atelierd.catalog.models import Reference
from atelierd.catalog.references import collect_references
from atelierd.config import AtelierConfig, ReferencesConfig


class TestCollectReferences:
    def test_default_globs_find_images_tagged_by_folder(self, project_root: Path) -> None:
        references = collect_references(project_root, AtelierConfig())
        assert references == [
            Reference(path="References/Palette/palette.png", tag="Palette"),
            Reference(path="SourceArt/References/Creatures/ref_beast.png", tag="Creatures"),
            Reference(path="SourceArt/References/ref_mood.jpg", tag="References"),
        ]

    def test_custom_globs_replace_the_defaults(self, project_root: Path) -> None:
        config = AtelierConfig(references=ReferencesConfig(globs=("SourceArt/Weapons/**/Review/**",)))
        references = collect_references(project_root, config)
        assert [reference.path for reference in references] == [
            "SourceArt/Weapons/Carbine/Review/Carbine_Front.png",
            "SourceArt/Weapons/Carbine/Review/Detail/Carbine_Muzzle.jpg",
        ]

    def test_an_image_matched_twice_is_listed_once(self, project_root: Path) -> None:
        config = AtelierConfig(references=ReferencesConfig(globs=("References/**", "References/Palette/*.png")))
        assert len(collect_references(project_root, config)) == 1
