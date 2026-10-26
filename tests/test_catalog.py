"""Scanning into `state.db`: persistence, statuses and what a rescan reports."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

from atelierd.catalog import CameraBookmark, Catalog, SourceStatus
from atelierd.project import open_project

CARBINE = "SourceArt/Weapons/Carbine/Carbine.blend"


@pytest.fixture
def catalog(project_root: Path) -> Catalog:
    return Catalog(open_project(project_root))


class TestScan:
    def test_first_scan_adds_every_source_as_a_draft(self, catalog: Catalog) -> None:
        result = catalog.scan()
        assert len(result.change.added) == 7
        assert result.change.references_changed
        assert all(source.status is SourceStatus.DRAFT for source in result.sources)

    def test_state_db_is_sqlite_in_wal_mode(self, catalog: Catalog) -> None:
        catalog.scan()
        database_path = catalog.project.root / ".atelier" / "state.db"
        with sqlite3.connect(database_path) as connection:
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 4

    def test_stored_catalog_matches_the_scan(self, catalog: Catalog) -> None:
        result = catalog.scan()
        assert catalog.list_sources() == list(result.sources)
        assert catalog.list_references() == list(result.references)

    def test_an_unchanged_rescan_reports_nothing(self, catalog: Catalog) -> None:
        catalog.scan()
        assert catalog.scan().change.is_empty

    def test_a_new_export_marks_its_source_changed(self, catalog: Catalog) -> None:
        catalog.scan()
        (catalog.project.root / "SourceArt/Weapons/Carbine/Export/SK_Carbine_Mag.fbx").touch()
        change = catalog.scan().change
        assert change.changed == (CARBINE,)
        assert change.added == change.removed == ()

    def test_overlapping_roots_scan_without_a_duplicate_source(self, project_root: Path, tmp_path: Path) -> None:
        config_path = tmp_path / "overlap.toml"
        config_path.write_text('roots = ["SourceArt", "SourceArt/Weapons"]\n', encoding="utf-8")
        catalog = Catalog(open_project(project_root, config_path=config_path))
        assert len(catalog.scan().sources) == 7


class TestStatuses:
    def test_status_survives_a_rescan(self, catalog: Catalog) -> None:
        catalog.scan()
        catalog.set_status(CARBINE, SourceStatus.CHANGES_REQUESTED)
        (catalog.project.root / "SourceArt/Weapons/Carbine/Review/Carbine_Side.png").touch()
        result = catalog.scan()
        carbine = next(source for source in result.sources if source.path == CARBINE)
        assert carbine.status is SourceStatus.CHANGES_REQUESTED

    def test_status_survives_a_source_vanishing_and_returning(self, catalog: Catalog) -> None:
        catalog.scan()
        catalog.set_status(CARBINE, SourceStatus.APPROVED)
        blend = catalog.project.root / CARBINE
        parked = blend.with_name("parked")
        shutil.move(blend, parked)

        assert catalog.scan().change.removed == (CARBINE,)
        assert CARBINE not in {source.path for source in catalog.list_sources()}

        shutil.move(parked, blend)
        assert catalog.scan().change.added == (CARBINE,)
        carbine = next(source for source in catalog.list_sources() if source.path == CARBINE)
        assert carbine.status is SourceStatus.APPROVED

    def test_setting_the_status_of_an_unknown_source_fails(self, catalog: Catalog) -> None:
        catalog.scan()
        with pytest.raises(KeyError):
            catalog.set_status("SourceArt/Nope.blend", SourceStatus.APPROVED)


class TestStateLocation:
    def test_scan_adds_the_state_folder_to_the_ignore_file(self, catalog: Catalog) -> None:
        catalog.scan()
        assert (catalog.project.root / ".dvignore").read_text(encoding="utf-8").splitlines()[-1] == ".atelier/"

    def test_state_outside_the_project_writes_nothing_into_it(self, project_root: Path, tmp_path: Path) -> None:
        before = {path: path.stat().st_mtime_ns for path in project_root.rglob("*")}
        catalog = Catalog(open_project(project_root, state_dir=tmp_path / "elsewhere"))

        result = catalog.scan()

        assert len(result.sources) == 7
        assert (tmp_path / "elsewhere" / "state.db").is_file()
        assert {path: path.stat().st_mtime_ns for path in project_root.rglob("*")} == before

    def test_a_config_outside_the_project_is_used(self, project_root: Path, tmp_path: Path) -> None:
        outside_config = tmp_path / "other.toml"
        outside_config.write_text('roots = ["SourceArt/Ship"]\n', encoding="utf-8")
        catalog = Catalog(open_project(project_root, config_path=outside_config, state_dir=tmp_path / "state"))
        paths = [source.path for source in catalog.scan().sources]
        assert paths == ["SourceArt/Ship/DeckKit.blend", "SourceArt/Ship/Hull.blend"]


class TestCameraBookmarks:
    def test_saved_bookmarks_are_listed_by_name(self, catalog: Catalog) -> None:
        catalog.scan()
        side = CameraBookmark(name="side", target_cm=(0, 20, 10), yaw_degrees=90, pitch_degrees=0, distance_cm=150)
        hero = CameraBookmark(name="hero", target_cm=(1, 2, 3), yaw_degrees=225, pitch_degrees=-22, distance_cm=300)
        catalog.save_bookmark(CARBINE, side)
        catalog.save_bookmark(CARBINE, hero)
        assert catalog.list_bookmarks(CARBINE) == [hero, side]

    def test_saving_under_an_existing_name_replaces_it(self, catalog: Catalog) -> None:
        catalog.scan()
        first = CameraBookmark(name="hero", target_cm=(0, 0, 0), yaw_degrees=0, pitch_degrees=0, distance_cm=100)
        catalog.save_bookmark(CARBINE, first)
        catalog.save_bookmark(CARBINE, first.model_copy(update={"distance_cm": 250.0}))
        assert [bookmark.distance_cm for bookmark in catalog.list_bookmarks(CARBINE)] == [250.0]

    def test_deleting_removes_only_that_bookmark(self, catalog: Catalog) -> None:
        catalog.scan()
        for name in ("a", "b"):
            bookmark = CameraBookmark(name=name, target_cm=(0, 0, 0), yaw_degrees=0, pitch_degrees=0, distance_cm=1)
            catalog.save_bookmark(CARBINE, bookmark)
        catalog.delete_bookmark(CARBINE, "a")
        assert [bookmark.name for bookmark in catalog.list_bookmarks(CARBINE)] == ["b"]
        with pytest.raises(KeyError):
            catalog.delete_bookmark(CARBINE, "a")

    def test_bookmarks_need_a_present_source(self, catalog: Catalog) -> None:
        catalog.scan()
        with pytest.raises(KeyError):
            catalog.list_bookmarks("SourceArt/Nope.blend")
