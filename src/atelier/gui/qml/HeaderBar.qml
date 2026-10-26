// The window's title bar row: the app name, the project picker and the latest status message.
// The approvals badge for worker tool calls joins this row with workers.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    implicitHeight: 44
    color: Theme.panel

    Rectangle {
        anchors.bottom: parent.bottom
        width: parent.width
        height: 1
        color: Theme.border
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 12
        spacing: 10

        Label {
            text: "Atelier"
            font.pixelSize: Theme.fontTitle
            font.bold: true
            color: Theme.text
        }

        ComboBox {
            id: projectPicker
            Layout.preferredWidth: 220
            model: studio.projects
            displayText: studio.projectName || "Open a project"
            onActivated: studio.openProject(currentText)
            onModelChanged: currentIndex = studio.projects.indexOf(studio.projectName)

            Connections {
                target: studio
                function onProjectChanged() {
                    projectPicker.currentIndex = studio.projects.indexOf(studio.projectName)
                }
            }
        }

        BusyIndicator {
            Layout.preferredWidth: 22
            Layout.preferredHeight: 22
            running: studio.isBusy
            visible: running
        }

        Label {
            Layout.fillWidth: true
            text: studio.message
            color: Theme.textDim
            elide: Text.ElideRight
            font.pixelSize: Theme.fontSmall
        }
    }
}
