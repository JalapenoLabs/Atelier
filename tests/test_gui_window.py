"""The QML window loads cleanly against a real daemon, and the command line starts it."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import RunningDaemon
from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QColor, QGuiApplication, QImage
from PySide6.QtQml import QQmlApplicationEngine, QQmlError

from atelier import cli
from atelier.gui.app import _MAIN_QML, GuiOptions
from atelier.gui.backend import RpcBackend
from atelier.gui.studio import DaemonState, Studio
from atelier.gui.tasks import TaskRunner

# The fixture project's images are empty files, so decoding them warns; that is about the data, not the QML.
_DATA_WARNING = "QQuickImage: Error decoding"


def test_every_qml_file_loads_without_errors(
    qt_application: QGuiApplication,
    project_root: Path,
    tmp_path: Path,
    daemon: RunningDaemon,
    wait_until: Callable[[Callable[[], bool]], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The window's Settings write here, never into the user's own configuration.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    qt_application.setOrganizationName("AtelierTests")
    qt_application.setApplicationName("AtelierTests")
    backend = RpcBackend(daemon.socket_path, state_root=tmp_path / "state")
    settings = QSettings(str(tmp_path / "config" / "Atelier.conf"), QSettings.Format.IniFormat)
    studio = Studio(backend, TaskRunner(), settings)
    engine = QQmlApplicationEngine()
    warnings: list[QQmlError] = []
    engine.warnings.connect(warnings.extend)
    engine.rootContext().setContextProperty("studio", studio)
    engine.rootContext().setContextProperty("launchOptions", {"windowWidth": 1200, "windowHeight": 800})

    engine.load(QUrl.fromLocalFile(str(_MAIN_QML)))
    try:
        assert engine.rootObjects(), [warning.toString() for warning in warnings]
        studio.connectDaemon()
        wait_until(lambda: studio.property("daemonState") == DaemonState.CONNECTED)
        studio.openProject(str(project_root))
        wait_until(lambda: studio.property("projectName") == "FixtureProject")
        studio.selectSource("SourceArt/Weapons/Carbine/Carbine.blend")
        # An open source, so the tree draws export rows too.
        studio.toggleTreeRow("SourceArt/Weapons/Carbine/Carbine.blend")
        assert any(row.kind == "export" for row in studio.property("sourceRows").rows)
        # A turn through the fake Claude Code, so the chat draws a prompt, the worker's text and the result.
        studio.chat.send("SAY Hello from the worker")
        wait_until(lambda: any(row.kind == "result" for row in studio.chat.property("rows").rows))
        # The markup editor, with a stroke and a pin, on a real image.
        render = tmp_path / "render.png"
        image = QImage(64, 64, QImage.Format.Format_RGB32)
        image.fill(QColor("#808080"))
        image.save(str(render))
        studio.markup.openImage(QUrl.fromLocalFile(str(render)).toString(), "render.png")
        studio.markup.addStroke("box", [[0.1, 0.1], [0.6, 0.6]])
        studio.markup.addPin(0.5, 0.5)
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
