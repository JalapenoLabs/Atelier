// Compare: two revisions of the selected export side by side, A (the older pick) on the left and B (the stage's
// revision) on the right. Either the same standard view of each, or both exports as models whose cameras move
// together. The models take their display settings (lighting, wireframe, grid) from the stage's viewport.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: comparePage
    required property Item primaryViewport
    readonly property bool isModels: studio.compareMode === "models"

    color: Theme.viewportBackground

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        RowLayout {
            Layout.fillWidth: true
            Layout.margins: Theme.gap
            spacing: Theme.gap

            Label {
                text: "Compare"
                color: Theme.text
                font.bold: true
            }

            ButtonGroup {
                id: modeGroup
            }

            Button {
                text: "Standard renders"
                checkable: true
                checked: !comparePage.isModels
                ButtonGroup.group: modeGroup
                onClicked: studio.setCompareMode("renders")
            }

            Button {
                text: "3D"
                checkable: true
                checked: comparePage.isModels
                ButtonGroup.group: modeGroup
                onClicked: studio.setCompareMode("models")
                ToolTip.visible: hovered
                ToolTip.text: "Both revisions' exports as models; the two cameras move together"
            }

            ToolSeparator {
                visible: !comparePage.isModels
            }

            Repeater {
                model: comparePage.isModels ? [] : studio.standardViews

                ToolButton {
                    required property string modelData
                    text: modelData
                    checkable: true
                    checked: studio.compareView === modelData
                    onClicked: studio.setCompareView(modelData)
                }
            }

            Item {
                Layout.fillWidth: true
            }

            Button {
                text: "Close compare"
                onClicked: studio.stopCompare()
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 2

            Repeater {
                model: [
                    {"letter": "A", "side": studio.compareLeft, "isLeader": true},
                    {"letter": "B", "side": studio.compareRight, "isLeader": false}
                ]

                Item {
                    id: half
                    required property var modelData
                    readonly property var side: modelData.side

                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true

                    Image {
                        anchors.fill: parent
                        anchors.margins: Theme.gap
                        visible: !comparePage.isModels
                        source: half.side.imageUrl
                        fillMode: Image.PreserveAspectFit
                        asynchronous: true
                        mipmap: true
                    }

                    Label {
                        anchors.centerIn: parent
                        visible: !comparePage.isModels && half.side.imageUrl === ""
                        text: half.side.imageNote
                        color: Theme.textDim
                    }

                    Viewport {
                        id: halfViewport
                        anchors.fill: parent
                        visible: comparePage.isModels
                        isPrimary: false
                        modelUrl: comparePage.isModels ? half.side.modelUrl : ""
                        modelStats: half.side.stats
                        frameKey: studio.selectedExport
                        // A frames itself; B takes A's camera, so both show the same view.
                        framesOnLoad: half.modelData.isLeader
                        lighting: comparePage.primaryViewport.lighting
                        wireframe: comparePage.primaryViewport.wireframe
                        showCollision: comparePage.primaryViewport.showCollision
                        showScale: comparePage.primaryViewport.showScale
                        showGrid: comparePage.primaryViewport.showGrid

                        Component.onCompleted: cameraLink.register(half.modelData.letter, halfViewport)
                    }

                    Label {
                        anchors.left: parent.left
                        anchors.top: parent.top
                        anchors.margins: Theme.gap * 1.5
                        text: half.modelData.letter + "   " + half.side.label
                        color: Theme.text
                        font.pixelSize: Theme.fontNormal
                        font.bold: true
                        padding: 6
                        background: Rectangle {
                            color: Theme.overlay
                            radius: 4
                        }
                    }
                }
            }
        }
    }

    // Keeps the two viewports' orbits equal: whichever one the reviewer moves, the other follows. The copy writes
    // three properties one by one, and each write fires the target's own change handler, which would copy its other,
    // not yet updated, properties back; the guard stops that echo.
    QtObject {
        id: cameraLink
        property Item viewA: null
        property Item viewB: null
        property bool isCopying: false

        function register(letter, view) {
            if (letter === "A")
                viewA = view
            else
                viewB = view
        }

        function copy(from, to) {
            if (!from || !to || isCopying)
                return
            isCopying = true
            to.orbitPosition = from.orbitPosition
            to.orbitRotation = from.orbitRotation
            to.cameraDistance = from.cameraDistance
            isCopying = false
        }
    }

    Connections {
        target: cameraLink.viewA
        function onOrbitPositionChanged() { cameraLink.copy(cameraLink.viewA, cameraLink.viewB) }
        function onOrbitRotationChanged() { cameraLink.copy(cameraLink.viewA, cameraLink.viewB) }
        function onCameraDistanceChanged() { cameraLink.copy(cameraLink.viewA, cameraLink.viewB) }
    }

    Connections {
        target: cameraLink.viewB
        function onOrbitPositionChanged() { cameraLink.copy(cameraLink.viewB, cameraLink.viewA) }
        function onOrbitRotationChanged() { cameraLink.copy(cameraLink.viewB, cameraLink.viewA) }
        function onCameraDistanceChanged() { cameraLink.copy(cameraLink.viewB, cameraLink.viewA) }
    }
}
