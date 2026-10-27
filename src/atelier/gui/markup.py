"""Markup: drawing on a render or a frozen viewport frame, and sending it to the source's worker as feedback.

The editor holds the strokes (pen, arrow, box) and numbered pins in coordinates from 0 to 1 across the image, so the
same marks land in the same place at any size: QML's canvas draws them over the image on screen, and
`flatten_markup` draws them in the same style onto the image itself, at its full resolution, for the annotated PNG
the worker reads. Sending writes the clean capture and the annotated PNG to a temporary folder, has the daemon store
them as feedback (in `<state>/feedback/<source>/<id>/`), and sends the prompt that names the annotated image.
"""

from __future__ import annotations

import logging
import math
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from PySide6.QtCore import Property, QObject, QPointF, QRectF, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen

from atelier.gui.backend import StudioBackend
from atelier.gui.chat import RunWork
from atelier.gui.list_model import RowListModel
from atelierd.catalog import CameraBookmark
from atelierd.workers import Feedback, FeedbackRequest, Markup, Pin, Stroke, StrokeTool

logger = logging.getLogger(__name__)

# The stroke colours on offer: red, amber, cyan, green, each readable on a grey studio render.
MARKUP_COLORS = ("#ff4d4d", "#ffc233", "#33d1ff", "#5ce65c")
PIN_COLOR = "#ff4d4d"
# The ring and number on a pin, in the editor and on the flattened image alike.
PIN_INK_COLOR = "#ffffff"
# Sizes as fractions of the image's longer side, so marks look the same on a thumbnail and on a 4K frame.
_STROKE_WIDTH = 0.005
_ARROW_HEAD = 0.025
_PIN_RADIUS = 0.016
_ARROW_HEAD_ANGLE = math.radians(28)


class MarkupTarget(BaseModel):
    """What a capture shows: the source, and the revision and export on the stage when it was taken."""

    model_config = ConfigDict(frozen=True)

    source: str
    revision_id: int | None = None
    export: str | None = None


class PinRow(BaseModel):
    """One pin in the editor's list, with its note."""

    model_config = ConfigDict(frozen=True)

    number: int
    position_x: float
    position_y: float
    note: str


def flatten_markup(capture: QImage, markup: Markup) -> QImage:
    """Return the capture with every stroke and pin drawn onto it, at the capture's own resolution."""
    image = capture.convertToFormat(QImage.Format.Format_ARGB32)
    width, height = image.width(), image.height()
    size = max(width, height)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    def point(normalised: tuple[float, float]) -> QPointF:
        return QPointF(normalised[0] * width, normalised[1] * height)

    for stroke in markup.strokes:
        pen = QPen(QColor(stroke.color), _STROKE_WIDTH * size)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        points = [point(stroke_point) for stroke_point in stroke.points]
        if stroke.tool is StrokeTool.PEN and points:
            path = QPainterPath(points[0])
            for next_point in points[1:]:
                path.lineTo(next_point)
            painter.drawPath(path)
        elif stroke.tool is StrokeTool.BOX and len(points) >= 2:
            painter.drawRect(QRectF(points[0], points[-1]).normalized())
        elif stroke.tool is StrokeTool.ARROW and len(points) >= 2:
            start, end = points[0], points[-1]
            painter.drawLine(start, end)
            angle = math.atan2(end.y() - start.y(), end.x() - start.x())
            head = _ARROW_HEAD * size
            for side in (-1, 1):
                barb_angle = angle + math.pi + side * _ARROW_HEAD_ANGLE
                barb = QPointF(end.x() + head * math.cos(barb_angle), end.y() + head * math.sin(barb_angle))
                painter.drawLine(end, barb)

    radius = _PIN_RADIUS * size
    font = QFont()
    font.setBold(True)
    font.setPixelSize(max(8, round(radius * 1.2)))
    painter.setFont(font)
    for pin in markup.pins:
        centre = point((pin.x, pin.y))
        painter.setPen(QPen(QColor(PIN_INK_COLOR), radius * 0.18))
        painter.setBrush(QBrush(QColor(PIN_COLOR)))
        painter.drawEllipse(centre, radius, radius)
        painter.drawText(
            QRectF(centre.x() - radius, centre.y() - radius, radius * 2, radius * 2),
            Qt.AlignmentFlag.AlignCenter,
            str(pin.number),
        )
    painter.end()
    return image


