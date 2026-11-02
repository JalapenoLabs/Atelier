// Focus mode's page: one ticket large, opened from the grid with Enter or a double click (the review board's
// `focusRequested`). Until focus mode's stage, markup and notes land here, it shows the ticket's hero image full size.
// Esc goes back to the grid.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: focusPage
    color: Theme.window

    readonly property var tile: studio.reviewBoard.focusedTile

    Keys.onEscapePressed: studio.reviewBoard.closeFocus()

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 44
            color: Theme.panel

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                spacing: 10

                Button {
                    text: "Back to the grid"
                    onClicked: studio.reviewBoard.closeFocus()
                    ToolTip.visible: hovered
                    ToolTip.text: "Esc"
                }
                Label {
                    text: focusPage.tile.ticket || ""
                    color: Theme.text
                    font.pixelSize: Theme.fontTitle
                    font.bold: true
                }
                Label {
                    Layout.fillWidth: true
                    text: focusPage.tile.summary || ""
                    color: Theme.textDim
                    font.pixelSize: Theme.fontNormal
                    elide: Text.ElideRight
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            color: Theme.viewportBackground

            Image {
                id: heroImage
                anchors.fill: parent
                anchors.margins: Theme.gap
                source: focusPage.tile.hero_url || ""
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                smooth: true
                mipmap: true
            }

            Label {
                anchors.centerIn: parent
                visible: !focusPage.tile.hero_url || heroImage.status === Image.Error
                text: focusPage.tile.hero_note || ""
                color: Theme.textDim
            }
        }
    }
}
