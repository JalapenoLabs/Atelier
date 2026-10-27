"""The viewport's Blender-style camera: the turntable math, and the mouse and keys that drive it."""

from __future__ import annotations

import math
from collections.abc import Iterator

import pytest
from PySide6.QtCore import QByteArray, QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QGuiApplication, QQuaternion, QVector3D, QWheelEvent
from PySide6.QtQml import QQmlComponent, QQmlEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtTest import QTest

from atelier.gui.app import _MAIN_QML
from atelier.gui.camera import (
    DOLLY_FACTOR_PER_STEP,
    FIELD_OF_VIEW_DEGREES,
    FRAME_MARGIN,
    ORBIT_DEGREES_PER_PIXEL,
    ORBIT_STEP_DEGREES,
    THREE_QUARTER_PITCH_DEGREES,
    THREE_QUARTER_YAW_DEGREES,
    ViewportCamera,
)


def unreal(qt_vector: QVector3D) -> tuple[float, float, float]:
    """A Qt scene vector (Y up) in Unreal's frame (Z up), rounded so axis vectors compare exactly."""
    return (
        round(qt_vector.x(), 6) + 0.0,
        round(qt_vector.z(), 6) + 0.0,
        round(qt_vector.y(), 6) + 0.0,
    )


def image_axes(camera: ViewportCamera) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """The Unreal directions of the image's right and up."""
    rotation = camera.rotation()
    return unreal(rotation.rotatedVector(QVector3D(1, 0, 0))), unreal(rotation.rotatedVector(QVector3D(0, 1, 0)))


def direction_to_camera(camera: ViewportCamera) -> tuple[float, float, float]:
    return unreal((camera.position() - camera.property("pivot")).normalized())


@pytest.fixture
def camera(qt_application: QGuiApplication) -> ViewportCamera:
    view_camera = ViewportCamera()
    view_camera.place(QVector3D(0, 0, 0), 0, 0, 100)
    return view_camera


class TestAxisViews:
    """Numpad front, right and top must be exactly the standard renders' views (docs/revisions.md)."""

    def test_front_looks_from_plus_y_with_z_up(self, camera: ViewportCamera) -> None:
        camera.showAxisView("front")

        assert direction_to_camera(camera) == (0, 1, 0)
        assert image_axes(camera)[1] == (0, 0, 1)

    def test_right_is_the_side_render_from_plus_x_with_plus_y_on_the_left(self, camera: ViewportCamera) -> None:
        camera.showAxisView("right")

        image_right, image_up = image_axes(camera)
        assert direction_to_camera(camera) == (1, 0, 0)
        assert image_right == (0, -1, 0)
        assert image_up == (0, 0, 1)

    def test_top_looks_down_with_minus_x_up_and_plus_y_on_the_left(self, camera: ViewportCamera) -> None:
        camera.showAxisView("top")

        image_right, image_up = image_axes(camera)
        assert direction_to_camera(camera) == (0, 0, 1)
        assert image_up == (-1, 0, 0)
        assert image_right == (0, -1, 0)

    @pytest.mark.parametrize(
        ("name", "expected_direction"),
        [("back", (0, -1, 0)), ("left", (-1, 0, 0)), ("bottom", (0, 0, -1))],
    )
    def test_ctrl_views_look_from_the_opposite_side(
        self, camera: ViewportCamera, name: str, expected_direction: tuple[float, float, float]
    ) -> None:
        camera.showAxisView(name)

        assert direction_to_camera(camera) == expected_direction

    def test_an_axis_view_turns_orthographic_and_orbiting_off_it_turns_back(self, camera: ViewportCamera) -> None:
        camera.showAxisView("front")
        assert camera.property("isOrthographic")
        assert camera.property("viewLabel") == "Front orthographic"

        camera.orbit(3, 0)

        assert not camera.property("isOrthographic")
        assert camera.property("viewLabel") == "User perspective"

    def test_turning_around_an_axis_view_stays_orthographic_on_the_opposite_axis(self, camera: ViewportCamera) -> None:
        camera.showAxisView("front")

        camera.turnAround()

        assert camera.property("viewLabel") == "Back orthographic"

    def test_orthographic_chosen_with_numpad_5_stays_when_orbiting(self, camera: ViewportCamera) -> None:
        camera.toggleOrthographic()
        camera.showAxisView("front")

        camera.orbit(3, 0)

        assert camera.property("isOrthographic")
        assert camera.property("viewLabel") == "User orthographic"

    def test_numpad_5_on_an_automatic_orthographic_view_goes_back_to_perspective(self, camera: ViewportCamera) -> None:
        camera.showAxisView("top")

        camera.toggleOrthographic()

        assert camera.property("viewLabel") == "Top perspective"

    def test_an_unknown_view_changes_nothing(self, camera: ViewportCamera) -> None:
        camera.showAxisView("sideways")

        assert camera.property("yaw") == 0
        assert not camera.property("isOrthographic")


