// The render gallery under the stage: the stage revision's standard renders of the selected export as the first
// group, then every render of the source, grouped by stem. Clicking one shows it full size on the stage. A standard
// group with no images yet says why (rendering, failed, not rendered).
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    color: Theme.panel

    ListView {
        id: gallery
        anchors.fill: parent
        anchors.margins: Theme.gap
        orientation: ListView.Horizontal
        spacing: 6
        clip: true
        model: studio.renderRows
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.horizontal: ScrollBar {}

        section.property: "stem"
        section.delegate: Item {
            required property string section
            width: 26
            height: gallery.height

            Label {
                anchors.centerIn: parent
                rotation: -90
                text: section
                color: Theme.textDim
                font.pixelSize: Theme.fontSmall
                font.bold: true
            }
        }

        delegate: Rectangle {
            id: tile
            required property string path
            required property string name
            required property string url

            readonly property bool isShown: studio.stageImage === url
            width: tile.url === "" ? 110 : Math.max(thumbnail.paintedWidth, 60) + 8
            height: gallery.height - 12
            radius: 4
            color: isShown ? Theme.selection : Theme.panelRaised
            border.color: isShown ? Theme.accent : "transparent"

            Label {
                anchors.centerIn: parent
                visible: tile.url === ""
                width: parent.width - 8
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                text: tile.name
                color: Theme.textDim
            }

            Image {
                id: thumbnail
                visible: tile.url !== ""
                anchors.top: parent.top
                anchors.horizontalCenter: parent.horizontalCenter
                anchors.topMargin: 4
                height: parent.height - caption.height - 10
                width: height * 2
                fillMode: Image.PreserveAspectFit
                source: tile.url
                sourceSize.height: 192
                asynchronous: true
                smooth: true
            }

            Label {
                id: caption
                visible: tile.url !== ""
                anchors.bottom: parent.bottom
                anchors.bottomMargin: 2
                anchors.horizontalCenter: parent.horizontalCenter
                width: parent.width - 6
                horizontalAlignment: Text.AlignHCenter
                text: tile.name
                elide: Text.ElideMiddle
                color: Theme.textDim
                font.pixelSize: 10
            }

            MouseArea {
                anchors.fill: parent
                onClicked: studio.showRender(tile.path)
            }
        }

        Label {
            anchors.centerIn: parent
            visible: gallery.count === 0 && studio.selectedSource !== ""
            text: "No renders"
            color: Theme.textDim
        }
    }
}
