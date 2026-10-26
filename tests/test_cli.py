"""The `atelier scan` command line."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.cli import main


@pytest.fixture(autouse=True)
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    return data_home / "atelier" / "projects.toml"


class TestScanCommand:
    def test_lists_every_source_with_its_exports_and_renders(
        self,
        project_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert main(["scan", str(project_root)]) == 0
        output = capsys.readouterr().out

        assert "SourceArt/Rooms/Rooms.blend  [draft]" in output
        assert "  exports  2 (atelier.toml)\n    Cabin/Export/SM_Bed.fbx\n    Pool/Export/SM_Pool.fbx\n" in output
        assert "    Review/Carbine_Front.png\n" in output
        assert "FixtureProject: 7 sources, 7 exports, 10 renders, 3 references" in output

    def test_registers_the_project_for_scanning_by_name(
        self,
        project_root: Path,
        isolated_registry: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        main(["scan", str(project_root)])
        assert isolated_registry.is_file()
        capsys.readouterr()

        assert main(["scan", "FixtureProject"]) == 0
        assert "FixtureProject: 7 sources" in capsys.readouterr().out

    def test_state_dir_and_config_overrides(
        self,
        project_root: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        config_path = tmp_path / "scratch.toml"
        config_path.write_text('roots = ["SourceArt/Weapons"]\n', encoding="utf-8")
        state_dir = tmp_path / "state"

        exit_code = main(["scan", str(project_root), "--config", str(config_path), "--state-dir", str(state_dir)])

        assert exit_code == 0
        assert "FixtureProject: 2 sources" in capsys.readouterr().out
        assert (state_dir / "state.db").is_file()
        assert not (project_root / ".atelier").exists()

    def test_an_invalid_config_is_reported(
        self,
        project_root: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        (project_root / "atelier.toml").write_text("rootz = []\n", encoding="utf-8")
        assert main(["scan", str(project_root)]) == 2
        assert "Invalid config" in capsys.readouterr().err

    def test_an_unknown_project_is_reported(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert main(["scan", "NoSuchProject"]) == 2
        assert "neither a folder nor a registered project" in capsys.readouterr().err