class TestOrbit:
    def test_dragging_right_turns_the_model_right_so_the_camera_goes_left(self, camera: ViewportCamera) -> None:
        camera.orbit(10, 0)

        assert camera.property("yaw") == pytest.approx(360 - 10 * ORBIT_DEGREES_PER_PIXEL)
        # From the front (+Y), the camera's left is -X.
        assert camera.position().x() < 0

    def test_dragging_up_tilts_the_model_up_so_the_camera_goes_down(self, camera: ViewportCamera) -> None:
        camera.orbit(0, -10)

        assert camera.property("pitch") == pytest.approx(10 * ORBIT_DEGREES_PER_PIXEL)
        assert camera.position().y() < 0

    def test_turntable_stops_at_the_poles_instead_of_flipping(self, camera: ViewportCamera) -> None:
        camera.orbit(0, 1000)
        assert camera.property("pitch") == -90
        camera.orbit(40, 0)
        image_up_at_top = image_axes(camera)[1]

        camera.orbit(0, -10_000)

        assert camera.property("pitch") == 90
        # Never any roll: the image's right stays level whatever the pitch.
        assert image_axes(camera)[0][2] == 0
        assert image_up_at_top[2] == 0

    def test_yaw_turns_about_the_world_vertical_at_any_pitch(self, camera: ViewportCamera) -> None:
        camera.place(QVector3D(0, 0, 0), 30, -40, 100)
        height_before = camera.position().y()

        camera.orbit(123, 0)

        assert camera.position().y() == pytest.approx(height_before)

    def test_numpad_steps_move_the_camera_as_blender_names_them(self, camera: ViewportCamera) -> None:
        camera.stepOrbit(1, 0)
        assert camera.property("yaw") == ORBIT_STEP_DEGREES

        camera.stepOrbit(0, 1)
        assert camera.property("pitch") == -ORBIT_STEP_DEGREES

    def test_six_steps_right_from_the_front_reach_the_right_view(self, camera: ViewportCamera) -> None:
        camera.showAxisView("front")
        camera.stepOrbit(1, 0)
        for _ in range(5):
            camera.stepOrbit(1, 0)

        assert camera.property("viewLabel") == "Right perspective"

    def test_euler_order_matches_qt_quick_3d(self, camera: ViewportCamera) -> None:
        """QML puts (pitch, yaw, 0) on the pivot node; Qt applies yaw about Y first, then pitch about X."""
        camera.place(QVector3D(0, 0, 0), 225, -22, 100)
        yaw_then_pitch = QQuaternion.fromAxisAndAngle(0, 1, 0, 225) * QQuaternion.fromAxisAndAngle(1, 0, 0, -22)

        expected = yaw_then_pitch.rotatedVector(QVector3D(0, 0, 100))
        assert (camera.position() - expected).length() == pytest.approx(0, abs=1e-4)


