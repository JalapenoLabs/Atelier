"""The theme file: parsing and fallbacks, live reload (edits, saves by rename, removal), and QML seeing it."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

import pytest
from conftest import WaitUntil
from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQml import QQmlComponent, QQmlEngine

from atelier.gui.app import _MAIN_QML
from atelier.gui.theme import ThemeError, ThemeFile, ThemeMode, ThemeTokens, default_theme_path, read_theme

_QML_DIR = _MAIN_QML.parent
# A colour written into QML: a hex literal, or one built with Qt.rgba / Qt.hsla / Qt.hsva.
_COLOR_LITERAL = re.compile(r'"#[0-9A-Fa-f]{3,8}"|Qt\.(rgba|hsla|hsva)\(')


def write_theme(path: Path, theme: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(theme), encoding="utf-8")


class TestReadTheme:
    def test_reads_every_token_by_its_camel_case_key(self, tmp_path: Path) -> None:
        theme_path = tmp_path / "theme.json"
        write_theme(
            theme_path, {"mode": "light", "window": "#FAFAFA", "panelRaised": "#eeeeee", "viewportAxisX": "#c00000"}
        )

        tokens = read_theme(theme_path)

        assert tokens.mode == ThemeMode.LIGHT
        assert tokens.window == "#FAFAFA"
        assert tokens.panel_raised == "#eeeeee"
        assert tokens.viewport_axis_x == "#c00000"

    def test_a_missing_key_keeps_the_built_in_value(self, tmp_path: Path) -> None:
        theme_path = tmp_path / "theme.json"
        write_theme(theme_path, {"accent": "#ff8800"})

        tokens = read_theme(theme_path)

        assert tokens.accent == "#ff8800"
        assert tokens.model_dump(exclude={"accent"}) == ThemeTokens().model_dump(exclude={"accent"})

    def test_an_unknown_key_is_ignored_with_a_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        theme_path = tmp_path / "theme.json"
        write_theme(theme_path, {"accent": "#ff8800", "sparkle": "#ffffff"})

        with caplog.at_level(logging.WARNING, logger="atelier.gui.theme"):
            tokens = read_theme(theme_path)

        assert tokens.accent == "#ff8800"
        assert "sparkle" in caplog.text

    @pytest.mark.parametrize(
        "content",
        [
            "{ not json",
            "[]",
            json.dumps({"window": "#1b1e24cc"}),
            json.dumps({"window": "red"}),
            json.dumps({"mode": "dim"}),
        ],
        ids=["not-json", "not-an-object", "eight-digit-hex", "colour-name", "unknown-mode"],
    )
    def test_an_invalid_file_is_refused(self, tmp_path: Path, content: str) -> None:
        theme_path = tmp_path / "theme.json"
        theme_path.write_text(content, encoding="utf-8")

        with pytest.raises(ThemeError):
            read_theme(theme_path)

    def test_the_default_path_follows_xdg_config_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert default_theme_path() == tmp_path / "atelier" / "theme.json"


class TestThemeFile:
    def test_no_file_shows_the_built_in_theme(self, qt_application: QGuiApplication, tmp_path: Path) -> None:
        theme = ThemeFile(tmp_path / "atelier" / "theme.json")

        assert theme.tokens == ThemeTokens()
        assert theme.property("colors")["window"] == QColor(ThemeTokens().window)

    def test_an_edit_in_place_shows_live(self, tmp_path: Path, wait_until: WaitUntil) -> None:
        theme_path = tmp_path / "theme.json"
        write_theme(theme_path, {"accent": "#111111"})
        theme = ThemeFile(theme_path)
        assert theme.tokens.accent == "#111111"

        write_theme(theme_path, {"accent": "#222222"})

        wait_until(lambda: theme.tokens.accent == "#222222")

    def test_a_save_by_rename_shows_live_and_later_edits_still_do(self, tmp_path: Path, wait_until: WaitUntil) -> None:
        theme_path = tmp_path / "theme.json"
        write_theme(theme_path, {"accent": "#111111"})
        theme = ThemeFile(theme_path)

        # What an atomic installer does: write a temporary file beside it, then move it over the theme.
        temporary_path = tmp_path / "theme.json.tmp"
        write_theme(temporary_path, {"accent": "#222222"})
        os.replace(temporary_path, theme_path)
        wait_until(lambda: theme.tokens.accent == "#222222")

        # The rename replaced the file the first watch followed; an edit in place after it must still be seen.
        write_theme(theme_path, {"accent": "#333333"})
        wait_until(lambda: theme.tokens.accent == "#333333")

    def test_an_invalid_save_keeps_the_current_theme(
        self, tmp_path: Path, wait_until: WaitUntil, caplog: pytest.LogCaptureFixture
    ) -> None:
        theme_path = tmp_path / "theme.json"
        write_theme(theme_path, {"accent": "#111111"})
        theme = ThemeFile(theme_path)

        with caplog.at_level(logging.ERROR, logger="atelier.gui.theme"):
            theme_path.write_text("{ half written", encoding="utf-8")
            wait_until(lambda: "Keeping the current theme" in caplog.text)

        assert theme.tokens.accent == "#111111"

    def test_removing_the_file_goes_back_to_the_built_in_theme(self, tmp_path: Path, wait_until: WaitUntil) -> None:
        theme_path = tmp_path / "theme.json"
        write_theme(theme_path, {"accent": "#111111"})
        theme = ThemeFile(theme_path)
        changes: list[None] = []
        theme.changed.connect(lambda: changes.append(None))

        theme_path.unlink()

        wait_until(lambda: theme.tokens == ThemeTokens())
        assert len(changes) == 1

    def test_a_file_in_a_folder_created_later_shows_live(self, tmp_path: Path, wait_until: WaitUntil) -> None:
        theme_path = tmp_path / "config" / "atelier" / "theme.json"
        theme = ThemeFile(theme_path)

        (tmp_path / "config").mkdir()
        wait_until(lambda: str(tmp_path / "config") in theme._watcher.directories())
        write_theme(theme_path, {"accent": "#444444"})

        wait_until(lambda: theme.tokens.accent == "#444444")


def test_qml_reads_the_theme_file_live(
    qt_application: QGuiApplication, tmp_path: Path, wait_until: WaitUntil, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    theme_path = tmp_path / "atelier" / "theme.json"
    write_theme(theme_path, {"window": "#102030", "statusApproved": "#00ff00"})
    engine = QQmlEngine()
    component = QQmlComponent(engine)
    # Loaded as if it were a file beside Theme.qml, so `Theme` resolves as it does in the window.
    component.setData(
        b"import QtQuick\n"
        b"QtObject {\n"
        b"    property color window: Theme.window\n"
        b"    property color approved: Theme.statusColor('approved')\n"
        b"    property color overlay: Theme.overlay\n"
        b"}\n",
        QUrl.fromLocalFile(str(_QML_DIR / "ThemeProbe.qml")),
    )
    probe = component.create()
    assert probe is not None, component.errorString()

    assert probe.property("window") == QColor("#102030")
    assert probe.property("approved") == QColor("#00ff00")
    overlay = probe.property("overlay")
    assert overlay.name() == ThemeTokens().field and overlay.alphaF() == pytest.approx(0.8, abs=0.01)

    write_theme(theme_path, {"window": "#405060"})

    wait_until(lambda: probe.property("window") == QColor("#405060"))
    assert probe.property("approved") == QColor(ThemeTokens().status_approved)


def test_no_qml_file_but_theme_qml_writes_a_colour() -> None:
    offenders = [
        f"{qml_path.name}:{line_number}: {line.strip()}"
        for qml_path in sorted(_QML_DIR.glob("*.qml"))
        if qml_path.name != "Theme.qml"
        for line_number, line in enumerate(qml_path.read_text(encoding="utf-8").splitlines(), start=1)
        if _COLOR_LITERAL.search(line)
    ]

    assert offenders == []
