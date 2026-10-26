"""The machine registry of known projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelierd.registry import (
    RegistryError,
    default_registry_path,
    load_registry,
    register_project,
    resolve_project_root,
)


class TestRegistry:
    def test_path_follows_xdg_data_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert default_registry_path() == tmp_path / "atelier" / "projects.toml"

    def test_path_defaults_to_local_share(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        assert default_registry_path() == Path.home() / ".local/share/atelier/projects.toml"

    def test_register_then_resolve_by_name(self, project_root: Path, tmp_path: Path) -> None:
        registry_path = tmp_path / "registry" / "projects.toml"
        assert register_project(project_root, registry_path) == "FixtureProject"
        assert register_project(project_root, registry_path) == "FixtureProject"
        assert load_registry(registry_path).projects["FixtureProject"].path == str(project_root)
        assert resolve_project_root("FixtureProject", registry_path) == project_root

    def test_a_path_resolves_without_the_registry(self, project_root: Path, tmp_path: Path) -> None:
        assert resolve_project_root(str(project_root), tmp_path / "none.toml") == project_root

    def test_an_unknown_name_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(RegistryError, match="neither a folder nor a registered project"):
            resolve_project_root("Nope", tmp_path / "projects.toml")

    def test_a_name_held_by_another_live_project_is_an_error(self, tmp_path: Path) -> None:
        registry_path = tmp_path / "projects.toml"
        (tmp_path / "a" / "Game").mkdir(parents=True)
        (tmp_path / "b" / "Game").mkdir(parents=True)
        register_project(tmp_path / "a" / "Game", registry_path)
        with pytest.raises(RegistryError, match="already registered"):
            register_project(tmp_path / "b" / "Game", registry_path)

    def test_a_moved_project_is_re_pointed(self, tmp_path: Path) -> None:
        registry_path = tmp_path / "projects.toml"
        old_root = tmp_path / "old" / "Game"
        new_root = tmp_path / "new" / "Game"
        old_root.mkdir(parents=True)
        new_root.mkdir(parents=True)
        register_project(old_root, registry_path)
        old_root.rmdir()
        register_project(new_root, registry_path)
        assert resolve_project_root("Game", registry_path) == new_root
