"""Shared fixtures: a disposable copy of the fixture project for every test, and Qt for the GUI tests."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication

FIXTURE_PROJECT = Path(__file__).parent / "fixtures" / "project"


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """Copy the fixture project into a temporary folder, so tests can scan and change it freely.

    `__pycache__` is created here rather than committed, because the repository ignores those folders.
    """
    root = tmp_path / "FixtureProject"
    shutil.copytree(FIXTURE_PROJECT, root)
    cache_folder = root / "SourceArt" / "Weapons" / "Carbine" / "__pycache__"
    cache_folder.mkdir()
    (cache_folder / "Cached.blend").touch()
    return root


@pytest.fixture(scope="session")
def qt_application() -> QGuiApplication:
    """One Qt application for every GUI test, on the `offscreen` platform so no display is needed.

    `offscreen` has no 3D graphics API, so Qt Quick 3D renders nothing there; GUI tests cover the view-model and
    that the QML loads, not pixels.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    existing = QGuiApplication.instance()
    if isinstance(existing, QGuiApplication):
        return existing
    return QGuiApplication([])


WaitUntil = Callable[[Callable[[], bool]], None]


@pytest.fixture
def wait_until(qt_application: QGuiApplication) -> WaitUntil:
    """Return a function that processes Qt events until a condition holds, failing the test after 10 s."""

    def wait(condition: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 10
        while not condition():
            if time.monotonic() > deadline:
                pytest.fail(f"Timed out waiting for {condition}")
            QCoreApplication.processEvents()
            time.sleep(0.005)

    return wait
