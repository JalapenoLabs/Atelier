"""Markup: drawing on an image, for feedback to a source's worker and for the drawings a review decision carries.

`Drawing` is the drawing layer both use: strokes (pen, highlighter, eraser, arrow, box) in a few colours and sizes,
numbered pins, and undo and redo. Marks are kept in coordinates from 0 to 1 across the image, so the same marks land
in the same place at any size: QML's canvas draws them over the image on screen, and `flatten_markup` draws them in
the same style onto the image itself, at its full resolution.

Two editors build on it:

- `MarkupEditor` marks up a render or a frozen viewport frame and sends it to the source's worker as feedback: it
  writes the clean capture and the annotated PNG to a temporary folder, has the daemon store them as feedback (in
  `<state>/feedback/<source>/<id>/`), and sends the prompt that names the annotated image.
- `DecisionDrawing` draws on a delivery's image, a frozen frame, or a blank canvas in review focus mode, and saves the
  flattened PNG wherever focus mode keeps the ticket's drawings.
"""

from __future__ import annotations

import logging
import math
import shutil
import tempfile
import uuid
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from PySide6.QtCore import Property, QObject, QPointF, QRectF, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPainterPath, QPen

from atelier.gui.backend import StudioBackend
from atelier.gui.chat import RunWork
from atelier.gui.list_model import RowListModel
from atelierd.catalog import CameraBookmark
from atelierd.workers import DEFAULT_STROKE_WIDTH, Feedback, FeedbackRequest, Markup, Pin, Stroke, StrokeTool

logger = logging.getLogger(__name__)

# The stroke colours on offer: red, amber, cyan and green read on a grey studio render, ink and white on the rest.
MARKUP_COLORS = ("#ff4d4d", "#ffc233", "#33d1ff", "#5ce65c", "#1b1b1b", "#ffffff")
# What a sketch on a blank canvas starts in.
INK_COLOR = MARKUP_COLORS[4]
PIN_COLOR = "#ff4d4d"
# The ring and number on a pin, in the editor and on the flattened image alike.
PIN_INK_COLOR = "#ffffff"
# A highlighter's ink, laid over the image at this opacity. One stroke is drawn as one path, so it never darkens where
# it crosses itself.
HIGHLIGHTER_OPACITY = 0.35
# A blank canvas: a white sheet of a common screen's shape, large enough to sketch on in detail.
BLANK_CANVAS_SIZE = (1920, 1080)
BLANK_CANVAS_COLOR = "#ffffff"

_ARROW_HEAD = 0.025
_PIN_RADIUS = 0.016
_ARROW_HEAD_ANGLE = math.radians(28)


class DrawingTool(StrEnum):
    """What a press on the image does: draw a stroke of one kind, or place a pin."""

    PEN = "pen"
    HIGHLIGHTER = "highlighter"
    ERASER = "eraser"
    ARROW = "arrow"
    BOX = "box"
    PIN = "pin"


