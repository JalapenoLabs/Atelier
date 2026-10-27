"""The viewport's camera, navigated like Blender's: a turntable orbit around an invisible pivot.

The camera hangs off a pivot node in Qt's Y-up scene, `distance` cm out along the pivot's local +Z, turned by `yaw`
about the vertical and then by `pitch` about the camera's own horizontal axis. That is Qt Quick 3D's Euler order, so
QML gives the pivot node `eulerRotation: (pitch, yaw, 0)`. Yaw is counterclockwise seen from above, 0 with the camera
on the pivot's Unreal +Y side and 90 on its +X side (the camera bookmarks' convention); negative pitch looks down.

Turntable means no roll: yaw always turns about the world's vertical and pitch is held between straight down and
straight up, so the view never passes over a pole and flips. Like Blender with its default Auto Perspective, an axis
view (numpad 1, 3, 7) turns orthographic by itself, and orbiting off the axis turns it back to perspective.

Everything here is plain math on one QObject, so it is unit-tested without a graphics API. `ViewportNavigation.qml`
maps the mouse and keys onto these slots, and `Viewport.qml` binds its pivot node and cameras to the properties.
"""

from __future__ import annotations

import logging
import math
from enum import StrEnum

from PySide6.QtCore import Property, QObject, Signal, Slot
from PySide6.QtGui import QQuaternion, QVector3D
from PySide6.QtQml import QmlElement

logger = logging.getLogger(__name__)

QML_IMPORT_NAME = "Atelier"
QML_IMPORT_MAJOR_VERSION = 1

FIELD_OF_VIEW_DEGREES = 40.0
# The default three-quarter view, from the -X -Y side and slightly above; the hero standard render matches it.
THREE_QUARTER_YAW_DEGREES = 225.0
THREE_QUARTER_PITCH_DEGREES = -22.0
# Blender's defaults: 0.4 degrees of turntable orbit per pixel dragged, and 15 degree numpad steps.
ORBIT_DEGREES_PER_PIXEL = 0.4
ORBIT_STEP_DEGREES = 15.0
# One wheel notch (or numpad + or -) moves the camera this factor nearer the pivot or further from it.
DOLLY_FACTOR_PER_STEP = 1.2
# Framing leaves this much room around the bounding sphere.
FRAME_MARGIN = 1.1
# The camera never reaches the pivot: zooming only gets up to the point of interest, as in Blender.
_MIN_DISTANCE_CM = 1.0
_MAX_DISTANCE_CM = 1_000_000.0
_ANGLE_TOLERANCE_DEGREES = 1e-6


class AxisView(StrEnum):
    """The six views along the world's axes, named as Blender names its numpad views."""

    FRONT = "front"
    BACK = "back"
    RIGHT = "right"
    LEFT = "left"
    TOP = "top"
    BOTTOM = "bottom"


# (yaw, pitch) of each axis view. Front, right and top are exactly the `front`, `side` and `top` standard renders
# (docs/revisions.md): front from +Y, right from +X with +Y on the image's left, top from above with -X up the image.
ORIENTATION_BY_AXIS_VIEW: dict[AxisView, tuple[float, float]] = {
    AxisView.FRONT: (0.0, 0.0),
    AxisView.BACK: (180.0, 0.0),
    AxisView.RIGHT: (90.0, 0.0),
    AxisView.LEFT: (270.0, 0.0),
    AxisView.TOP: (90.0, -90.0),
    AxisView.BOTTOM: (90.0, 90.0),
}


