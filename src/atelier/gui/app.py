"""Starting the desktop app: the Qt application, the window, and a dev path that renders one screenshot.

The window is the `ApplicationWindow` in `qml/Main.qml`, with the `Studio` view-model as the `studio` context
property and the launch flags as `launchOptions`.

`--screenshot` opens the project, selects what the flags name, waits until the stage has settled (the model loaded
and measured, or the image shown), grabs the window once and quits. Qt's `offscreen` platform has no 3D graphics
API, so it runs on the normal platform. By default the window is never shown: `QQuickWindow.grabWindow()` renders a
hidden window offscreen, which is right for the 3D view and the panes, but a hidden window renders no frames, so
images inside views (the gallery, the references) mostly never arrive, and only its first grab is reliable.
`--screenshot-shown` shows the window instead, for a run launched where it cannot be seen, such as a silent Hyprland
special workspace; that screenshot is exactly what the user would see. `--revision`, `--compare`, `--compare-base`
and `--compare-view` stage a revision (by number) or compare once the source's history has arrived. `--tab chat`
waits for the source's chat, and `--markup` opens the markup editor on the render `--render` names, with the strokes
and pins of a `markup.json` (`--markup-file`).

The window is a client of the daemon (`atelierd`) on `--socket` (default `$XDG_RUNTIME_DIR/atelier.sock`). When none
answers, it says so and offers to start one; a screenshot run fails instead.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtCore import QObject, QTimer, QUrl, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle

from atelier.gui.backend import RpcBackend
from atelier.gui.studio import DaemonState, Studio
from atelier.gui.tasks import TaskRunner
from atelierd.daemon.system import default_socket_path
from atelierd.workers import Markup

logger = logging.getLogger(__name__)

_MAIN_QML = Path(__file__).parent / "qml" / "Main.qml"
_ORGANIZATION_NAME = "JalapenoLabs"
_APPLICATION_NAME = "Atelier"
_DEFAULT_WINDOW_SIZE = (1600, 1000)

# How long a screenshot waits after the stage settles, for images to decode and a frame to lay out.
_SCREENSHOT_SETTLE_MS = 3000
# A screenshot that has not settled by then fails rather than hangs.
_SCREENSHOT_TIMEOUT_MS = 180_000


class GuiOptions(BaseModel):
    """How the app was started."""

    model_config = ConfigDict(frozen=True)

    project: str | None = Field(default=None, description="A project to open at start, by name or path.")
    socket_path: Path | None = Field(default=None, description="The daemon's socket, when not the default.")
    state_root: Path | None = Field(
        default=None,
        description="The state root a daemon this window starts keeps each project's state in (<state_root>/<name>/).",
    )
    screenshot: Path | None = Field(default=None, description="Render one frame to this PNG and quit.")
    is_screenshot_shown: bool = Field(default=False, description="Show the window while taking the screenshot.")
    source: str | None = Field(default=None, description="The source to select, project-relative.")
    export: str | None = Field(default=None, description="The export to show, by file name or stem.")
    render: str | None = Field(default=None, description="A render to show full size, by file name.")
    revision: int | None = Field(default=None, description="The revision to show on the stage, by number.")
    compare: str | None = Field(default=None, description="Compare as `renders` or `models`.")
    compare_base: int | None = Field(default=None, description="The revision compare shows beside, by number.")
    compare_view: str | None = Field(default=None, description="The standard view compare shows.")
    is_markup: bool = Field(default=False, description="Open the markup editor on the render shown.")
    markup_file: Path | None = Field(default=None, description="A markup.json to put on the editor's capture.")
    view_options: dict[str, object] = Field(
        default_factory=dict,
        description="Viewport and pane settings applied at start (lighting, wireframe, collision, tab).",
    )
    window_size: tuple[int, int] = _DEFAULT_WINDOW_SIZE


def run_gui(options: GuiOptions) -> int:
    """Run the app until its window closes, or until the screenshot is written. Returns the exit code."""
    QQuickStyle.setStyle("Fusion")
    application = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    if not isinstance(application, QGuiApplication):
        raise RuntimeError("A non-GUI Qt application already exists in this process")
    application.setOrganizationName(_ORGANIZATION_NAME)
    application.setApplicationName(_APPLICATION_NAME)

    backend = RpcBackend(options.socket_path or default_socket_path(), state_root=options.state_root)
    studio = Studio(backend, TaskRunner())

    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("studio", studio)
    window_width, window_height = options.window_size
    launch_options = {
        **options.view_options,
        "project": options.project or "",
        "windowWidth": window_width,
        "windowHeight": window_height,
    }
    engine.rootContext().setContextProperty("launchOptions", launch_options)
    engine.load(QUrl.fromLocalFile(str(_MAIN_QML)))
    windows = engine.rootObjects()
    if not windows or not isinstance(windows[0], QQuickWindow):
        logger.error(f"[App] {_MAIN_QML.name} did not load as a window; see the QML errors above")
        return 1
    window = windows[0]

    studio.projectChanged.connect(lambda: window.setTitle(f"{studio.projectName} - {_APPLICATION_NAME}"))
    studio.connectDaemon()

    driver: _ScreenshotDriver | None = None
    if options.screenshot:
        driver = _ScreenshotDriver(window, studio, options)
    if not options.screenshot or options.is_screenshot_shown:
        window.show()

    if options.project:
        studio.openProject(options.project)
    elif options.screenshot:
        logger.error("[App] --screenshot needs --project")
        return 2

    exit_code = application.exec()
    studio.close()
    if driver and not driver.is_written:
        return 1
    return exit_code


class _ScreenshotDriver(QObject):
    """Selects what the options name once the project is open, then grabs the window once the stage settles."""

    def __init__(self, window: QQuickWindow, studio: Studio, options: GuiOptions) -> None:
        super().__init__()
        self._window = window
        self._studio = studio
        self._options = options
        self._is_selected = False
        self._is_grab_scheduled = False
        self._needs_history = any(
            option is not None for option in (options.revision, options.compare, options.compare_base)
        )
        self._needs_chat = options.view_options.get("tab") == "chat"
        self.is_written = False

        studio.projectChanged.connect(self._on_project)
        studio.stageReadyChanged.connect(self._on_stage_ready_changed)
        studio.historyChanged.connect(self._on_history)
        studio.chat.loaded.connect(self._on_chat_loaded)
        studio.daemonChanged.connect(self._on_daemon_changed)
        QTimer.singleShot(_SCREENSHOT_TIMEOUT_MS, self._give_up)

    @Slot()
    def _on_daemon_changed(self) -> None:
        if self._studio.property("daemonState") == DaemonState.MISSING:
            note = self._studio.property("daemonNote")
            logger.error(f"[Screenshot] No daemon: {note}. Start one with `atelierd run`.")
            QGuiApplication.exit(1)

    @Slot()
    def _on_chat_loaded(self) -> None:
        self._needs_chat = False
        self._on_stage_ready_changed()

    @Slot()
    def _on_project(self) -> None:
        if self._is_selected:
            return
        self._is_selected = True
        if not self._options.source:
            self._schedule_grab()
            return

        self._studio.selectSource(self._options.source)
        if not self._studio.selectedSource:
            logger.error(f"[Screenshot] No source '{self._options.source}' in {self._studio.projectName}")
            QGuiApplication.exit(1)
            return

        if self._options.export:
            wanted_export = self._options.export
            matches = [
                path
                for path in self._studio.export_paths
                if wanted_export in (PurePosixPath(path).name, PurePosixPath(path).stem)
            ]
            if not matches:
                logger.error(f"[Screenshot] {self._options.source} has no export '{self._options.export}'")
                QGuiApplication.exit(1)
                return
            self._studio.selectExport(matches[0])

        if self._options.render:
            matches = [path for path in self._studio.render_paths if PurePosixPath(path).name == self._options.render]
            if not matches:
                logger.error(f"[Screenshot] {self._options.source} has no render '{self._options.render}'")
                QGuiApplication.exit(1)
                return
            self._studio.showRender(matches[0])
            if self._options.is_markup:
                self._open_markup(self._options.render)
        self._on_history()
        self._on_stage_ready_changed()

    def _open_markup(self, render_name: str) -> None:
        markup_editor = self._studio.markup
        markup_editor.openImage(self._studio.property("stageImage"), render_name)
        markup_file = self._options.markup_file
        if markup_file:
            markup_editor.open_markup(Markup.model_validate_json(markup_file.read_text(encoding="utf-8")))

    @Slot()
    def _on_history(self) -> None:
        """Stage the revision or compare the options name, once the selected source's history has arrived."""
        if not (self._is_selected and self._needs_history and self._studio.hasRevisions):
            return
        self._needs_history = False
        options = self._options
        try:
            if options.revision is not None:
                self._studio.selectRevision(self._studio.revision_id_by_number(options.revision))
            if options.compare_base is not None:
                self._studio.setCompareBase(self._studio.revision_id_by_number(options.compare_base))
            elif options.compare:
                self._studio.startCompare()
        except KeyError as error:
            logger.error(f"[Screenshot] {error}")
            QGuiApplication.exit(1)
            return
        if options.compare:
            self._studio.setCompareMode(options.compare)
        if options.compare_view:
            self._studio.setCompareView(options.compare_view)
        self._on_stage_ready_changed()

    @Slot()
    def _on_stage_ready_changed(self) -> None:
        is_waiting = self._needs_history or self._needs_chat
        if self._is_selected and not is_waiting and self._studio.isStageReady:
            self._schedule_grab()

    def _schedule_grab(self) -> None:
        if self._is_grab_scheduled:
            return
        self._is_grab_scheduled = True
        QTimer.singleShot(_SCREENSHOT_SETTLE_MS, self._grab)

    @Slot()
    def _grab(self) -> None:
        screenshot_path = self._options.screenshot
        if screenshot_path is None:
            raise RuntimeError("The screenshot driver runs only with a screenshot path")
        screenshot_path.parent.mkdir(parents=True, exist_ok=True)
        image = self._window.grabWindow()
        self.is_written = image.save(str(screenshot_path))
        if self.is_written:
            logger.info(f"[Screenshot] Wrote {screenshot_path}")
        else:
            logger.error(f"[Screenshot] Could not write {screenshot_path}")
        QGuiApplication.quit()

    @Slot()
    def _give_up(self) -> None:
        if self.is_written:
            return
        logger.error(f"[Screenshot] The stage did not settle within {_SCREENSHOT_TIMEOUT_MS / 1000:.0f} s")
        QGuiApplication.exit(1)
