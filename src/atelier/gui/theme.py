"""The window's colours: the theme file, the built-in theme it falls back to, and watching it for live changes.

A theme is a flat JSON object of semantic colour tokens plus `mode`, read from `$XDG_CONFIG_HOME/atelier/theme.json`
(see `docs/theming.md`). Any token the file leaves out keeps its built-in value, the dark theme the window ships
with, so a file may set as few tokens as it likes. A key Atelier does not know is ignored with a warning. A file that
is not a JSON object, or holds a value that is not valid, is refused with an error and the theme already showing
stays, so a half-written save never blanks the window. With no file at all, the built-in theme shows; deleting the
file while the window is open goes back to it.

`ThemeFile` is the QML singleton `Atelier.ThemeFile`, created by each QML engine that imports it. Only
`qml/Theme.qml` reads it; every other QML file reads `Theme`. It watches the file's folder as well as the file
itself, because a save by rename (a new file moved over the old one, as an atomic installer writes it) replaces the
file a plain file watch was following. Watching the folder also sees the file, or the folder, appear later.
"""

from __future__ import annotations

import json
import logging
import os
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from pydantic.alias_generators import to_camel
from PySide6.QtCore import Property, QFileSystemWatcher, QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQml import QmlElement, QmlSingleton

logger = logging.getLogger(__name__)

# The QML module `ThemeFile` is registered in: `import Atelier` in Theme.qml.
QML_IMPORT_NAME = "Atelier"
QML_IMPORT_MAJOR_VERSION = 1

# Only opaque `#rrggbb`: eight hex digits mean #rrggbbaa to CSS and most theme tools but #aarrggbb to Qt, so a
# translucent colour could never mean the same thing to every writer. Translucent shades are derived in Theme.qml.
HexColor = Annotated[str, StringConstraints(pattern=r"^#[0-9A-Fa-f]{6}$")]

# A writer that truncates before it writes, or writes a temporary file and renames it, causes a burst of events;
# reading once they settle avoids reading the file half written.
_RELOAD_DEBOUNCE_MS = 100


class ThemeError(ValueError):
    """A theme file that cannot be used: unreadable, not a JSON object, or holding an invalid value."""


class ThemeMode(StrEnum):
    """Whether the theme is dark or light, for the colours Qt derives itself (scroll bars, native dialogs)."""

    DARK = "dark"
    LIGHT = "light"


class ThemeTokens(BaseModel):
    """Every token a theme file can set, keyed by its camelCase name; the defaults are the built-in theme."""

    model_config = ConfigDict(frozen=True, alias_generator=to_camel, populate_by_name=True)

    mode: ThemeMode = ThemeMode.DARK

    # Backgrounds, from the window itself to the panes on it, raised surfaces (cards, buttons) and input fields.
    window: HexColor = "#1b1e24"
    panel: HexColor = "#20242c"
    panel_raised: HexColor = "#272b35"
    field: HexColor = "#15171c"
    # Foregrounds: text, secondary text, and text on the selection colour.
    text: HexColor = "#e4e7ee"
    text_dim: HexColor = "#9aa1b2"
    selection_text: HexColor = "#ffffff"
    accent: HexColor = "#6aa8ff"
    border: HexColor = "#30353f"
    selection: HexColor = "#2d4a73"

    # A source's review status.
    status_draft: HexColor = "#8a909c"
    status_in_review: HexColor = "#e0b341"
    status_changes_requested: HexColor = "#e0645a"
    status_approved: HexColor = "#5cc285"

    # Failures, things waiting on the user, and things that went well.
    danger: HexColor = "#e0645a"
    warning: HexColor = "#e0b341"
    success: HexColor = "#5cc285"

    # The 3D viewport (and the image stages): the backdrop, the ground grid, and its Unreal X and Y axes.
    viewport_background: HexColor = "#2a2e36"
    viewport_grid: HexColor = "#808080"
    viewport_axis_x: HexColor = "#ff0000"
    viewport_axis_y: HexColor = "#0000ff"


# The keys a theme file may hold, as written in the file.
_THEME_KEYS = frozenset(field.alias or name for name, field in ThemeTokens.model_fields.items())


def default_theme_path() -> Path:
    """The theme file: `$XDG_CONFIG_HOME/atelier/theme.json`, by default `~/.config/atelier/theme.json`."""
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "atelier" / "theme.json"