@QmlElement
class ViewportCamera(QObject):
    """Where the viewport looks from: a pivot, a turntable orientation, a distance and a projection.

    Every property is read-only from QML and the slots are the only way to move the camera, so a move notifies once.
    Positions are in Qt's scene frame (Y up, centimetres).
    """

    changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pivot = QVector3D(0, 0, 0)
        self._yaw = THREE_QUARTER_YAW_DEGREES
        self._pitch = THREE_QUARTER_PITCH_DEGREES
        self._distance = 500.0
        self._is_orthographic = False
        # Set when an axis view turned orthographic by itself, so orbiting off the axis turns it back.
        self._is_orthographic_automatic = False

    # ------------------------------------------------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------------------------------------------------

    def _get_pivot(self) -> QVector3D:
        return QVector3D(self._pivot)

    def _get_yaw(self) -> float:
        return self._yaw

    def _get_pitch(self) -> float:
        return self._pitch

    def _get_distance(self) -> float:
        return self._distance

    def _get_is_orthographic(self) -> bool:
        return self._is_orthographic

    def _get_field_of_view(self) -> float:
        return FIELD_OF_VIEW_DEGREES

    def _get_view_height(self) -> float:
        """The height in cm the view shows at the pivot.

        Perspective sees this much at the pivot's depth, and the orthographic camera is scaled to show the same, so
        switching projection keeps the model the same size and a pan moves the pivot exactly with the mouse.
        """
        return 2 * self._distance * math.tan(math.radians(FIELD_OF_VIEW_DEGREES) / 2)

    def _get_view_label(self) -> str:
        """What the view is, as Blender's viewport names it: "Front orthographic", "User perspective"."""
        axis_view = self.axis_view()
        name = axis_view.value.capitalize() if axis_view else "User"
        projection = "orthographic" if self._is_orthographic else "perspective"
        return f"{name} {projection}"

    pivot = Property(QVector3D, _get_pivot, notify=changed)
    yaw = Property(float, _get_yaw, notify=changed)
    pitch = Property(float, _get_pitch, notify=changed)
    distance = Property(float, _get_distance, notify=changed)
    isOrthographic = Property(bool, _get_is_orthographic, notify=changed)
    fieldOfView = Property(float, _get_field_of_view, constant=True)
    viewHeight = Property(float, _get_view_height, notify=changed)
    viewLabel = Property(str, _get_view_label, notify=changed)

    # ------------------------------------------------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------------------------------------------------

    def rotation(self) -> QQuaternion:
        """The pivot's rotation: yaw about the vertical, then pitch about the camera's horizontal axis."""
        return QQuaternion.fromEulerAngles(self._pitch, self._yaw, 0)

    def position(self) -> QVector3D:
        """Where the perspective camera is in the scene."""
        offset = self.rotation().rotatedVector(QVector3D(0, 0, self._distance))
        return self._pivot + offset

    def axis_view(self) -> AxisView | None:
        """The axis view the camera looks along, if any.

        Straight down is the top view and straight up the bottom whatever the yaw, as in Blender; level views count
        when the yaw is exactly an axis.
        """
        for axis_view, (yaw, pitch) in ORIENTATION_BY_AXIS_VIEW.items():
            is_pitch_matching = math.isclose(self._pitch, pitch, abs_tol=_ANGLE_TOLERANCE_DEGREES)
            is_pole = abs(pitch) == 90
            # Measured round the circle, so 359.9999999 is still the front.
            yaw_difference = abs((self._yaw - yaw + 180) % 360 - 180)
            is_yaw_matching = yaw_difference <= _ANGLE_TOLERANCE_DEGREES
            if is_pitch_matching and (is_pole or is_yaw_matching):
                return axis_view
        return None

    # ------------------------------------------------------------------------------------------------------------
    # Moves
    # ------------------------------------------------------------------------------------------------------------

    @Slot(QVector3D, float, float, float)
    def place(self, pivot: QVector3D, yaw: float, pitch: float, distance: float) -> None:
        """Put the camera exactly here, as a bookmark does. A chosen projection is kept; an automatic one may not be."""
        self._pivot = QVector3D(pivot)
        self._distance = min(max(distance, _MIN_DISTANCE_CM), _MAX_DISTANCE_CM)
        self._turn_to(yaw, pitch)

    @Slot(float, float)
    def orbit(self, horizontal_pixels: float, vertical_pixels: float) -> None:
        """Orbit the pivot for a mouse drag, turning the scene with the mouse as Blender does.

        Dragging right turns the model to the right (the camera goes left, so yaw falls), and dragging up tilts it
        up (the camera goes down, so pitch rises; screen Y grows downwards).
        """
        self._turn_to(
            self._yaw - horizontal_pixels * ORBIT_DEGREES_PER_PIXEL,
            self._pitch - vertical_pixels * ORBIT_DEGREES_PER_PIXEL,
        )

    @Slot(int, int)
    def stepOrbit(self, right_steps: int, up_steps: int) -> None:
        """Move the camera round the pivot in numpad steps: 6 and 4 go right and left, 8 and 2 up and down.

        Blender's numpad steps name where the camera goes, the opposite of a drag, which names where the scene goes.
        """
        self._turn_to(
            self._yaw + right_steps * ORBIT_STEP_DEGREES,
            self._pitch - up_steps * ORBIT_STEP_DEGREES,
        )

    @Slot()
    def turnAround(self) -> None:
        """Look from the opposite side, turning 180 degrees about the vertical (numpad 9)."""
        self._turn_to(self._yaw + 180, self._pitch)

    @Slot(float, float, float)
    def pan(self, horizontal_pixels: float, vertical_pixels: float, view_height_pixels: float) -> None:
        """Slide the pivot in the view plane so the point under the mouse at the pivot's depth follows it."""
        if view_height_pixels <= 0:
            logger.debug(f"[Camera] Pan ignored: the view is {view_height_pixels} px high")
            return
        centimetres_per_pixel = self._get_view_height() / view_height_pixels
        rotation = self.rotation()
        right = rotation.rotatedVector(QVector3D(1, 0, 0))
        up = rotation.rotatedVector(QVector3D(0, 1, 0))
        # The scene follows the mouse, so the pivot moves the other way; screen Y grows downwards.
        offset = right * (-horizontal_pixels * centimetres_per_pixel) + up * (vertical_pixels * centimetres_per_pixel)
        self._pivot = self._pivot + offset
        self.changed.emit()

    @Slot(float)
    def dolly(self, steps: float) -> None:
        """Move toward the pivot by `steps` wheel notches (away when negative); orthographic scales to match."""
        distance = self._distance / DOLLY_FACTOR_PER_STEP**steps
        self._distance = min(max(distance, _MIN_DISTANCE_CM), _MAX_DISTANCE_CM)
        self.changed.emit()

    @Slot(str)
    def showAxisView(self, name: str) -> None:
        """Look along an axis, orthographic unless the reviewer chose perspective there (Blender's Auto Perspective)."""
        try:
            axis_view = AxisView(name)
        except ValueError:
            logger.warning(f"[Camera] No axis view '{name}'; expected one of {[view.value for view in AxisView]}")
            return
        if not self._is_orthographic:
            self._is_orthographic = True
            self._is_orthographic_automatic = True
        yaw, pitch = ORIENTATION_BY_AXIS_VIEW[axis_view]
        self._turn_to(yaw, pitch)

    @Slot()
    def toggleOrthographic(self) -> None:
        """Switch between perspective and orthographic (numpad 5); a choice made here stays when orbiting."""
        self._is_orthographic = not self._is_orthographic
        self._is_orthographic_automatic = False
        self.changed.emit()

    @Slot(QVector3D, float)
    def frame(self, centre: QVector3D, radius: float) -> None:
        """Fit a bounding sphere in view from the current direction, pivoting on its centre."""
        half_field_of_view = math.radians(FIELD_OF_VIEW_DEGREES) / 2
        self._pivot = QVector3D(centre)
        distance = max(radius, _MIN_DISTANCE_CM) / math.sin(half_field_of_view) * FRAME_MARGIN
        self._distance = min(distance, _MAX_DISTANCE_CM)
        self.changed.emit()

    @Slot()
    def resetDirection(self) -> None:
        """Turn to the default three-quarter view, from the -X -Y side and slightly above."""
        self._turn_to(THREE_QUARTER_YAW_DEGREES, THREE_QUARTER_PITCH_DEGREES)

    def _turn_to(self, yaw: float, pitch: float) -> None:
        """Turn to an orientation held to a turntable, leaving an automatic orthographic view that is off its axis."""
        self._yaw = yaw % 360
        self._pitch = min(max(pitch, -90.0), 90.0)
        if self._is_orthographic_automatic and self.axis_view() is None:
            self._is_orthographic = False
            self._is_orthographic_automatic = False
        self.changed.emit()