class StrokeSize(StrEnum):
    """The line sizes on offer."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


# A pen's width at each size, as a fraction of the image's longer side. Medium is the width of a stroke saved without
# one.
_PEN_WIDTHS = {
    StrokeSize.SMALL: DEFAULT_STROKE_WIDTH / 2,
    StrokeSize.MEDIUM: DEFAULT_STROKE_WIDTH,
    StrokeSize.LARGE: DEFAULT_STROKE_WIDTH * 2,
}
if set(_PEN_WIDTHS) != set(StrokeSize):
    raise RuntimeError("Every stroke size needs a pen width")

# How much wider than a pen each tool draws: a highlighter covers a line of text, an eraser rubs out a pen line easily.
_WIDTH_FACTORS = {
    StrokeTool.PEN: 1.0,
    StrokeTool.HIGHLIGHTER: 4.0,
    StrokeTool.ERASER: 4.0,
    StrokeTool.ARROW: 1.0,
    StrokeTool.BOX: 1.0,
}
if set(_WIDTH_FACTORS) != set(StrokeTool):
    raise RuntimeError("Every stroke tool needs a width factor")

# Tools whose stroke is the whole path dragged; the others keep only where the drag started and ended.
_PATH_TOOLS = frozenset({StrokeTool.PEN, StrokeTool.HIGHLIGHTER, StrokeTool.ERASER})


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


class _EditKind(StrEnum):
    STROKE = "stroke"
    PIN = "pin"
    CLEAR = "clear"


class _Edit(BaseModel):
    """One undoable change to a drawing: a stroke or pin added, or everything cleared, with what it touched."""

    model_config = ConfigDict(frozen=True)

    kind: _EditKind
    strokes: tuple[Stroke, ...] = ()
    pins: tuple[Pin, ...] = ()


def flatten_markup(capture: QImage, markup: Markup) -> QImage:
    """Return the capture with every stroke and pin drawn onto it, at the capture's own resolution.

    Strokes are painted onto a transparent layer first, where an eraser clears what is under it, and the layer is then
    laid over the capture: an eraser rubs out marks, never the image itself. Pins go on top, as the editor shows them.
    """
    image = capture.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    width, height = image.width(), image.height()
    size = max(width, height)

    def point(normalised: tuple[float, float]) -> QPointF:
        return QPointF(normalised[0] * width, normalised[1] * height)

    layer = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    layer.fill(Qt.GlobalColor.transparent)
    layer_painter = QPainter(layer)
    layer_painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    for stroke in markup.strokes:
        _paint_stroke(layer_painter, stroke, [point(stroke_point) for stroke_point in stroke.points], size)
    layer_painter.end()

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.drawImage(0, 0, layer)

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
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def _paint_stroke(painter: QPainter, stroke: Stroke, points: list[QPointF], size: int) -> None:
    """Paint one stroke onto the marks' layer, in the editor canvas's style."""
    color = QColor(stroke.color)
    if stroke.tool is StrokeTool.HIGHLIGHTER:
        color.setAlphaF(HIGHLIGHTER_OPACITY)
    pen = QPen(color, stroke.width * size)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    composition = (
        QPainter.CompositionMode.CompositionMode_Clear
        if stroke.tool is StrokeTool.ERASER
        else QPainter.CompositionMode.CompositionMode_SourceOver
    )
    painter.setCompositionMode(composition)

    if stroke.tool in _PATH_TOOLS and points:
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
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)


# ========================================================================================
# The drawing layer
# ========================================================================================


class Drawing(QObject):
    """The marks on one open image: strokes and pins, the tool, colour and size of the next, and undo and redo."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._image_path: Path | None = None
        self._label = ""
        self._tool = DrawingTool.PEN
        self._color = MARKUP_COLORS[0]
        self._size = StrokeSize.MEDIUM
        self._strokes: list[Stroke] = []
        self._pins: list[Pin] = []
        # Each undo takes back the newest edit and keeps it for redo; any new edit forgets what could be redone.
        self._undo_edits: list[_Edit] = []
        self._redo_edits: list[_Edit] = []
        self._pin_rows = RowListModel(PinRow)

    # ####################### #
    #       PROPERTIES        #
    # ####################### #

    def _get_is_open(self) -> bool:
        return self.is_open

    def _get_image_url(self) -> str:
        return QUrl.fromLocalFile(str(self._image_path)).toString() if self._image_path else ""

    def _get_label(self) -> str:
        return self._label

    def _get_tool(self) -> str:
        return self._tool.value

    def _get_color(self) -> str:
        return self._color

    def _get_colors(self) -> list[str]:
        return list(MARKUP_COLORS)

    def _get_size(self) -> str:
        return self._size.value

    def _get_sizes(self) -> list[str]:
        return [size.value for size in StrokeSize]

    def _get_stroke_width(self) -> float:
        return self._stroke_width(self._tool)

    def _get_highlighter_opacity(self) -> float:
        return HIGHLIGHTER_OPACITY

    def _get_pin_color(self) -> str:
        return PIN_COLOR

    def _get_pin_ink_color(self) -> str:
        return PIN_INK_COLOR

    def _get_strokes(self) -> list[dict[str, Any]]:
        return [
            {
                "tool": stroke.tool.value,
                "color": stroke.color,
                "width": stroke.width,
                "points": [list(point) for point in stroke.points],
            }
            for stroke in self._strokes
        ]

    def _get_pin_rows(self) -> QObject:
        return self._pin_rows

    def _get_can_undo(self) -> bool:
        return bool(self._undo_edits)

    def _get_can_redo(self) -> bool:
        return bool(self._redo_edits)

    def _get_has_marks(self) -> bool:
        return self.has_marks

    isOpen = Property(bool, _get_is_open, notify=changed)
    imageUrl = Property(str, _get_image_url, notify=changed)
    label = Property(str, _get_label, notify=changed)
    tool = Property(str, _get_tool, notify=changed)
    color = Property(str, _get_color, notify=changed)
    colors = Property(list, _get_colors, constant=True)
    size = Property(str, _get_size, notify=changed)
    sizes = Property(list, _get_sizes, constant=True)
    # The width the next stroke is drawn at, for the canvas to draw the stroke being dragged.
    strokeWidth = Property(float, _get_stroke_width, notify=changed)
    highlighterOpacity = Property(float, _get_highlighter_opacity, constant=True)
    pinColor = Property(str, _get_pin_color, constant=True)
    pinInkColor = Property(str, _get_pin_ink_color, constant=True)
    strokes = Property(list, _get_strokes, notify=changed)
    pinRows = Property(QObject, _get_pin_rows, constant=True)
    canUndo = Property(bool, _get_can_undo, notify=changed)
    canRedo = Property(bool, _get_can_redo, notify=changed)
    hasMarks = Property(bool, _get_has_marks, notify=changed)

    @property
    def is_open(self) -> bool:
        """Whether an image is open to draw on."""
        return self._image_path is not None

    @property
    def has_marks(self) -> bool:
        """Whether anything is drawn: a stroke or a pin."""
        return bool(self._strokes or self._pins)

    @property
    def markup(self) -> Markup:
        """Everything drawn so far."""
        return Markup(strokes=tuple(self._strokes), pins=tuple(self._pins))

    def open_markup(self, markup: Markup) -> None:
        """Put earlier strokes and pins back on the open image, each an edit undo can take back."""
        self._strokes = list(markup.strokes)
        self._pins = list(markup.pins)
        self._undo_edits = [_Edit(kind=_EditKind.STROKE, strokes=(stroke,)) for stroke in markup.strokes]
        self._undo_edits += [_Edit(kind=_EditKind.PIN, pins=(pin,)) for pin in markup.pins]
        self._redo_edits = []
        self._refresh_pins()
        self.changed.emit()

    def _open_image(self, image_path: Path, label: str) -> None:
        """Open an image with nothing drawn on it yet.

        Drawing starts with the pen again: an eraser left from the last image would draw nothing on a fresh one.
        """
        self._image_path = image_path
        self._label = label
        self._tool = DrawingTool.PEN
        self._reset_marks()

    def _reset_marks(self) -> None:
        self._strokes = []
        self._pins = []
        self._undo_edits = []
        self._redo_edits = []
        self._refresh_pins()
        self.changed.emit()

    # ####################### #
    #         DRAWING         #
    # ####################### #

    @Slot(str)
    def setTool(self, tool: str) -> None:
        """Draw with `pen`, `highlighter`, `eraser`, `arrow` or `box`, or place `pin`s."""
        self._tool = DrawingTool(tool)
        self.changed.emit()

    @Slot(str)
    def setColor(self, color: str) -> None:
        """Draw the next strokes in this colour."""
        self._color = color
        self.changed.emit()

    @Slot(str)
    def setSize(self, size: str) -> None:
        """Draw the next strokes `small`, `medium` or `large`."""
        self._size = StrokeSize(size)
        self.changed.emit()

    @Slot(str, list)
    def addStroke(self, tool: str, points: list[Any]) -> None:
        """Add a finished stroke: a path, or an arrow's or box's two corners, from 0 to 1 across the image."""
        stroke_points = tuple((float(point[0]), float(point[1])) for point in points)
        if len(stroke_points) < 2:
            logger.debug(f"[Markup] Ignoring a {tool} stroke with {len(stroke_points)} points")
            return
        stroke_tool = StrokeTool(tool)
        stroke = Stroke(
            tool=stroke_tool,
            color=self._color,
            points=stroke_points,
            width=self._stroke_width(stroke_tool),
        )
        self._strokes.append(stroke)
        self._record(_Edit(kind=_EditKind.STROKE, strokes=(stroke,)))
        self.changed.emit()

    @Slot(float, float)
    def addPin(self, x: float, y: float) -> None:
        """Place the next numbered pin."""
        number = max((pin.number for pin in self._pins), default=0) + 1
        pin = Pin(number=number, x=x, y=y)
        self._pins.append(pin)
        self._record(_Edit(kind=_EditKind.PIN, pins=(pin,)))
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
        """Take back the newest edit: a stroke, a pin, or a clear."""
        if not self._undo_edits:
            logger.debug("[Markup] Nothing to undo")
            return
        edit = self._undo_edits.pop()
        match edit.kind:
            case _EditKind.STROKE:
                self._strokes.pop()
            case _EditKind.PIN:
                # Kept with its note as it is now, so a redo brings the note back too.
                edit = edit.model_copy(update={"pins": (self._pins.pop(),)})
            case _EditKind.CLEAR:
                self._strokes = list(edit.strokes)
                self._pins = list(edit.pins)
        self._redo_edits.append(edit)
        self._refresh_pins()
        self.changed.emit()

    @Slot()
    def redo(self) -> None:
        """Make the newest undone edit again."""
        if not self._redo_edits:
            logger.debug("[Markup] Nothing to redo")
            return
        edit = self._redo_edits.pop()
        match edit.kind:
            case _EditKind.STROKE:
                self._strokes.extend(edit.strokes)
            case _EditKind.PIN:
                self._pins.extend(edit.pins)
            case _EditKind.CLEAR:
                self._strokes = []
                self._pins = []
        self._undo_edits.append(edit)
        self._refresh_pins()
        self.changed.emit()

    @Slot()
    def clear(self) -> None:
        """Remove every stroke and pin, as one edit undo can take back."""
        if not self.has_marks:
            logger.debug("[Markup] Nothing to clear")
            return
        self._record(_Edit(kind=_EditKind.CLEAR, strokes=tuple(self._strokes), pins=tuple(self._pins)))
        self._strokes = []
        self._pins = []
        self._refresh_pins()
        self.changed.emit()

    @Slot()
    def close(self) -> None:
        """Put the image away, and what was drawn on it."""
        self._image_path = None
        self._reset_marks()

    def _record(self, edit: _Edit) -> None:
        self._undo_edits.append(edit)
        self._redo_edits = []

    def _stroke_width(self, tool: DrawingTool | StrokeTool) -> float:
        if tool == DrawingTool.PIN:
            return _PEN_WIDTHS[self._size]
        return _PEN_WIDTHS[self._size] * _WIDTH_FACTORS[StrokeTool(tool)]

    def _refresh_pins(self) -> None:
        rows = [PinRow(number=pin.number, position_x=pin.x, position_y=pin.y, note=pin.note) for pin in self._pins]
        self._pin_rows.set_rows(rows)


