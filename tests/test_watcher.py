"""The debounced file watcher keeping the catalog live, against real filesystem events."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from atelierd.catalog import Catalog, CatalogEvent, CatalogWatcher
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