class TestPanAndDolly:
    def test_pan_moves_the_pivot_exactly_with_the_mouse(self, camera: ViewportCamera) -> None:
        view_height = camera.property("viewHeight")

        camera.pan(50, 0, 500)

        # From the front, the image's right is +X: the scene follows the mouse right, so the pivot goes left.
        assert camera.property("pivot").x() == pytest.approx(-view_height / 10)
        camera.pan(0, 50, 500)
        assert camera.property("pivot").y() == pytest.approx(view_height / 10)

    def test_pan_keeps_the_orientation_and_distance(self, camera: ViewportCamera) -> None:
        camera.place(QVector3D(10, 20, 30), 200, -30, 250)

        camera.pan(40, -25, 600)

        assert (camera.property("yaw"), camera.property("pitch"), camera.property("distance")) == (200, -30, 250)

    def test_wheel_steps_dolly_toward_the_pivot_and_back(self, camera: ViewportCamera) -> None:
        camera.dolly(1)
        assert camera.property("distance") == pytest.approx(100 / DOLLY_FACTOR_PER_STEP)

        camera.dolly(-2)
        assert camera.property("distance") == pytest.approx(100 * DOLLY_FACTOR_PER_STEP)

    def test_dolly_never_reaches_the_pivot(self, camera: ViewportCamera) -> None:
        camera.dolly(1000)

        assert camera.property("distance") == 1

    def test_orthographic_shows_the_height_perspective_sees_at_the_pivot(self, camera: ViewportCamera) -> None:
        expected = 2 * 100 * math.tan(math.radians(FIELD_OF_VIEW_DEGREES / 2))

        assert camera.property("viewHeight") == pytest.approx(expected)


class TestFrame:
    def test_frame_keeps_the_direction_and_fits_the_bounding_sphere(self, camera: ViewportCamera) -> None:
        camera.place(QVector3D(0, 0, 0), 90, -30, 100)

        camera.frame(QVector3D(5, 50, 0), 40)

        expected_distance = 40 / math.sin(math.radians(FIELD_OF_VIEW_DEGREES / 2)) * FRAME_MARGIN
        assert camera.property("pivot") == QVector3D(5, 50, 0)
        assert camera.property("distance") == pytest.approx(expected_distance)
        assert (camera.property("yaw"), camera.property("pitch")) == (90, -30)
        # The sphere fits the orthographic view too.
        assert camera.property("viewHeight") > 2 * 40

    def test_reset_direction_is_the_three_quarter_view(self, camera: ViewportCamera) -> None:
        camera.resetDirection()

        assert camera.property("yaw") == THREE_QUARTER_YAW_DEGREES
        assert camera.property("pitch") == THREE_QUARTER_PITCH_DEGREES
        assert direction_to_camera(camera)[0] < 0
        assert direction_to_camera(camera)[1] < 0


# ####################### #
#      INPUT MAPPING      #
# ####################### #

# A window holding only the navigation over a camera. Loaded with a URL in the QML folder, so ViewportNavigation
# resolves from there as it does in the app.
_HARNESS_QML = b"""
import QtQuick
import Atelier

Window {
    width: 400
    height: 300
    visible: true
    property int frameRequests: 0
    property var threeQuarterRequests: []

    ViewportCamera {
        id: harnessCamera
        objectName: "harnessCamera"
    }

    ViewportNavigation {
        anchors.fill: parent
        viewCamera: harnessCamera
        onFrameRequested: frameRequests += 1
        onThreeQuarterFrameRequested: (withFigure) => threeQuarterRequests = threeQuarterRequests.concat([withFigure])
    }
}
"""


