"""Opening a project and keeping its state folder out of version control."""

from __future__ import annotations

from pathlib import Path

import pytest

from atelierd.project import ensure_state_ignored, open_project


class TestOpenProject:
    def test_defaults_live_in_the_project(self, project_root: Path) -> None:
        project = open_project(project_root)
        assert project.config_path == project_root / "atelier.toml"
        assert project.state_dir == project_root / ".atelier"
        assert project.database_path == project_root / ".atelier" / "state.db"
        assert project.is_state_in_project

    def test_opening_writes_nothing(self, project_root: Path) -> None:
        open_project(project_root)
        assert not (project_root / ".atelier").exists()

    def test_a_missing_folder_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(NotADirectoryError):
            open_project(tmp_path / "missing")


class TestEnsureStateIgnored:
    def test_appends_once_after_a_missing_trailing_newline(self, project_root: Path) -> None:
        project = open_project(project_root)
        ignore_file = project_root / ".dvignore"

        assert ensure_state_ignored(project) == [ignore_file]
        assert ensure_state_ignored(project) == []
        assert ignore_file.read_text(encoding="utf-8") == (
            "# Diversion ignores\nSaved/\nIntermediate/\n\n# Atelier's review state\n.atelier/\n"
        )

    @pytest.mark.parametrize("existing_entry", [".atelier", ".atelier/", "/.atelier/", "  /.atelier  "])
    def test_an_equivalent_entry_is_respected(self, tmp_path: Path, existing_entry: str) -> None:
        (tmp_path / ".gitignore").write_text(f"build/\n{existing_entry}\n", encoding="utf-8")
        assert ensure_state_ignored(open_project(tmp_path)) == []

    def test_every_existing_ignore_file_gets_the_entry(self, tmp_path: Path) -> None:
        (tmp_path / ".dvignore").write_text("Saved/\n", encoding="utf-8")
        (tmp_path / ".gitignore").write_text("", encoding="utf-8")
        changed = ensure_state_ignored(open_project(tmp_path))
        assert changed == [tmp_path / ".dvignore", tmp_path / ".gitignore"]
        assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "# Atelier's review state\n.atelier/\n"

    @pytest.mark.parametrize(("vcs_folder", "ignore_name"), [(".diversion", ".dvignore"), (".git", ".gitignore")])
    def test_creates_the_ignore_file_for_the_project_vcs(
        self,
        tmp_path: Path,
        vcs_folder: str,
        ignore_name: str,
    ) -> None:
        (tmp_path / vcs_folder).mkdir()
        assert ensure_state_ignored(open_project(tmp_path)) == [tmp_path / ignore_name]
        assert (tmp_path / ignore_name).read_text(encoding="utf-8").endswith(".atelier/\n")

    def test_a_project_without_version_control_is_left_alone(self, tmp_path: Path) -> None:
        assert ensure_state_ignored(open_project(tmp_path)) == []
        assert list(tmp_path.iterdir()) == []

    def test_a_state_dir_inside_the_project_is_the_folder_ignored(self, project_root: Path) -> None:
        project = open_project(project_root, state_dir=project_root / "review-state")
        assert ensure_state_ignored(project) == [project_root / ".dvignore"]
        lines = (project_root / ".dvignore").read_text(encoding="utf-8").splitlines()
        assert lines[-1] == "review-state/"
        assert ".atelier/" not in lines
        assert ensure_state_ignored(project) == []

    def test_state_outside_the_project_needs_no_entry(self, project_root: Path, tmp_path: Path) -> None:
        before = (project_root / ".dvignore").read_text(encoding="utf-8")
        project = open_project(project_root, state_dir=tmp_path / "state")
        assert ensure_state_ignored(project) == []
        assert (project_root / ".dvignore").read_text(encoding="utf-8") == before
