"""Collecting reference images."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelierd.catalog.models import Reference
from atelierd.catalog.references import add_reference, collect_references
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


class TestAddReference:
    def test_copies_the_image_into_the_drop_folder(self, project_root: Path, tmp_path: Path) -> None:
        image = tmp_path / "concept.png"
        image.write_bytes(b"concept")
        assert add_reference(project_root, AtelierConfig(), image) == "SourceArt/References/concept.png"
        assert (project_root / "SourceArt" / "References" / "concept.png").read_bytes() == b"concept"

    def test_the_same_image_is_not_copied_twice(self, project_root: Path, tmp_path: Path) -> None:
        image = tmp_path / "concept.png"
        image.write_bytes(b"concept")
        add_reference(project_root, AtelierConfig(), image)
        assert add_reference(project_root, AtelierConfig(), image) == "SourceArt/References/concept.png"
        assert not (project_root / "SourceArt" / "References" / "concept-2.png").exists()

    def test_a_different_image_of_the_same_name_is_never_overwritten(self, project_root: Path, tmp_path: Path) -> None:
        first = tmp_path / "first" / "concept.png"
        second = tmp_path / "second" / "concept.png"
        for image, content in ((first, b"one"), (second, b"two")):
            image.parent.mkdir()
            image.write_bytes(content)

        add_reference(project_root, AtelierConfig(), first)
        assert add_reference(project_root, AtelierConfig(), second) == "SourceArt/References/concept-2.png"
        assert (project_root / "SourceArt" / "References" / "concept.png").read_bytes() == b"one"

    def test_the_drop_folder_comes_from_the_config(self, project_root: Path, tmp_path: Path) -> None:
        image = tmp_path / "mood.jpg"
        image.write_bytes(b"mood")
        config = AtelierConfig(references=ReferencesConfig(drop_folder="References/Dropped"))
        assert add_reference(project_root, config, image) == "References/Dropped/mood.jpg"

    def test_a_file_that_is_not_an_image_is_rejected(self, project_root: Path, tmp_path: Path) -> None:
        notes = tmp_path / "notes.txt"
        notes.write_text("not an image", encoding="utf-8")
        with pytest.raises(ValueError, match="not an image"):
            add_reference(project_root, AtelierConfig(), notes)