@pytest.fixture
def harness(qt_application: QGuiApplication) -> Iterator[QQuickWindow]:
    engine = QQmlEngine()
    component = QQmlComponent(engine)
    component.setData(QByteArray(_HARNESS_QML), QUrl.fromLocalFile(str(_MAIN_QML.parent / "NavigationHarness.qml")))
    window = component.create()
    assert isinstance(window, QQuickWindow), [error.toString() for error in component.errors()]
    window.requestActivate()
    assert QTest.qWaitForWindowActive(window)
    yield window
    window.close()
    window.deleteLater()


def harness_camera(window: QQuickWindow) -> ViewportCamera:
    # Read as an object, since PySide cannot convert a QML property typed with a Python class.
    view_camera = window.findChild(ViewportCamera, "harnessCamera")
    assert isinstance(view_camera, ViewportCamera)
    view_camera.place(QVector3D(0, 0, 0), 0, 0, 100)
    return view_camera


def drag(
    window: QQuickWindow,
    button: Qt.MouseButton,
    offset: QPoint,
    modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
) -> None:
    start = QPoint(200, 150)
    QTest.mousePress(window, button, modifiers, start)
    # Two moves, because the first is spent getting past the drag threshold on some platforms.
    QTest.mouseMove(window, start + offset / 2)
    QTest.mouseMove(window, start + offset)
    QTest.mouseRelease(window, button, modifiers, start + offset)


class TestMouse:
    @pytest.mark.parametrize("button", [Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton])
    def test_middle_or_left_drag_orbits(self, harness: QQuickWindow, button: Qt.MouseButton) -> None:
        view_camera = harness_camera(harness)

        drag(harness, button, QPoint(20, 0))

        assert view_camera.property("yaw") == pytest.approx(360 - 20 * ORBIT_DEGREES_PER_PIXEL)
        assert view_camera.property("pivot") == QVector3D(0, 0, 0)

    @pytest.mark.parametrize(
        ("button", "modifiers"),
        [
            (Qt.MouseButton.MiddleButton, Qt.KeyboardModifier.ShiftModifier),
            (Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier),
            (Qt.MouseButton.RightButton, Qt.KeyboardModifier.NoModifier),
        ],
    )
    def test_shift_drag_or_right_drag_pans(
        self, harness: QQuickWindow, button: Qt.MouseButton, modifiers: Qt.KeyboardModifier
    ) -> None:
        view_camera = harness_camera(harness)

        drag(harness, button, QPoint(30, 0), modifiers)

        assert view_camera.property("pivot").x() < 0
        assert view_camera.property("yaw") == 0

    @pytest.mark.parametrize("button", [Qt.MouseButton.MiddleButton, Qt.MouseButton.LeftButton])
    def test_ctrl_drag_down_zooms_in(self, harness: QQuickWindow, button: Qt.MouseButton) -> None:
        view_camera = harness_camera(harness)

        drag(harness, button, QPoint(0, 40), Qt.KeyboardModifier.ControlModifier)

        assert view_camera.property("distance") == pytest.approx(100 / DOLLY_FACTOR_PER_STEP)
        assert view_camera.property("yaw") == 0

    def test_wheel_up_zooms_in(self, harness: QQuickWindow) -> None:
        view_camera = harness_camera(harness)
        position = QPointF(200, 150)
        wheel = QWheelEvent(
            position,
            harness.mapToGlobal(position),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )

        QGuiApplication.sendEvent(harness, wheel)

        assert view_camera.property("distance") == pytest.approx(100 / DOLLY_FACTOR_PER_STEP)


