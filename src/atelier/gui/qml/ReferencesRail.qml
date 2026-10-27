// The references rail: the project's reference images, tagged by folder. Images dropped here are copied into
// the project's reference drop folder (`references.drop_folder` in atelier.toml). With a source selected, each
// image can be pinned to that source's next worker prompt.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: rail

    DropArea {
        id: dropArea
        anchors.fill: parent
        keys: ["text/uri-list"]
        onDropped: drop => {
            if (drop.hasUrls) {
                studio.addReferences(drop.urls)
                drop.acceptProposedAction()
            }
        }
    }

    GridView {
        id: grid
        anchors.fill: parent
        anchors.margins: Theme.gap
        clip: true
        model: studio.referenceRows
        cellWidth: width / Math.max(1, Math.floor(width / 150))
        cellHeight: cellWidth + 22
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {}

        delegate: Item {
            id: cell
            required property string path
            required property string name
            required property string tag
            required property string url
            required property bool is_pinned
            width: grid.cellWidth
            height: grid.cellHeight

            Rectangle {
                anchors.fill: parent
                anchors.margins: 3
                radius: 4
                color: Theme.panelRaised

                Image {
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: 4
                    height: width
                    fillMode: Image.PreserveAspectFit
                    source: cell.url
                    sourceSize.width: 256
                    asynchronous: true
                }

                Label {
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left
                    anchors.right: pinButton.left
                    anchors.margins: 4
                    text: cell.tag
                    elide: Text.ElideRight
                    color: Theme.textDim
                    font.pixelSize: Theme.fontSmall
                }

                // Pins the reference to the selected source's next prompt; the worker gets its path.
                ToolButton {
                    id: pinButton
                    anchors.bottom: parent.bottom
                    anchors.right: parent.right
                    visible: studio.selectedSource !== ""
                    height: 24
                    text: cell.is_pinned ? "Pinned" : "Pin"
                    font.pixelSize: Theme.fontSmall
                    opacity: cell.is_pinned || hovered ? 1 : 0.6
                    onClicked: studio.togglePinnedReference(cell.path)
                    ToolTip.visible: hovered
                    ToolTip.text: cell.is_pinned ? "Pinned to " + studio.selectedName + "'s next prompt; click to unpin"
                                                 : "Pin to " + studio.selectedName + "'s next prompt"
                }

                Rectangle {
                    anchors.fill: parent
                    visible: cell.is_pinned
                    color: "transparent"
                    border.color: Theme.accent
                    border.width: 2
                    radius: 4
                }
            }

            ToolTip.visible: hoverHandler.hovered
            ToolTip.delay: 600
            ToolTip.text: cell.path

            HoverHandler {
                id: hoverHandler
            }
        }

        Label {
            anchors.centerIn: parent
            width: parent.width - 40
            visible: grid.count === 0
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
            text: studio.projectName ? "No references yet. Drop images here to add them to the project." : ""
            color: Theme.textDim
        }
    }

    Rectangle {
        anchors.fill: parent
        visible: dropArea.containsDrag
        color: Theme.dropHighlight
        border.color: Theme.accent
        border.width: 2
        radius: 6

        Label {
            anchors.centerIn: parent
            text: "Drop to add to the project's references"
            color: Theme.text
        }
    }
}
