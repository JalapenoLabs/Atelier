"""Revisions: snapshots in the object store, restore and its undo, prune, and standard render bookkeeping."""

from __future__ import annotations

import hashlib
import queue
import shutil
from pathlib import Path

import pytest

from atelierd.blender import BlenderError
from atelierd.catalog import Catalog, CatalogChange, CatalogEvent
from atelierd.project import open_project
from atelierd.revisions import (
    STANDARD_VIEWS,
    FileKind,
    RenderState,
    RevisionEvent,
    RevisionEventKind,
    Revisions,
    RevisionTracker,
    RevisionTrigger,
    SourceBusyError,
)
from atelierd.revisions import renders as renders_module
from atelierd.revisions.objects import ObjectStore

CARBINE = "SourceArt/Weapons/Carbine/Carbine.blend"
CARBINE_EXPORT = "SourceArt/Weapons/Carbine/Export/SK_Carbine.fbx"
CARBINE_README = "SourceArt/Weapons/Carbine/README.md"
DOG_TAG = "SourceArt/Weapons/Charms/DogTag/DogTag.blend"
SHARED_RUNNER = "SourceArt/_lib/run_asset.py"


@pytest.fixture
def catalog(project_root: Path, tmp_path: Path) -> Catalog:
    catalog = Catalog(open_project(project_root, state_dir=tmp_path / "state"))
    catalog.scan()
    return catalog


@pytest.fixture
def revisions(catalog: Catalog) -> Revisions:
    return Revisions(catalog.project)


class FakeBlender:
    """Stands in for `run_blender_script` in the render module, writing what the render script would."""

    def __init__(self) -> None:
        self.rendered: list[str] = []
        self.is_failing = False

    def __call__(self, script: Path, script_arguments: list[str]) -> str:
        export_file, output_folder, name, _ = script_arguments
        self.rendered.append(name)
        if self.is_failing:
            raise BlenderError("Blender fell over")
        for view in STANDARD_VIEWS:
            (Path(output_folder) / f"{name}-{view}.png").write_bytes(Path(export_file).read_bytes() + view.encode())
        return ""


@pytest.fixture
def fake_blender(monkeypatch: pytest.MonkeyPatch) -> FakeBlender:
    fake = FakeBlender()
    monkeypatch.setattr(renders_module, "run_blender_script", fake)
    return fake