class TestKeys:
    @pytest.mark.parametrize(
        ("key", "modifiers", "expected_label"),
        [
            (Qt.Key.Key_1, Qt.KeyboardModifier.KeypadModifier, "Front orthographic"),
            (Qt.Key.Key_3, Qt.KeyboardModifier.KeypadModifier, "Right orthographic"),
            (Qt.Key.Key_7, Qt.KeyboardModifier.KeypadModifier, "Top orthographic"),
            (
                Qt.Key.Key_1,
                Qt.KeyboardModifier.KeypadModifier | Qt.KeyboardModifier.ControlModifier,
                "Back orthographic",
            ),
            (
                Qt.Key.Key_3,
                Qt.KeyboardModifier.KeypadModifier | Qt.KeyboardModifier.ControlModifier,
                "Left orthographic",
            ),
            (
                Qt.Key.Key_7,
                Qt.KeyboardModifier.KeypadModifier | Qt.KeyboardModifier.ControlModifier,
                "Bottom orthographic",
            ),
            # Num Lock off: the numpad's 1 sends End.
            (Qt.Key.Key_End, Qt.KeyboardModifier.KeypadModifier, "Front orthographic"),
        ],
    )
    def test_numpad_axis_views(
        self, harness: QQuickWindow, key: Qt.Key, modifiers: Qt.KeyboardModifier, expected_label: str
    ) -> None:
        view_camera = harness_camera(harness)
        view_camera.orbit(7, 3)

        QTest.keyClick(harness, key, modifiers)

        assert view_camera.property("viewLabel") == expected_label

    def test_numpad_5_toggles_orthographic(self, harness: QQuickWindow) -> None:
        view_camera = harness_camera(harness)

        QTest.keyClick(harness, Qt.Key.Key_5, Qt.KeyboardModifier.KeypadModifier)

        assert view_camera.property("isOrthographic")

    @pytest.mark.parametrize(
        ("key", "expected_yaw", "expected_pitch"),
        [
            (Qt.Key.Key_6, ORBIT_STEP_DEGREES, 0),
            (Qt.Key.Key_4, 360 - ORBIT_STEP_DEGREES, 0),
            (Qt.Key.Key_8, 0, -ORBIT_STEP_DEGREES),
            (Qt.Key.Key_2, 0, ORBIT_STEP_DEGREES),
            (Qt.Key.Key_9, 180, 0),
        ],
    )
    def test_numpad_steps(self, harness: QQuickWindow, key: Qt.Key, expected_yaw: float, expected_pitch: float) -> None:
        view_camera = harness_camera(harness)

        QTest.keyClick(harness, key, Qt.KeyboardModifier.KeypadModifier)

        assert (view_camera.property("yaw"), view_camera.property("pitch")) == (expected_yaw, expected_pitch)

    def test_numpad_plus_and_minus_zoom(self, harness: QQuickWindow) -> None:
        view_camera = harness_camera(harness)

        QTest.keyClick(harness, Qt.Key.Key_Plus, Qt.KeyboardModifier.KeypadModifier)

        assert view_camera.property("distance") == pytest.approx(100 / DOLLY_FACTOR_PER_STEP)

    def test_the_top_row_digits_are_not_numpad_views(self, harness: QQuickWindow) -> None:
        view_camera = harness_camera(harness)

        QTest.keyClick(harness, Qt.Key.Key_1)

        assert not view_camera.property("isOrthographic")

    def test_numpad_period_and_home_frame_and_f_frames_from_three_quarter(self, harness: QQuickWindow) -> None:
        QTest.keyClick(harness, Qt.Key.Key_Period, Qt.KeyboardModifier.KeypadModifier)
        QTest.keyClick(harness, Qt.Key.Key_Home)
        QTest.keyClick(harness, Qt.Key.Key_F)
        QTest.keyClick(harness, Qt.Key.Key_F, Qt.KeyboardModifier.ShiftModifier)

        assert harness.property("frameRequests") == 2
        assert harness.property("threeQuarterRequests").toVariant() == [False, True]

    def test_keys_do_nothing_while_the_viewport_does_not_handle_them(self, harness: QQuickWindow) -> None:
        view_camera = harness_camera(harness)
        navigation = harness.contentItem().childItems()[0]
        assert isinstance(navigation, QObject)
        navigation.setProperty("handlesKeys", False)

        QTest.keyClick(harness, Qt.Key.Key_1, Qt.KeyboardModifier.KeypadModifier)

        assert not view_camera.property("isOrthographic")
