"""The debounced file watcher keeping the catalog live, against real filesystem events."""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from atelierd.catalog import Catalog, CatalogEvent, CatalogWatcher, ScanResult
from atelierd.catalog import watcher as watcher_module
from atelierd.project import open_project

CARBINE = "SourceArt/Weapons/Carbine/Carbine.blend"
SETTLE_MS = 200
EVENT_TIMEOUT_SECONDS = 10


class WatchedProject:
    """A watcher running on its own thread, with the events it reported."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.events: queue.Queue[CatalogEvent] = queue.Queue()
        self.stop_event = threading.Event()
        watcher = CatalogWatcher(catalog, on_event=self.events.put, settle_ms=SETTLE_MS, max_batch_ms=5000)
        self.thread = threading.Thread(target=watcher.run, args=(self.stop_event,), daemon=True)

    def next_event(self) -> CatalogEvent:
        return self.events.get(timeout=EVENT_TIMEOUT_SECONDS)


@pytest.fixture
def watched(project_root: Path) -> Iterator[WatchedProject]:
    catalog = Catalog(open_project(project_root))
    catalog.scan()
    watched_project = WatchedProject(catalog)
    watched_project.thread.start()
    # Give the watcher time to install its watches before the test starts writing.
    time.sleep(0.5)
    yield watched_project
    watched_project.stop_event.set()
    watched_project.thread.join(timeout=EVENT_TIMEOUT_SECONDS)


class TestCatalogWatcher:
    def test_a_new_blend_is_added(self, watched: WatchedProject) -> None:
        new_blend = watched.catalog.project.root / "SourceArt/Weapons/Sidearm/Sidearm.blend"
        new_blend.parent.mkdir()
        new_blend.touch()

        event = watched.next_event()

        assert event.change.added == ("SourceArt/Weapons/Sidearm/Sidearm.blend",)
        assert "SourceArt/Weapons/Sidearm/Sidearm.blend" in {source.path for source in watched.catalog.list_sources()}

    def test_rewriting_an_export_touches_its_source(self, watched: WatchedProject) -> None:
        export = watched.catalog.project.root / "SourceArt/Weapons/Carbine/Export/SK_Carbine.fbx"
        export.write_bytes(b"new mesh")

        event = watched.next_event()

        assert event.change.is_empty
        assert event.touched == (CARBINE,)

    def test_a_burst_of_writes_settles_into_one_event(self, watched: WatchedProject) -> None:
        export_folder = watched.catalog.project.root / "SourceArt/Weapons/Carbine/Export"
        for index in range(5):
            (export_folder / f"SK_Carbine_Part{index}.fbx").touch()
            time.sleep(SETTLE_MS / 4000)

        event = watched.next_event()

        assert event.change.changed == (CARBINE,)
        with pytest.raises(queue.Empty):
            watched.events.get(timeout=SETTLE_MS * 3 / 1000)

    def test_blender_backups_and_private_folders_are_ignored(self, watched: WatchedProject) -> None:
        root = watched.catalog.project.root
        (root / "SourceArt/Weapons/Carbine/Carbine.blend1").write_bytes(b"backup")
        (root / "SourceArt/_lib/run_asset.py").write_text("# edited\n", encoding="utf-8")

        with pytest.raises(queue.Empty):
            watched.events.get(timeout=SETTLE_MS * 5 / 1000)

    def test_editing_the_config_applies_and_rewatches(self, watched: WatchedProject) -> None:
        config_path = watched.catalog.project.config_path
        config_path.write_text('roots = ["SourceArt/Ship"]\n', encoding="utf-8")

        event = watched.next_event()
        assert CARBINE in event.change.removed

        # The restarted watch follows the new roots, once it has installed its watches.
        time.sleep(0.5)
        (watched.catalog.project.root / "SourceArt/Ship/Funnel.blend").touch()
        assert watched.next_event().change.added == ("SourceArt/Ship/Funnel.blend",)

    def test_deleting_an_export_touches_the_source_that_had_it(self, watched: WatchedProject) -> None:
        (watched.catalog.project.root / "SourceArt/Weapons/Carbine/Export/SK_Carbine.fbx").unlink()

        event = watched.next_event()

        assert event.change.changed == (CARBINE,)
        assert event.touched == (CARBINE,)

    def test_config_saved_by_rename_keeps_being_watched(self, watched: WatchedProject) -> None:
        # Both saves leave the paths to watch as they were, so only the save itself can restart the watch.
        override = f'[sources."{CARBINE}"]\noutputs = ["SourceArt/Rooms/Galley/**"]\n'
        config_path = watched.catalog.project.config_path
        for config_text in (override, ""):
            # Editors save by writing a new file and renaming it over the old one.
            temporary_path = config_path.with_name("atelier.toml.swp")
            temporary_path.write_text(config_text, encoding="utf-8")
            os.replace(temporary_path, config_path)

            assert CARBINE in watched.next_event().change.changed
            time.sleep(0.5)

    def test_a_failed_rescan_does_not_end_the_watch(
        self,
        watched: WatchedProject,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        real_scan = watched.catalog.scan
        calls: list[int] = []

        def scan_failing_once() -> ScanResult:
            calls.append(1)
            if len(calls) == 1:
                raise OSError("folder vanished mid-walk")
            return real_scan()

        monkeypatch.setattr(watched.catalog, "scan", scan_failing_once)
        export_folder = watched.catalog.project.root / "SourceArt/Weapons/Carbine/Export"
        (export_folder / "SK_Carbine_First.fbx").touch()
        with pytest.raises(queue.Empty):
            watched.events.get(timeout=SETTLE_MS * 5 / 1000)

        (export_folder / "SK_Carbine_Second.fbx").touch()

        assert watched.next_event().change.changed == (CARBINE,)
        assert watched.thread.is_alive()


def test_an_interrupted_watch_ends_the_watcher(project_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # watchfiles returns quietly on Ctrl+C when raise_interrupt is off; the watcher must not start watching again.
    def interrupted_watch(*paths: Path, **options: object) -> Iterator[set[tuple[object, str]]]:
        yield from ()

    monkeypatch.setattr(watcher_module, "watch", interrupted_watch)
    catalog = Catalog(open_project(project_root))
    catalog.scan()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=CatalogWatcher(catalog, on_event=lambda event: None).run,
        args=(stop_event,),
        daemon=True,
    )

    thread.start()
    thread.join(timeout=EVENT_TIMEOUT_SECONDS)

    assert not thread.is_alive()
    assert not stop_event.is_set()
