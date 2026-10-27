// Compare: two revisions of the selected export side by side, A (the older pick) on the left and B (the stage's
// revision) on the right. Either the same standard view of each, or both exports as models seen through one shared
// camera, so they always move together. The models take their display settings (lighting, wireframe, grid) from the
// stage's viewport.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Atelier

Rectangle {
    id: comparePage
    required property Item primaryViewport
    readonly property bool isModels: studio.compareMode === "models"

    color: Theme.viewportBackground

    // Both models' camera: navigating either viewport moves the one camera they both look through.
    ViewportCamera {
        id: sharedCamera
    }

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
                        anchors.fill: parent
                        visible: comparePage.isModels
                        isPrimary: false
                        modelUrl: comparePage.isModels ? half.side.modelUrl : ""
                        modelStats: half.side.stats
                        frameKey: studio.selectedExport
                        viewCamera: sharedCamera
                        // A frames the shared camera on its model, and takes the keys; B shows the same view.
                        framesOnLoad: half.modelData.isLeader
                        handlesKeys: half.modelData.isLeader && comparePage.isModels && comparePage.visible
                        lighting: comparePage.primaryViewport.lighting
                        wireframe: comparePage.primaryViewport.wireframe
                        showCollision: comparePage.primaryViewport.showCollision
                        showScale: comparePage.primaryViewport.showScale
                        showGrid: comparePage.primaryViewport.showGrid
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
}