# ========================================================================================
# Feedback to a worker
# ========================================================================================


class MarkupEditor(Drawing):
    """The drawing layer over a render or a frozen frame, and the feedback it sends to the source's worker."""

    # Feedback was stored and its prompt sent: the window switches to the chat.
    sent = Signal()
    # A PySide property's notify signal must be the class's own; an inherited one breaks QML's view of the class.
    sendingChanged = Signal()

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
        self._target: MarkupTarget | None = None
        self._camera: CameraBookmark | None = None
        self._is_sending = False

    def _get_is_sending(self) -> bool:
        return self._is_sending

    isSending = Property(bool, _get_is_sending, notify=sendingChanged)

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
        self._open_capture(path, label, camera=None)

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
        self._open_capture(Path(frame_path), label, camera)

    def _open_capture(self, capture_path: Path, label: str, camera: CameraBookmark | None) -> None:
        self._target = self._read_target()
        if self._target is None:
            self._show_message("Select a source to mark up")
            return
        self._camera = camera
        self._open_image(capture_path, label)

    @Slot()
    def close(self) -> None:
        """Leave the editor without sending."""
        self._target = None
        super().close()

    # ####################### #
    #         SENDING         #
    # ####################### #

    @Slot(str)
    def send(self, prompt: str) -> None:
        """Store the capture and its markup as feedback, and send it to the source's worker with `prompt`."""
        capture_path, target = self._image_path, self._target
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
            self._set_sending(False)

        self._set_sending(True)
        self._run(store_and_send, self._on_sent, on_failed)

    def _set_sending(self, is_sending: bool) -> None:
        self._is_sending = is_sending
        self.sendingChanged.emit()

    def _on_sent(self, feedback: Feedback) -> None:
        self._set_sending(False)
        self._show_message(f"Sent feedback {feedback.id} with {len(feedback.pins)} pins")
        self.close()
        self.sent.emit()