def _tree(root: Path, folder: str) -> dict[str, bytes]:
    """Every file under a project folder, by project-relative path, with its bytes."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted((root / folder).rglob("*"))
        if path.is_file()
    }


class TestRecording:
    def test_catching_up_records_one_baseline_per_source_then_nothing(self, revisions: Revisions) -> None:
        recorded = revisions.catch_up()

        assert len(recorded) == 7
        assert {revision.trigger for revision in recorded} == {RevisionTrigger.BASELINE}
        assert {revision.number for revision in recorded} == {1}
        assert revisions.catch_up() == []

    def test_a_revision_lists_every_file_of_the_source_by_kind(self, revisions: Revisions) -> None:
        revision = revisions.record(CARBINE, RevisionTrigger.CHANGE)

        assert revision is not None
        kinds = {file.path: file.kind for file in revision.files}
        assert kinds[CARBINE] is FileKind.BLEND
        assert kinds["SourceArt/Weapons/Carbine/build_carbine.py"] is FileKind.BUILDER
        assert kinds[CARBINE_README] is FileKind.README
        assert kinds[CARBINE_EXPORT] is FileKind.EXPORT
        assert kinds["SourceArt/Weapons/Carbine/Review/Carbine_Front.png"] is FileKind.RENDER

    def test_each_files_bytes_are_stored_once_under_their_hash(self, revisions: Revisions, catalog: Catalog) -> None:
        readme = catalog.project.root / CARBINE_README
        readme.write_text("first\n", encoding="utf-8")
        revisions.record(CARBINE, RevisionTrigger.CHANGE)
        (catalog.project.root / CARBINE_EXPORT).write_bytes(b"mesh v2")
        revision = revisions.record(CARBINE, RevisionTrigger.CHANGE)

        assert revision is not None
        sha256 = hashlib.sha256(b"first\n").hexdigest()
        assert revision.manifest[CARBINE_README] == sha256
        assert revisions.object_path(sha256).read_bytes() == b"first\n"
        assert revision.number == 2

    def test_an_unchanged_source_records_nothing(self, revisions: Revisions) -> None:
        assert revisions.record(CARBINE, RevisionTrigger.CHANGE) is not None
        assert revisions.record(CARBINE, RevisionTrigger.WORKER_TURN) is None

    def test_an_unchanged_file_is_not_read_again(
        self,
        revisions: Revisions,
        catalog: Catalog,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        revisions.record(CARBINE, RevisionTrigger.CHANGE)
        stored: list[str] = []
        real_store = ObjectStore.store

        def counting_store(store: ObjectStore, file: Path) -> tuple[str, int]:
            stored.append(file.name)
            return real_store(store, file)

        monkeypatch.setattr(ObjectStore, "store", counting_store)
        (catalog.project.root / CARBINE_EXPORT).write_bytes(b"mesh v2")
        revisions.record(CARBINE, RevisionTrigger.CHANGE)

        assert stored == ["SK_Carbine.fbx"]

    def test_recording_an_unknown_source_fails(self, revisions: Revisions) -> None:
        with pytest.raises(KeyError):
            revisions.record("SourceArt/Nope.blend", RevisionTrigger.CHANGE)


class TestRestore:
    def test_restore_then_undo_round_trips_the_files_byte_for_byte(
        self,
        revisions: Revisions,
        catalog: Catalog,
    ) -> None:
        root = catalog.project.root
        folder = "SourceArt/Weapons/Carbine"
        first = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert first is not None
        first_tree = _tree(root, folder)

        # A rebuild: the export and README change, and a second export appears.
        (root / CARBINE_EXPORT).write_bytes(b"rebuilt mesh")
        (root / CARBINE_README).write_text("# Carbine, rebuilt\n", encoding="utf-8")
        (root / folder / "Export" / "SK_Carbine_Mag.fbx").write_bytes(b"magazine")
        catalog.scan()
        second = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert second is not None
        second_tree = _tree(root, folder)

        result = revisions.restore(CARBINE, first.id)

        assert _tree(root, folder) == first_tree
        assert result.pre_restore.id == second.id
        assert result.deleted == (f"{folder}/Export/SK_Carbine_Mag.fbx",)
        assert result.restored.trigger is RevisionTrigger.RESTORE
        assert result.restored.restored_from == first.id
        assert result.restored.manifest == first.manifest
        mark = revisions.get_restore_mark(CARBINE)
        assert mark is not None
        assert (mark.revision_id, mark.revision_number) == (first.id, 1)

        catalog.scan()
        undo = revisions.restore(CARBINE, result.pre_restore.id)

        assert _tree(root, folder) == second_tree
        assert undo.restored.manifest == second.manifest

    def test_shared_files_are_snapshotted_but_never_restored(self, revisions: Revisions, catalog: Catalog) -> None:
        runner = catalog.project.root / SHARED_RUNNER
        first = revisions.record(DOG_TAG, RevisionTrigger.CHANGE)
        assert first is not None
        assert SHARED_RUNNER in first.manifest

        runner.write_text("# the shared runner, newer\n", encoding="utf-8")
        revisions.record(DOG_TAG, RevisionTrigger.CHANGE)
        result = revisions.restore(DOG_TAG, first.id)

        assert result.skipped == (SHARED_RUNNER,)
        assert runner.read_text(encoding="utf-8") == "# the shared runner, newer\n"

    def test_restoring_a_busy_source_is_refused(self, catalog: Catalog) -> None:
        revisions = Revisions(catalog.project, is_source_busy=lambda source_path: source_path == CARBINE)
        first = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert first is not None

        with pytest.raises(SourceBusyError):
            revisions.restore(CARBINE, first.id)

    def test_a_revision_of_another_source_cannot_be_restored(self, revisions: Revisions) -> None:
        dog_tag = revisions.record(DOG_TAG, RevisionTrigger.CHANGE)
        assert dog_tag is not None
        with pytest.raises(ValueError, match="belongs to"):
            revisions.restore(CARBINE, dog_tag.id)

    def test_clearing_the_restore_mark(self, revisions: Revisions, catalog: Catalog) -> None:
        first = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert first is not None
        (catalog.project.root / CARBINE_README).write_text("changed\n", encoding="utf-8")
        revisions.restore(CARBINE, first.id)

        revisions.clear_restore_mark(CARBINE)

        assert revisions.get_restore_mark(CARBINE) is None


class TestPrune:
    def test_prune_keeps_the_newest_revisions_and_the_objects_they_name(
        self,
        revisions: Revisions,
        catalog: Catalog,
        fake_blender: FakeBlender,
    ) -> None:
        export = catalog.project.root / CARBINE_EXPORT
        revision_ids = []
        for version in range(4):
            export.write_bytes(f"mesh v{version}".encode())
            revision = revisions.record(CARBINE, RevisionTrigger.CHANGE)
            assert revision is not None
            revisions.render_standard(revision.id, CARBINE_EXPORT)
            revision_ids.append(revision.id)

        result = revisions.prune(keep=2)

        assert result.revisions_removed == 2
        assert result.objects_removed == 2
        assert result.render_folders_removed == 2
        kept = revisions.history(CARBINE).revisions
        assert [revision.id for revision in kept] == revision_ids[:1:-1]
        for revision in kept:
            assert all(revisions.object_path(file.sha256).is_file() for file in revision.files)
        assert {render.state for render in revisions.history(CARBINE).standard_renders} == {RenderState.DONE}

    def test_prune_keeps_the_revision_a_source_was_restored_to(self, revisions: Revisions, catalog: Catalog) -> None:
        export = catalog.project.root / CARBINE_EXPORT
        first = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert first is not None
        export.write_bytes(b"mesh v2")
        revisions.record(CARBINE, RevisionTrigger.CHANGE)
        revisions.restore(CARBINE, first.id)

        revisions.prune(keep=1)

        assert first.id in {revision.id for revision in revisions.history(CARBINE).revisions}

    def test_prune_must_keep_a_revision(self, revisions: Revisions) -> None:
        with pytest.raises(ValueError):
            revisions.prune(keep=0)


class TestStandardRenders:
    def test_a_baseline_waits_to_be_asked(self, revisions: Revisions) -> None:
        baseline = revisions.record(CARBINE, RevisionTrigger.BASELINE)
        assert baseline is not None
        assert revisions.exports_to_render(baseline) == []

    def test_only_changed_exports_are_rendered(self, revisions: Revisions, catalog: Catalog) -> None:
        revisions.record(CARBINE, RevisionTrigger.BASELINE)
        (catalog.project.root / CARBINE_README).write_text("changed\n", encoding="utf-8")
        readme_change = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        (catalog.project.root / CARBINE_EXPORT).write_bytes(b"mesh v2")
        export_change = revisions.record(CARBINE, RevisionTrigger.CHANGE)

        assert readme_change is not None and export_change is not None
        assert revisions.exports_to_render(readme_change) == []
        assert revisions.exports_to_render(export_change) == [CARBINE_EXPORT]

    def test_a_rendered_set_is_complete_and_shared_with_revisions_of_the_same_bytes(
        self,
        revisions: Revisions,
        catalog: Catalog,
        fake_blender: FakeBlender,
    ) -> None:
        first = revisions.record(CARBINE, RevisionTrigger.BASELINE)
        (catalog.project.root / CARBINE_README).write_text("changed\n", encoding="utf-8")
        second = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert first is not None and second is not None

        rendered = revisions.render_standard(second.id, CARBINE_EXPORT)

        assert rendered.state is RenderState.DONE
        assert set(rendered.images) == set(STANDARD_VIEWS)
        assert rendered.images["hero"] == revisions.renders_dir / str(second.id) / "SK_Carbine-hero.png"
        assert rendered.images["hero"].is_file()
        # The baseline holds the same export bytes, so it shows the same set without a second Blender run.
        assert revisions.standard_renders(first)[0].state is RenderState.DONE
        assert revisions.render_standard(first.id, CARBINE_EXPORT).state is RenderState.DONE
        assert fake_blender.rendered == ["SK_Carbine"]

    def test_a_new_revision_with_an_unchanged_export_reuses_its_set(
        self,
        revisions: Revisions,
        catalog: Catalog,
        fake_blender: FakeBlender,
    ) -> None:
        first = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert first is not None
        revisions.render_standard(first.id, CARBINE_EXPORT)
        (catalog.project.root / CARBINE_README).write_text("changed\n", encoding="utf-8")
        second = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert second is not None

        assert revisions.exports_to_render(second) == []
        assert revisions.standard_renders(second)[0].state is RenderState.DONE
        assert len(fake_blender.rendered) == 1

    def test_a_restore_shows_the_restored_revisions_renders_at_once(
        self,
        revisions: Revisions,
        catalog: Catalog,
        fake_blender: FakeBlender,
    ) -> None:
        first = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert first is not None
        revisions.render_standard(first.id, CARBINE_EXPORT)
        (catalog.project.root / CARBINE_EXPORT).write_bytes(b"mesh v2")
        revisions.record(CARBINE, RevisionTrigger.CHANGE)

        restored = revisions.restore(CARBINE, first.id).restored

        assert revisions.standard_renders(restored)[0].state is RenderState.DONE
        assert len(fake_blender.rendered) == 1

    def test_a_failed_render_is_recorded_and_not_retried(
        self,
        revisions: Revisions,
        fake_blender: FakeBlender,
    ) -> None:
        revision = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert revision is not None
        fake_blender.is_failing = True

        failed = revisions.render_standard(revision.id, CARBINE_EXPORT)

        assert failed.state is RenderState.FAILED
        assert "fell over" in failed.error
        assert revisions.render_standard(revision.id, CARBINE_EXPORT).state is RenderState.FAILED
        assert len(fake_blender.rendered) == 1

    def test_a_machine_without_blender_records_no_failure(
        self,
        revisions: Revisions,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("ATELIER_BLENDER", "no-such-blender-command")
        revision = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert revision is not None

        assert revisions.render_standard(revision.id, CARBINE_EXPORT).state is RenderState.MISSING

    def test_a_set_from_another_renderer_version_counts_as_missing(
        self,
        revisions: Revisions,
        fake_blender: FakeBlender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        revision = revisions.record(CARBINE, RevisionTrigger.CHANGE)
        assert revision is not None
        revisions.render_standard(revision.id, CARBINE_EXPORT)
        monkeypatch.setattr(renders_module, "RENDERER_VERSION", 99)

        assert revisions.standard_renders(revision)[0].state is RenderState.MISSING

    def test_namesake_exports_get_their_folders_in_the_name(self) -> None:
        names = renders_module.render_names(
            ["Rooms/Cabin/Export/SM_Bed.fbx", "Rooms/Pool/Export/SM_Bed.fbx", "A/B.fbx"]
        )
        assert names == {
            "Rooms/Cabin/Export/SM_Bed.fbx": "Rooms_Cabin_Export_SM_Bed",
            "Rooms/Pool/Export/SM_Bed.fbx": "Rooms_Pool_Export_SM_Bed",
            "A/B.fbx": "B",
        }


class TestTracker:
    def test_a_settled_change_records_a_revision_and_renders_its_changed_export(
        self,
        revisions: Revisions,
        catalog: Catalog,
        fake_blender: FakeBlender,
    ) -> None:
        events: queue.Queue[RevisionEvent] = queue.Queue()
        tracker = RevisionTracker(revisions, on_event=events.put)
        try:
            tracker.start()
            for _ in range(7):
                assert events.get(timeout=10).kind is RevisionEventKind.RECORDED

            (catalog.project.root / CARBINE_EXPORT).write_bytes(b"rebuilt")
            tracker.handle_catalog_event(CatalogEvent(change=CatalogChange(), touched=(CARBINE,)))

            recorded = events.get(timeout=10)
            rendered = events.get(timeout=10)
        finally:
            tracker.stop()

        assert (recorded.kind, recorded.source) == (RevisionEventKind.RECORDED, CARBINE)
        assert (rendered.kind, rendered.revision_id) == (RevisionEventKind.RENDERED, recorded.revision_id)
        history = tracker.history(CARBINE)
        assert [revision.trigger for revision in history.revisions] == [
            RevisionTrigger.CHANGE,
            RevisionTrigger.BASELINE,
        ]
        assert history.standard_renders[0].state is RenderState.DONE

    def test_a_requested_render_shows_as_pending_until_it_is_done(
        self,
        revisions: Revisions,
        fake_blender: FakeBlender,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        baseline = revisions.record(CARBINE, RevisionTrigger.BASELINE)
        assert baseline is not None
        events: queue.Queue[RevisionEvent] = queue.Queue()
        tracker = RevisionTracker(revisions, on_event=events.put)
        released = queue.Queue[None]()
        real_render = revisions.render_standard

        def held_render(revision_id: int, export_path: str) -> object:
            released.get(timeout=10)
            return real_render(revision_id, export_path)

        monkeypatch.setattr(revisions, "render_standard", held_render)
        try:
            tracker.request_standard_renders(baseline.id, CARBINE_EXPORT)
            assert tracker.history(CARBINE).standard_renders[0].state is RenderState.PENDING
            released.put(None)
            assert events.get(timeout=10).kind is RevisionEventKind.RENDERED
        finally:
            tracker.stop()

        assert tracker.history(CARBINE).standard_renders[0].state is RenderState.DONE


@pytest.mark.skipif(shutil.which("blender") is None, reason="Blender is not installed")
def test_blender_renders_every_standard_view_of_a_real_export(catalog: Catalog, revisions: Revisions) -> None:
    crate = Path(__file__).parent / "fixtures" / "meshes" / "SM_TestCrate.fbx"
    shutil.copy(crate, catalog.project.root / CARBINE_EXPORT)
    revision = revisions.record(CARBINE, RevisionTrigger.CHANGE)
    assert revision is not None

    rendered = revisions.render_standard(revision.id, CARBINE_EXPORT)

    assert rendered.state is RenderState.DONE, rendered.error
    from PySide6.QtGui import QImage

    for view, image_path in rendered.images.items():
        image = QImage(str(image_path))
        assert (image.width(), image.height()) == (1024, 1024), view
        # The crate covers part of the frame: the centre is not the backdrop's colour at the corner.
        assert image.pixel(512, 512) != image.pixel(4, 4), view