def read_theme(path: Path) -> ThemeTokens:
    """Read a theme file, taking every token it leaves out from the built-in theme.

    Raises:
        ThemeError: the file cannot be read, is not a JSON object, or holds a value that is not valid.
    """
    try:
        raw_theme = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ThemeError(f"{path} is not readable JSON: {error}") from error
    if not isinstance(raw_theme, dict):
        raise ThemeError(f"{path} must hold a JSON object of tokens, not a {type(raw_theme).__name__}")

    unknown_keys = sorted(set(raw_theme) - _THEME_KEYS)
    if unknown_keys:
        logger.warning(f"[Theme] {path}: ignoring unknown keys {', '.join(unknown_keys)}")

    try:
        return ThemeTokens.model_validate(raw_theme)
    except ValidationError as error:
        problems = "; ".join(f"{detail['loc'][0]}: {detail['msg']}" for detail in error.errors())
        raise ThemeError(f"{path} has invalid values ({problems})") from error


@QmlElement
@QmlSingleton
class ThemeFile(QObject):
    """The theme QML shows: the file's tokens over the built-in theme, reloaded whenever the file changes."""

    changed = Signal()

    def __init__(self, path: Path | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = path or default_theme_path()
        self._tokens = ThemeTokens()
        self._colors = self._qml_colors(self._tokens)

        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._schedule_reload)
        self._watcher.directoryChanged.connect(self._schedule_reload)
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(_RELOAD_DEBOUNCE_MS)
        self._reload_timer.timeout.connect(self.reload)

        self.reload()

    @property
    def tokens(self) -> ThemeTokens:
        """The theme showing now."""
        return self._tokens

    @Slot()
    def reload(self) -> None:
        """Read the file again and show it, or the built-in theme when there is no file; keep the theme on an error."""
        self._watch()
        if not self._path.is_file():
            if self._tokens != ThemeTokens():
                logger.info(f"[Theme] {self._path} is gone; showing the built-in theme")
            self._apply(ThemeTokens())
            return

        try:
            tokens = read_theme(self._path)
        except ThemeError as error:
            logger.error(f"[Theme] {error}. Keeping the current theme.")
            return
        if tokens != self._tokens:
            logger.info(f"[Theme] Showing {self._path}")
        self._apply(tokens)

    @Slot()
    def _schedule_reload(self) -> None:
        self._reload_timer.start()

    def _watch(self) -> None:
        """Watch the file, when it exists, and the nearest folder on its path that exists; nothing else.

        The file is watched afresh on every reload: after a save by rename the path names a new file, while the old
        watch still follows the replaced one, so the file watch is moved to the new file.
        """
        path_name = str(self._path)
        if path_name in self._watcher.files():
            self._watcher.removePath(path_name)
        if self._path.is_file():
            self._watcher.addPath(path_name)

        # The root always exists, so there is always a folder to watch for the theme's folder to appear in.
        folder = next(parent for parent in self._path.parents if parent.is_dir())
        stale_folders = [watched for watched in self._watcher.directories() if watched != str(folder)]
        if stale_folders:
            self._watcher.removePaths(stale_folders)
        if str(folder) not in self._watcher.directories() and not self._watcher.addPath(str(folder)):
            logger.warning(f"[Theme] Cannot watch {folder}; changes to {self._path} need a restart to show")

    def _apply(self, tokens: ThemeTokens) -> None:
        if tokens == self._tokens:
            return
        self._tokens = tokens
        self._colors = self._qml_colors(tokens)
        # Qt derives some colours itself (scroll bars, native dialogs); have it derive them for the right mode.
        color_scheme = Qt.ColorScheme.Light if tokens.mode == ThemeMode.LIGHT else Qt.ColorScheme.Dark
        QGuiApplication.styleHints().setColorScheme(color_scheme)
        self.changed.emit()

    @staticmethod
    def _qml_colors(tokens: ThemeTokens) -> dict[str, QColor]:
        """Every colour token as a `QColor`, keyed as in the file and in Theme.qml."""
        token_values = tokens.model_dump(by_alias=True, exclude={"mode"})
        return {key: QColor(value) for key, value in token_values.items()}

    def _get_colors(self) -> dict[str, QColor]:
        return self._colors

    def _get_mode(self) -> str:
        return self._tokens.mode.value

    def _get_path(self) -> str:
        return str(self._path)

    colors = Property(dict, _get_colors, notify=changed)
    mode = Property(str, _get_mode, notify=changed)
    path = Property(str, _get_path, constant=True)
