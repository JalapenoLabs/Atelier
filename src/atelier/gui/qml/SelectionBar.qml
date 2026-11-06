// The grid's floating selection bar, from left to right: how many tickets are selected and select all, the decisions,
// and clearing the selection. It shows while anything is selected.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: selectionBar
    objectName: "selectionBar"

    required property var board
    required property int tileCount
    // "approve" or "changes".
    signal decisionRequested(string verdict)

    implicitWidth: barRow.implicitWidth + 24
    implicitHeight: barRow.implicitHeight + 16
    radius: 8
    color: Theme.panelRaised
    border.color: Theme.accent

    // Clicks between the buttons stay on the bar rather than reaching the grid under it.
    MouseArea {
        anchors.fill: parent
    }

    RowLayout {
        id: barRow
        anchors.centerIn: parent
        spacing: 8

        Label {
            objectName: "selectionCount"
            Layout.leftMargin: 4
            text: selectionBar.board.selectedCount + " selected"
            color: Theme.text
            font.bold: true
        }
        Button {
            objectName: "selectAllButton"
            text: "Select all"
            flat: true
            icon.source: Qt.resolvedUrl("icons/select-all.svg")
            icon.color: Theme.text
            focusPolicy: Qt.NoFocus
            enabled: selectionBar.board.selectedCount < selectionBar.tileCount
            onClicked: selectionBar.board.selectAll()
            ToolTip.visible: hovered
            ToolTip.text: "Ctrl+A"
        }
        Rectangle {
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            color: Theme.border
        }
        Button {
            objectName: "approveButton"
            text: "Approve"
            icon.source: Qt.resolvedUrl("icons/check.svg")
            icon.color: Theme.text
            focusPolicy: Qt.NoFocus
            onClicked: selectionBar.decisionRequested("approve")
            ToolTip.visible: hovered
            ToolTip.text: "A"
        }
        Button {
            objectName: "requestChangesButton"
            text: "Request changes"
            icon.source: Qt.resolvedUrl("icons/pencil.svg")
            icon.color: Theme.text
            focusPolicy: Qt.NoFocus
            onClicked: selectionBar.decisionRequested("changes")
            ToolTip.visible: hovered
            ToolTip.text: "C"
        }
        Rectangle {
            Layout.preferredWidth: 1
            Layout.fillHeight: true
            color: Theme.border
        }
        ToolButton {
            objectName: "clearSelectionButton"
            display: AbstractButton.IconOnly
            icon.source: Qt.resolvedUrl("icons/close.svg")
            icon.color: Theme.text
            focusPolicy: Qt.NoFocus
            onClicked: selectionBar.board.clearSelection()
            ToolTip.visible: hovered
            ToolTip.text: "Clear the selection (Esc)"
        }
    }
}
