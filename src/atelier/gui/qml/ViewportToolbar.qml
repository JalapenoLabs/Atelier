// The viewport's display controls: lighting, wireframe, collision, scale figure, grid, framing, the load path,
// and the source's camera bookmarks.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: toolbar
    required property Item viewport

    implicitWidth: row.implicitWidth + 12
    implicitHeight: row.implicitHeight + 10
    radius: 6
    color: Theme.overlay

    RowLayout {
        id: row
        anchors.centerIn: parent
        spacing: 2

        ButtonGroup {
            id: lightingGroup
        }

        ToolButton {
            text: "Studio"
            checkable: true
            checked: toolbar.viewport.lighting === "studio"
            ButtonGroup.group: lightingGroup
            onClicked: toolbar.viewport.lighting = "studio"
            ToolTip.visible: hovered
            ToolTip.text: "Three-point lighting with the export's own materials"
        }
        ToolButton {
            text: "Clay"
            checkable: true
            checked: toolbar.viewport.lighting === "clay"
            ButtonGroup.group: lightingGroup
            onClicked: toolbar.viewport.lighting = "clay"
            ToolTip.visible: hovered
            ToolTip.text: "Untextured neutral grey, to judge the form"
        }

        ToolSeparator {
        }

        ToolButton {
            text: "Wireframe"
            checkable: true
            checked: toolbar.viewport.wireframe
            onToggled: toolbar.viewport.wireframe = checked
        }
        ToolButton {
            text: "Collision" + (toolbar.viewport.collisionModels.length ? " (" + toolbar.viewport.collisionModels.length + ")" : "")
            checkable: true
            enabled: toolbar.viewport.collisionModels.length > 0
            checked: toolbar.viewport.showCollision
            onToggled: toolbar.viewport.showCollision = checked
            ToolTip.visible: hovered
            ToolTip.text: "Show the UCX_ collision hulls as a translucent overlay"
        }
        ToolButton {
            text: "180 cm"
            checkable: true
            checked: toolbar.viewport.showScale
            onToggled: toolbar.viewport.showScale = checked
            ToolTip.visible: hovered
            ToolTip.text: "A 180 cm figure beside the model"
        }
        ToolButton {
            text: "Grid"
            checkable: true
            checked: toolbar.viewport.showGrid
            onToggled: toolbar.viewport.showGrid = checked
        }

        ToolSeparator {
        }

        ToolButton {
            text: "Frame"
            onClicked: toolbar.viewport.frame(false)
            ToolTip.visible: hovered
            ToolTip.text: "Frame the model (F); Shift+F frames it with the 180 cm figure"
        }
        ToolButton {
            text: "Direct FBX"
            checkable: true
            checked: toolbar.viewport.preferDirect
            onToggled: toolbar.viewport.preferDirect = checked
            ToolTip.visible: hovered
            ToolTip.text: "Keep the FBX as Qt's own importer reads it, instead of Blender's conversion"
        }
        ToolButton {
            id: viewsButton
            text: "Views ▾"
            onClicked: viewsPopup.open()
        }
    }

    Popup {
        id: viewsPopup
        y: toolbar.height + 4
        x: toolbar.width - width
        width: 260
        padding: 8

        ColumnLayout {
            anchors.fill: parent
            spacing: 6

            Label {
                text: "Camera bookmarks"
                color: Theme.textDim
                font.pixelSize: Theme.fontSmall
            }

            Repeater {
                model: studio.bookmarkRows

                RowLayout {
                    id: bookmarkRow
                    required property string name
                    required property real target_x
                    required property real target_y
                    required property real target_z
                    required property real yaw
                    required property real pitch
                    required property real distance
                    Layout.fillWidth: true

                    Button {
                        Layout.fillWidth: true
                        text: bookmarkRow.name
                        flat: true
                        onClicked: {
                            toolbar.viewport.applyBookmark(bookmarkRow.target_x, bookmarkRow.target_y,
                                                           bookmarkRow.target_z, bookmarkRow.yaw,
                                                           bookmarkRow.pitch, bookmarkRow.distance)
                            viewsPopup.close()
                        }
                    }
                    ToolButton {
                        text: "✕"
                        onClicked: studio.deleteBookmark(bookmarkRow.name)
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true

                TextField {
                    id: bookmarkName
                    Layout.fillWidth: true
                    placeholderText: "Name this view"
                    onAccepted: saveButton.clicked()
                }
                Button {
                    id: saveButton
                    text: "Save"
                    enabled: bookmarkName.text.trim() !== ""
                    onClicked: {
                        toolbar.viewport.saveBookmark(bookmarkName.text)
                        bookmarkName.clear()
                    }
                }
            }
        }
    }
}
