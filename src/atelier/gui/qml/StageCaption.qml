// A caption in the top left corner of a stage, on a translucent backing so it reads over any image.
import QtQuick
import QtQuick.Controls

Label {
    anchors.top: parent.top
    anchors.left: parent.left
    anchors.margins: Theme.gap * 1.5
    color: Theme.text
    font.pixelSize: Theme.fontSmall
    padding: 6
    background: Rectangle {
        color: Theme.overlay
        radius: 4
    }
}
