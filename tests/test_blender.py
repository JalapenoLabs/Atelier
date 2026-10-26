"""Running headless Blender: the environment it gets."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atelierd.blender import BlenderError, _blender_environment, find_blender


def _make_python_folder(folder: Path) -> Path:
    folder.mkdir(parents=True)
    (folder / "python3").touch()
    return folder


class TestBlenderEnvironment:
    def test_other_pythons_leave_path_but_blenders_folder_stays(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        mise_python = _make_python_folder(tmp_path / "mise" / "bin")
        venv_python = _make_python_folder(tmp_path / ".venv" / "bin")
        system_folder = _make_python_folder(tmp_path / "usr" / "bin")
        tools_folder = tmp_path / "tools"
        tools_folder.mkdir()
        path = os.pathsep.join(str(folder) for folder in (mise_python, venv_python, system_folder, tools_folder))
        monkeypatch.setenv("PATH", path)

        environment = _blender_environment(system_folder)

        assert environment["PATH"].split(os.pathsep) == [str(system_folder), str(tools_folder)]

    def test_variables_pointing_python_elsewhere_are_dropped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path))
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
        monkeypatch.setenv("ATELIER_TEST_KEEP", "kept")

        environment = _blender_environment(tmp_path)

        assert "VIRTUAL_ENV" not in environment
        assert "PYTHONPATH" not in environment
        assert environment["ATELIER_TEST_KEEP"] == "kept"


class TestFindBlender:
    def test_a_missing_command_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ATELIER_BLENDER", "no-such-blender-command")
        with pytest.raises(BlenderError, match="ATELIER_BLENDER"):
            find_blender()
