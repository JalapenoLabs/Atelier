"""The QML window loads cleanly against a real catalog, and the command line starts it."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine, QQmlError

from atelier import cli
from atelier.gui.app import _MAIN_QML, GuiOptions
from atelier.gui.backend import InProcessBackend
from atelier.gui.studio import Studio
from atelier.gui.tasks import TaskRunner

# The fixture project's images are empty files, so decoding them warns; that is about the data, not the QML.
_DATA_WARNING = "QQuickImage: Error decoding"


def test_every_qml_file_loads_without_errors(
    qt_application: QGuiApplication,
    project_root: Path,
    tmp_path: Path,
    wait_until: Callable[[Callable[[], bool]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The window's Settings write here, never into the user's own configuration.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    qt_application.setOrganizationName("AtelierTests")
    qt_application.setApplicationName("AtelierTests")
    backend = InProcessBackend(tmp_path / "registry.toml", state_root=tmp_path / "state")
    studio = Studio(backend, TaskRunner())
    engine = QQmlApplicationEngine()
    warnings: list[QQmlError] = []
    engine.warnings.connect(warnings.extend)
    engine.rootContext().setContextProperty("studio", studio)
    engine.rootContext().setContextProperty("launchOptions", {"windowWidth": 1200, "windowHeight": 800})

    engine.load(QUrl.fromLocalFile(str(_MAIN_QML)))
    try:
        assert engine.rootObjects(), [warning.toString() for warning in warnings]
        studio.openProject(str(project_root))
        wait_until(lambda: studio.property("projectName") == "FixtureProject")
        studio.selectSource("SourceArt/Weapons/Carbine/Carbine.blend")
        qt_application.processEvents()
    finally:
        studio.close()

    assert [warning.toString() for warning in warnings if _DATA_WARNING not in warning.toString()] == []


class TestCommandLine:
    def test_no_command_starts_the_app(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        started: list[GuiOptions] = []

        def fake_run_gui(options: GuiOptions) -> int:
            started.append(options)
            return 0

        monkeypatch.setattr("atelier.gui.app.run_gui", fake_run_gui)

        exit_code = cli.main(["--project", "AnchorFall", "--state-root", str(tmp_path), "--lighting", "clay"])

        assert exit_code == 0
        assert started[0].project == "AnchorFall"
        assert started[0].state_root == tmp_path
        assert started[0].view_options == {"lighting": "clay"}

    def test_scan_still_needs_no_qt(self, project_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_DATA_HOME", str(project_root.parent / "data"))
        assert cli.main(["scan", str(project_root)]) == 0