# ========================================================================================
# Drawings for a review decision
# ========================================================================================


class DecisionDrawing(Drawing):
    """A drawing for a review decision: on a delivery's image, a frozen viewport frame, or a blank canvas.

    It never sends anything: `save` flattens it into a PNG where focus mode keeps the ticket's drawings, and the
    decision carries the file.
    """

    def __init__(self) -> None:
        super().__init__()
        self._scratch_dir: Path | None = None

    def open_image(self, image_path: Path, label: str) -> bool:
        """Draw on an image file; False, with nothing opened, when it is not on disk."""
        if not image_path.is_file():
            logger.warning(f"[Markup] Cannot draw on {image_path}: it is not on disk")
            return False
        self._open_image(image_path, label)
        return True

    def open_blank(self) -> None:
        """Draw on a blank white canvas, in ink."""
        blank_path = self._scratch() / "blank-canvas.png"
        if not blank_path.is_file():
            canvas = QImage(*BLANK_CANVAS_SIZE, QImage.Format.Format_RGB32)
            canvas.fill(QColor(BLANK_CANVAS_COLOR))
            if not canvas.save(str(blank_path)):
                raise OSError(f"Could not write a blank canvas to {blank_path}")
        self._color = INK_COLOR
        self._open_image(blank_path, "Blank canvas")

    @Slot(result=str)
    def newFramePath(self) -> str:
        """A fresh file for the viewport to save a frozen frame into."""
        return str(self._scratch() / f"frame-{uuid.uuid4().hex}.png")

    def save(self, drawing_path: Path) -> bool:
        """Flatten what is drawn onto its image and write it as a PNG; False when there is nothing to save or no file.

        Saving does not close the drawing; the caller does once the file is where it belongs.
        """
        image_path = self._image_path
        if image_path is None or not self.has_marks:
            logger.debug("[Markup] Nothing drawn to save")
            return False
        image = QImage(str(image_path))
        if image.isNull():
            logger.warning(f"[Markup] Could not read {image_path} to save a drawing on it")
            return False
        drawing_path.parent.mkdir(parents=True, exist_ok=True)
        # Written under a name of its own and renamed into place, so a decision never picks up half a file.
        partial_path = drawing_path.with_name(f".{drawing_path.stem}.{uuid.uuid4().hex}.partial.png")
        if not flatten_markup(image, self.markup).save(str(partial_path)):
            partial_path.unlink(missing_ok=True)
            logger.warning(f"[Markup] Could not write the drawing to {drawing_path}")
            return False
        partial_path.replace(drawing_path)
        return True

    def _scratch(self) -> Path:
        """A temporary folder of this drawing's own, for blank canvases and frozen frames."""
        if self._scratch_dir is None:
            self._scratch_dir = Path(tempfile.mkdtemp(prefix="atelier-drawing-"))
        return self._scratch_dir
