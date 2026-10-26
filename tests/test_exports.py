"""Converting exports to `.glb` with Blender, and the cache that keeps each conversion."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from atelierd.blender import BlenderError
from atelierd.exports import ExportConversionError, convert_export
from atelierd.exports import cache as export_cache
from atelierd.project import Project, open_project

CRATE_FIXTURE = Path(__file__).parent / "fixtures" / "meshes" / "SM_TestCrate.fbx"
CRATE = "SourceArt/Props/Crate/Export/SM_TestCrate.fbx"

is_blender_missing = shutil.which("blender") is None


@pytest.fixture
def project(tmp_path: Path) -> Project:
    root = tmp_path / "Project"
    (root / CRATE).parent.mkdir(parents=True)
    shutil.copy(CRATE_FIXTURE, root / CRATE)
    return open_project(root, state_dir=tmp_path / "state")


class _FakeBlender:
    """Stands in for `run_blender_script`, writing what the conversion script would."""

    def __init__(self) -> None:
        self.runs = 0
        self.error: BlenderError | None = None

    def __call__(self, script: Path, script_arguments: list[str]) -> str:
        self.runs += 1
        if self.error:
            raise self.error
        _, glb_path, stats_path, converter_version = script_arguments
        Path(glb_path).write_bytes(b"glTF")
        stats = {
            "converter_version": int(converter_version),
            "triangles": 12,
            "materials": ["Body"],
            "meshes": ["SM_TestCrate"],
            "collision_hulls": [],
            "is_skinned": False,
            "bounds_min_cm": [0, 0, 0],
            "bounds_max_cm": [1, 1, 1],
        }
        Path(stats_path).write_text(json.dumps(stats), encoding="utf-8")
        return ""


@pytest.fixture
def fake_blender(monkeypatch: pytest.MonkeyPatch) -> _FakeBlender:
    fake = _FakeBlender()
    monkeypatch.setattr(export_cache, "run_blender_script", fake)
    return fake


class TestCache:
    def test_a_conversion_is_cached_by_content(self, project: Project, fake_blender: _FakeBlender) -> None:
        first = convert_export(project, CRATE)
        second = convert_export(project, CRATE)

        assert fake_blender.runs == 1
        assert first == second
        assert first.glb_path == project.state_dir / "cache" / f"{first.sha256}.glb"
        assert first.glb_path.read_bytes() == b"glTF"

    def test_a_renamed_copy_hits_the_same_entry(self, project: Project, fake_blender: _FakeBlender) -> None:
        renamed = "SourceArt/Props/Crate/Export/SM_Renamed.fbx"
        shutil.copy(project.root / CRATE, project.root / renamed)

        assert convert_export(project, CRATE).sha256 == convert_export(project, renamed).sha256
        assert fake_blender.runs == 1

    def test_a_changed_export_is_converted_again(self, project: Project, fake_blender: _FakeBlender) -> None:
        first = convert_export(project, CRATE)
        with (project.root / CRATE).open("ab") as export_file:
            export_file.write(b"\0")
        assert convert_export(project, CRATE).sha256 != first.sha256
        assert fake_blender.runs == 2

    def test_a_result_from_another_converter_version_is_converted_again(
        self,
        project: Project,
        fake_blender: _FakeBlender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        convert_export(project, CRATE)
        monkeypatch.setattr(export_cache, "_CONVERTER_VERSION", 99)
        assert convert_export(project, CRATE).stats.converter_version == 99
        assert fake_blender.runs == 2

    def test_a_failed_conversion_leaves_nothing_behind(self, project: Project, fake_blender: _FakeBlender) -> None:
        fake_blender.error = BlenderError("boom")
        with pytest.raises(ExportConversionError, match="boom"):
            convert_export(project, CRATE)
        assert list((project.state_dir / "cache").iterdir()) == []

    def test_a_missing_export_is_reported(self, project: Project, fake_blender: _FakeBlender) -> None:
        with pytest.raises(ExportConversionError, match="does not exist"):
            convert_export(project, "SourceArt/Nope.fbx")
        assert fake_blender.runs == 0


@pytest.mark.skipif(is_blender_missing, reason="Blender is not installed")
class TestRealConversion:
    def test_the_crate_is_measured_in_unreal_centimetres(self, project: Project) -> None:
        converted = convert_export(project, CRATE)
        stats = converted.stats

        assert converted.glb_path.read_bytes()[:4] == b"glTF"
        assert stats.triangles == 12
        assert stats.materials == ("Body",)
        assert stats.meshes == ("SM_TestCrate",)
        assert stats.collision_hulls == ("UCX_SM_TestCrate_00",)
        assert not stats.is_skinned
        # The crate's centre is Unreal Y 20 (Blender Y -20), so its Y bounds tell the frames apart.
        assert stats.bounds_min_cm == pytest.approx((-50, -5, 0), abs=1e-3)
        assert stats.bounds_max_cm == pytest.approx((50, 45, 30), abs=1e-3)
