// The revision filmstrip under the gallery: the selected source's revisions, newest first, each with the hero
// standard render of the selected export. The stage follows the newest until one is clicked. Right-click a revision
// to compare the stage's revision with it; Restore writes the staged revision back into the project.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    color: Theme.panel

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.gap
        spacing: 4

        RowLayout {
            Layout.fillWidth: true
            spacing: Theme.gap

            Label {
                text: "Revisions"
                color: Theme.text
                font.bold: true
            }

            Label {
                visible: studio.hasRevisions
                text: studio.isFollowingNewest ? "following the newest" : "showing " + studio.stagedLabel
                color: Theme.textDim
                font.pixelSize: Theme.fontSmall
            }

            Label {
                visible: studio.restoreMarkText !== ""
                text: studio.restoreMarkText
                color: Theme.statusColor("in-review")
                font.pixelSize: Theme.fontSmall
            }

            Item {
                Layout.fillWidth: true
            }

            Button {
                text: "Back to newest"
                visible: !studio.isFollowingNewest
                onClicked: studio.followNewest()
            }

            Button {
                text: "Compare"
                checkable: true
                checked: studio.isComparing
                enabled: studio.hasRevisions
                onClicked: studio.isComparing ? studio.stopCompare() : studio.startCompare()
                ToolTip.visible: hovered
                ToolTip.text: "Compare the stage's revision with the one before it; right-click a revision to compare with it instead"
            }

            Button {
                text: "Restore " + studio.stagedLabel + "…"
                visible: !studio.isFollowingNewest
                onClicked: restoreDialog.open()
            }
        }

        ListView {
            id: strip
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: ListView.Horizontal
            spacing: 6
            clip: true
            model: studio.revisionRows
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.horizontal: ScrollBar {}

            delegate: Rectangle {
                id: tile
                required property int id
                required property string label
                required property string when
                required property string trigger
                required property string note
                required property string thumbnail
                required property bool is_staged
                required property bool is_base

                width: height * 1.1
                height: strip.height - 12
                radius: 4
                color: is_staged ? Theme.selection : Theme.panelRaised
                border.width: is_staged || is_base ? 2 : 0
                border.color: is_base ? Theme.statusColor("in-review") : Theme.accent

                Image {
                    id: thumbnailImage
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: caption.top
                    anchors.margins: 4
                    fillMode: Image.PreserveAspectFit
                    source: tile.thumbnail
                    sourceSize.height: 160
                    asynchronous: true
                }

                Label {
                    anchors.centerIn: thumbnailImage
                    visible: tile.thumbnail === ""
                    text: "no render"
                    color: Theme.textDim
                    font.pixelSize: 10
                }

                Label {
                    anchors.top: parent.top
                    anchors.right: parent.right
                    anchors.margins: 4
                    visible: studio.isComparing && (tile.is_staged || tile.is_base)
                    text: tile.is_base ? "A" : "B"
                    color: Theme.text
                    font.bold: true
                    padding: 3
                    background: Rectangle {
                        color: tile.is_base ? Theme.statusColor("in-review") : Theme.accent
                        radius: 3
                    }
                }

                Label {
                    id: caption
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.margins: 4
                    horizontalAlignment: Text.AlignHCenter
                    elide: Text.ElideRight
                    text: "<b>" + tile.label + "</b> " + tile.trigger + (tile.note ? " " + tile.note : "")
                          + "<br>" + tile.when
                    textFormat: Text.StyledText
                    color: Theme.textDim
                    font.pixelSize: 10
                }

                MouseArea {
                    anchors.fill: parent
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    onClicked: mouse => {
                        if (mouse.button === Qt.RightButton)
                            studio.setCompareBase(tile.id)
                        else
                            studio.selectRevision(tile.id)
                    }
                }

                ToolTip.visible: hoverArea.hovered
                ToolTip.text: tile.label + ": click to show it on the stage, right-click to compare with it"

                HoverHandler {
                    id: hoverArea
                }
            }

            Label {
                anchors.centerIn: parent
                visible: strip.count === 0 && studio.selectedSource !== ""
                text: "No revisions yet. One is recorded when this source's files change."
                color: Theme.textDim
            }
        }
    }

    Dialog {
        id: restoreDialog
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: 460
        modal: true
        title: "Restore " + studio.stagedLabel + "?"
        standardButtons: Dialog.Ok | Dialog.Cancel
        onAccepted: studio.restoreRevision(studio.stagedRevision)

        Label {
            width: restoreDialog.availableWidth
            wrapMode: Text.WordWrap
            color: Theme.text
            text: "This writes " + studio.stagedLabel + "'s .blend, exports, README and builder back into the project. "
                  + "Files shared with other sources are left alone. The current files are saved as a revision "
                  + "first, so restoring that one undoes this."
        }
    }
}
