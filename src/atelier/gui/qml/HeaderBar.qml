// The window's title bar row: the app name, the project picker, the latest status message, and the approvals badge:
// every worker tool call, in any project, waiting for you, listed as cards in its popup.
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

        Button {
            id: approvalsButton
            text: studio.approvalCount === 1 ? "1 approval" : studio.approvalCount + " approvals"
            highlighted: studio.approvalCount > 0
            enabled: studio.approvalCount > 0
            onClicked: approvalsPopup.open()
            ToolTip.visible: hovered
            ToolTip.text: "Worker tool calls outside their scope, waiting for you"
        }
    }

    Popup {
        id: approvalsPopup
        x: parent.width - width - 8
        y: parent.height + 4
        width: 460
        height: Math.min(560, approvalsList.contentHeight + 24)
        padding: 12

        Connections {
            target: studio
            function onApprovalsChanged() {
                if (studio.approvalCount === 0)
                    approvalsPopup.close()
            }
        }

        ListView {
            id: approvalsList
            anchors.fill: parent
            clip: true
            spacing: 8
            model: studio.approvalRows

            // `reason` is a role and the card's own required property, so it binds by name.
            delegate: ApprovalCard {
                required property string approval_id
                required property string source
                required property string source_name
                required property string tool_name
                required property string tool_summary
                required property string project
                required property bool is_in_open_project
                width: approvalsList.width
                approvalId: approval_id
                toolName: tool_name
                toolSummary: tool_summary
                sourceName: is_in_open_project ? source_name : project + " · " + source_name

                MouseArea {
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.right: parent.right
                    height: 28
                    enabled: is_in_open_project
                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                    onClicked: studio.selectSource(source)
                }
            }
        }
    }
}
