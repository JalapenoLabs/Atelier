"""Loading `atelier.toml`."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelierd.config import AtelierConfig, ConfigError, load_config
from atelierd.constants import DEFAULT_REFERENCE_GLOBS, DEFAULT_ROOTS, DEFAULT_SHARED


class TestLoadConfig:
    def test_a_missing_file_gives_the_documented_defaults(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "atelier.toml")
        assert config.roots == ("SourceArt",) == DEFAULT_ROOTS
        assert config.shared == DEFAULT_SHARED
        assert config.references.globs == DEFAULT_REFERENCE_GLOBS
        assert config.references.drop_folder == "SourceArt/References"
        assert config.sources == {}

    def test_every_key_is_optional(self, tmp_path: Path) -> None:
        config_path = tmp_path / "atelier.toml"
        config_path.write_text('[references]\ndrop_folder = "Art/Refs"\n', encoding="utf-8")
        config = load_config(config_path)
        assert config.references.drop_folder == "Art/Refs"
        assert config.references.globs == DEFAULT_REFERENCE_GLOBS
        assert config.roots == DEFAULT_ROOTS

    def test_source_overrides_are_read(self, tmp_path: Path) -> None:
        config_path = tmp_path / "atelier.toml"
        config_path.write_text(
            'roots = ["Art"]\n[sources."Art/Kit/Kit.blend"]\noutputs = ["Art/Kit/{A,B}/**"]\n',
            encoding="utf-8",
        )
        config = load_config(config_path)
        assert config.roots == ("Art",)
        assert config.sources["Art/Kit/Kit.blend"].outputs == ("Art/Kit/{A,B}/**",)

    def test_unknown_keys_are_rejected(self, tmp_path: Path) -> None:
        config_path = tmp_path / "atelier.toml"
        config_path.write_text('root = ["Art"]\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="root"):
            load_config(config_path)

    def test_invalid_toml_is_rejected(self, tmp_path: Path) -> None:
        config_path = tmp_path / "atelier.toml"
        config_path.write_text("roots = [\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_config(config_path)

    def test_the_fixture_config_loads(self, project_root: Path) -> None:
        config = load_config(project_root / "atelier.toml")
        assert isinstance(config, AtelierConfig)
        assert "SourceArt/Rooms/Rooms.blend" in config.sources
