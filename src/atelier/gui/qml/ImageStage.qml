// A render shown full size on the stage, fitted to it, with a way back to the 3D viewport and into markup.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    color: Theme.viewportBackground

    Image {
        id: image
        anchors.fill: parent
        anchors.margins: Theme.gap
        source: studio.stageImage
        fillMode: Image.PreserveAspectFit
        asynchronous: true
        mipmap: true
        smooth: true
    }

    BusyIndicator {
        anchors.centerIn: parent
        visible: running
        running: image.status === Image.Loading
    }

    RowLayout {
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.margins: Theme.gap * 1.5
        spacing: Theme.gap

        Button {
            text: "3D view"
            visible: studio.selectedExport !== ""
            onClicked: studio.showModel()
        }

        Button {
            text: "Mark up"
            enabled: image.status === Image.Ready
            onClicked: studio.markupEditor.openImage(studio.stageImage, studio.stageImageName)
            ToolTip.visible: hovered
            ToolTip.text: "Draw on this render and send it to the source's worker"
        }

        Label {
            text: studio.stageImageName
                  + (image.status === Image.Ready ? "   " + image.sourceSize.width + " × " + image.sourceSize.height : "")
            color: Theme.text
            font.pixelSize: Theme.fontSmall

            background: Rectangle {
                color: Theme.overlay
                radius: 4
            }
            padding: 6
        }
    }
}