class MarkupEditor(QObject):
    """The drawing layer over a render or a frozen frame, and the feedback it sends."""

    changed = Signal()
    # Feedback was stored and its prompt sent: the window switches to the chat.
    sent = Signal()

    def __init__(
        self,
        backend: StudioBackend,
        run: RunWork,
        show_message: Callable[[str], None],
        target: Callable[[], MarkupTarget | None],
    ) -> None:
        """Create the editor.

        Args:
            backend: the window's backend.
            run: runs backend work off the UI thread.
            show_message: shows a status message.
            target: what the stage shows now, read when a capture opens.
        """
        super().__init__()
        self._backend = backend
        self._run = run
        self._show_message = show_message
        self._read_target = target
        self._frames_dir: Path | None = None
        self._capture_path: Path | None = None
        self._label = ""
        self._target: MarkupTarget | None = None
        self._camera: CameraBookmark | None = None
        self._tool = StrokeTool.PEN.value
        self._color = MARKUP_COLORS[0]
        self._strokes: list[Stroke] = []
        self._pins: list[Pin] = []
        # What each undo removes, newest last: a stroke or a pin.
        self._history: list[str] = []
        self._is_sending = False
        self._pin_rows = RowListModel(PinRow)

    # ####################### #
    #       PROPERTIES        #
    # ####################### #

    def _get_is_open(self) -> bool:
        return self.is_open

    def _get_image_url(self) -> str:
        return QUrl.fromLocalFile(str(self._capture_path)).toString() if self._capture_path else ""

    def _get_label(self) -> str:
        return self._label

    def _get_tool(self) -> str:
        return self._tool

    def _get_color(self) -> str:
        return self._color

    def _get_colors(self) -> list[str]:
        return list(MARKUP_COLORS)

    def _get_pin_color(self) -> str:
        return PIN_COLOR

    def _get_pin_ink_color(self) -> str:
        return PIN_INK_COLOR

    def _get_strokes(self) -> list[dict[str, Any]]:
        return [
            {"tool": stroke.tool.value, "color": stroke.color, "points": [list(point) for point in stroke.points]}
            for stroke in self._strokes
        ]

    def _get_pin_rows(self) -> QObject:
        return self._pin_rows

    def _get_can_undo(self) -> bool:
        return bool(self._history)

    def _get_is_sending(self) -> bool:
        return self._is_sending

    isOpen = Property(bool, _get_is_open, notify=changed)
    imageUrl = Property(str, _get_image_url, notify=changed)
    label = Property(str, _get_label, notify=changed)
    tool = Property(str, _get_tool, notify=changed)
    color = Property(str, _get_color, notify=changed)
    colors = Property(list, _get_colors, constant=True)
    pinColor = Property(str, _get_pin_color, constant=True)
    pinInkColor = Property(str, _get_pin_ink_color, constant=True)
    strokes = Property(list, _get_strokes, notify=changed)
    pinRows = Property(QObject, _get_pin_rows, constant=True)
    canUndo = Property(bool, _get_can_undo, notify=changed)
    isSending = Property(bool, _get_is_sending, notify=changed)

    @property
    def is_open(self) -> bool:
        """Whether a capture is open for markup."""
        return self._capture_path is not None

    @property
    def markup(self) -> Markup:
        """Everything drawn so far."""
        return Markup(strokes=tuple(self._strokes), pins=tuple(self._pins))

    # ####################### #
    #     OPENING A CAPTURE   #
    # ####################### #

    @Slot(str, str)
    def openImage(self, image_url: str, label: str) -> None:
        """Mark up a render shown on the stage, by its URL."""
        path = Path(QUrl(image_url).toLocalFile())
        if not path.is_file():
            self._show_message(f"Cannot mark up {label}: its image is not on disk")
            return
        self._open(path, label, camera=None)

    @Slot(result=str)
    def newFramePath(self) -> str:
        """A fresh file for the viewport to save a frozen frame into."""
        if self._frames_dir is None:
            self._frames_dir = Path(tempfile.mkdtemp(prefix="atelier-frames-"))
        return str(self._frames_dir / f"{uuid.uuid4().hex}.png")

    @Slot(str, str, float, float, float, float, float, float)
    def openFrame(
        self,
        frame_path: str,
        label: str,
        target_x: float,
        target_y: float,
        target_z: float,
        yaw: float,
        pitch: float,
        distance: float,
    ) -> None:
        """Mark up a frozen viewport frame, remembering the camera it was seen from (Unreal's frame, cm)."""
        camera = CameraBookmark(
            name="frame",
            target_cm=(target_x, target_y, target_z),
            yaw_degrees=yaw,
            pitch_degrees=pitch,
            distance_cm=distance,
        )
        self._open(Path(frame_path), label, camera)

    def open_markup(self, markup: Markup) -> None:
        """Put earlier strokes and pins back on the open capture, as the screenshot path does."""
        self._strokes = list(markup.strokes)
        self._pins = list(markup.pins)
        self._history = ["stroke"] * len(self._strokes) + ["pin"] * len(self._pins)
        self._refresh_pins()
        self.changed.emit()

    def _open(self, capture_path: Path, label: str, camera: CameraBookmark | None) -> None:
        self._target = self._read_target()
        if self._target is None:
            self._show_message("Select a source to mark up")
            return
        self._capture_path = capture_path
        self._label = label
        self._camera = camera
        self._strokes = []
        self._pins = []
        self._history = []
        self._refresh_pins()
        self.changed.emit()

    # ####################### #
    #         DRAWING         #
    # ####################### #

    @Slot(str)
    def setTool(self, tool: str) -> None:
        """Draw with `pen`, `arrow` or `box`, or place `pin`s."""
        self._tool = tool
        self.changed.emit()

    @Slot(str)
    def setColor(self, color: str) -> None:
        """Draw the next strokes in this colour."""
        self._color = color
        self.changed.emit()

    @Slot(str, list)
    def addStroke(self, tool: str, points: list[Any]) -> None:
        """Add a finished stroke: a pen's path, or an arrow's or box's two corners, from 0 to 1 across the image."""
        stroke_points = tuple((float(point[0]), float(point[1])) for point in points)
        if len(stroke_points) < 2:
            logger.debug(f"[Markup] Ignoring a {tool} stroke with {len(stroke_points)} points")
            return
        self._strokes.append(Stroke(tool=StrokeTool(tool), color=self._color, points=stroke_points))
        self._history.append("stroke")
        self.changed.emit()

    @Slot(float, float)
    def addPin(self, x: float, y: float) -> None:
        """Place the next numbered pin."""
        number = max((pin.number for pin in self._pins), default=0) + 1
        self._pins.append(Pin(number=number, x=x, y=y))
        self._history.append("pin")
        self._refresh_pins()
        self.changed.emit()

    @Slot(int, str)
    def setPinNote(self, number: int, note: str) -> None:
        """Write a pin's note.

        The list is left alone: its text field already shows what was typed, and rebuilding the rows would take the
        focus from it.
        """
        self._pins = [pin.model_copy(update={"note": note}) if pin.number == number else pin for pin in self._pins]

    @Slot()
    def undo(self) -> None:
        """Take back the last stroke or pin."""
        if not self._history:
            return
        if self._history.pop() == "pin":
            self._pins.pop()
            self._refresh_pins()
        else:
            self._strokes.pop()
        self.changed.emit()

    @Slot()
    def clear(self) -> None:
        """Remove every stroke and pin."""
        self._strokes = []
        self._pins = []
        self._history = []
        self._refresh_pins()
        self.changed.emit()

    @Slot()
    def close(self) -> None:
        """Leave the editor without sending."""
        self._capture_path = None
        self._target = None
        self.changed.emit()

    # ####################### #
    #         SENDING         #
    # ####################### #

    @Slot(str)
    def send(self, prompt: str) -> None:
        """Store the capture and its markup as feedback, and send it to the source's worker with `prompt`."""
        capture_path, target = self._capture_path, self._target
        if capture_path is None or target is None:
            logger.debug("[Markup] send called with no capture open")
            return
        capture = QImage(str(capture_path))
        if capture.isNull():
            self._show_message(f"Could not read {capture_path.name}")
            return

        markup = self.markup
        folder = Path(tempfile.mkdtemp(prefix="atelier-markup-"))
        clean_path = folder / "capture.png"
        annotated_path = folder / "annotated.png"
        if not (capture.save(str(clean_path)) and flatten_markup(capture, markup).save(str(annotated_path))):
            shutil.rmtree(folder, ignore_errors=True)
            self._show_message("Could not write the marked-up image")
            return
        request = FeedbackRequest(
            source=target.source,
            capture_path=clean_path,
            annotated_path=annotated_path,
            markup=markup,
            revision_id=target.revision_id,
            export=target.export,
            camera=self._camera,
        )

        def store_and_send() -> Feedback:
            try:
                feedback = self._backend.create_feedback(request)
            finally:
                # The daemon copied both images into the feedback's own folder.
                shutil.rmtree(folder, ignore_errors=True)
            self._backend.send_prompt(target.source, prompt, feedback.id)
            return feedback

        def on_failed(error: Exception) -> None:
            self._is_sending = False
            self.changed.emit()

        self._is_sending = True
        self.changed.emit()
        self._run(store_and_send, self._on_sent, on_failed)

    def _on_sent(self, feedback: Feedback) -> None:
        self._is_sending = False
        self._show_message(f"Sent feedback {feedback.id} with {len(feedback.pins)} pins")
        self.close()
        self.sent.emit()

    def _refresh_pins(self) -> None:
        rows = [PinRow(number=pin.number, position_x=pin.x, position_y=pin.y, note=pin.note) for pin in self._pins]
        self._pin_rows.set_rows(rows)
