// Blender-style navigation over a viewport: the mouse and the numpad drive a ViewportCamera (gui/camera.py, where the
// camera math lives).
//
// Mouse: middle-drag orbits the pivot, Shift+middle-drag pans, Ctrl+middle-drag and the wheel zoom, as in Blender.
// The left button does the same, the viewer having nothing to select, and the right button pans, for trackpads and
// mice without an easy middle button. A drag keeps the gesture it started with, as Blender's does.
//
// Keys, as Blender's numpad: 1 front, 3 right, 7 top (Ctrl for the opposite side), 5 perspective or orthographic,
// 2 4 6 8 step the orbit, 9 the opposite side, + and - zoom, . (and Home) frame the model from where the camera is.
// F frames it from the default three-quarter view, and Shift+F with the 180 cm figure. Each numpad key is also bound
// as the key it sends with Num Lock off (End for 1, and so on), so the numpad works either way.
import QtQuick
import Atelier

MouseArea {
    id: navigation

    required property ViewportCamera viewCamera
    property bool handlesKeys: true

    // Home and numpad . frame the model keeping the view's direction; F and Shift+F from the three-quarter view.
    signal frameRequested()
    signal threeQuarterFrameRequested(bool withFigure)

    // The gesture of the drag in progress, and where the mouse last was.
    property string gesture: ""
    property point lastPosition: Qt.point(0, 0)

    // Ctrl+drag zooms by one wheel notch for this many pixels; dragging down zooms in (Blender's vertical zoom).
    readonly property real dollyPixelsPerStep: 40

    acceptedButtons: Qt.LeftButton | Qt.MiddleButton | Qt.RightButton

    function gestureFor(button, modifiers) {
        if (button === Qt.RightButton)
            return "pan"
        if (modifiers & Qt.ShiftModifier)
            return "pan"
        if (modifiers & Qt.ControlModifier)
            return "dolly"
        return "orbit"
    }

    onPressed: (mouse) => {
        gesture = gestureFor(mouse.button, mouse.modifiers)
        lastPosition = Qt.point(mouse.x, mouse.y)
    }

    onPositionChanged: (mouse) => {
        const horizontal = mouse.x - lastPosition.x
        const vertical = mouse.y - lastPosition.y
        lastPosition = Qt.point(mouse.x, mouse.y)
        const moveByGesture = {
            "orbit": () => viewCamera.orbit(horizontal, vertical),
            "pan": () => viewCamera.pan(horizontal, vertical, navigation.height),
            "dolly": () => viewCamera.dolly(vertical / dollyPixelsPerStep),
        }
        const move = moveByGesture[gesture]
        if (move)
            move()
    }

    onReleased: gesture = ""
    onCanceled: gesture = ""

    // One notch is 120 eighths of a degree; a trackpad sends fractions of a notch.
    onWheel: (wheel) => viewCamera.dolly(wheel.angleDelta.y / 120)

    // Each key: the numpad key, the key the numpad sends with Num Lock off (when it differs), and what it does.
    readonly property var keyBindings: [
        {"keys": ["Num+1", "Num+End"], "act": () => viewCamera.showAxisView("front")},
        {"keys": ["Ctrl+Num+1", "Ctrl+Num+End"], "act": () => viewCamera.showAxisView("back")},
        {"keys": ["Num+3", "Num+PgDown"], "act": () => viewCamera.showAxisView("right")},
        {"keys": ["Ctrl+Num+3", "Ctrl+Num+PgDown"], "act": () => viewCamera.showAxisView("left")},
        {"keys": ["Num+7", "Num+Home"], "act": () => viewCamera.showAxisView("top")},
        {"keys": ["Ctrl+Num+7", "Ctrl+Num+Home"], "act": () => viewCamera.showAxisView("bottom")},
        {"keys": ["Num+5", "Num+Clear"], "act": () => viewCamera.toggleOrthographic()},
        {"keys": ["Num+4", "Num+Left"], "act": () => viewCamera.stepOrbit(-1, 0)},
        {"keys": ["Num+6", "Num+Right"], "act": () => viewCamera.stepOrbit(1, 0)},
        {"keys": ["Num+8", "Num+Up"], "act": () => viewCamera.stepOrbit(0, 1)},
        {"keys": ["Num+2", "Num+Down"], "act": () => viewCamera.stepOrbit(0, -1)},
        {"keys": ["Num+9", "Num+PgUp"], "act": () => viewCamera.turnAround()},
        {"keys": ["Num++"], "act": () => viewCamera.dolly(1)},
        {"keys": ["Num+-"], "act": () => viewCamera.dolly(-1)},
        {"keys": ["Num+.", "Num+Del", "Home"], "act": () => navigation.frameRequested()},
        {"keys": ["F"], "act": () => navigation.threeQuarterFrameRequested(false)},
        {"keys": ["Shift+F"], "act": () => navigation.threeQuarterFrameRequested(true)},
    ]

    Instantiator {
        model: navigation.keyBindings

        delegate: Shortcut {
            required property var modelData
            sequences: modelData.keys
            enabled: navigation.handlesKeys
            onActivated: modelData.act()
        }
    }
}
