// One escalated tool call: what the worker wants to do, why the scope policy stopped it, and Allow / Deny.
// Used inline in the chat and in the header's approvals list. A pending card takes an optional note, which a denial
// passes on to the worker.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: card

    required property string approvalId
    required property string toolName
    required property string toolSummary
    required property string reason
    // "pending", "allowed", "denied" or "cancelled".
    property string status: "pending"
    property string sourceName: ""
    property string answerNote: ""

    readonly property bool isPending: status === "pending"
    readonly property var statusLabels: ({
        "pending": "Waiting for you",
        "allowed": "Allowed",
        "denied": "Denied",
        "cancelled": "Cancelled: the turn ended first"
    })

    implicitHeight: column.implicitHeight + 20
    radius: 6
    color: isPending ? Theme.pendingSurface : Theme.panelRaised
    border.color: isPending ? Theme.warning : Theme.border

    ColumnLayout {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 10
        spacing: 6

        RowLayout {
            Layout.fillWidth: true

            Label {
                text: "Approval"
                font.bold: true
                color: card.isPending ? Theme.warning : Theme.textDim
            }
            Label {
                visible: card.sourceName !== ""
                text: card.sourceName
                color: Theme.text
                font.bold: true
            }
            Item {
                Layout.fillWidth: true
            }
            Label {
                text: card.statusLabels[card.status] || card.status
                color: card.status === "allowed" ? Theme.success
                       : card.status === "denied" ? Theme.danger : Theme.textDim
                font.pixelSize: Theme.fontSmall
            }
        }

        Label {
            Layout.fillWidth: true
            text: card.reason
            wrapMode: Text.Wrap
            color: Theme.text
        }

        Label {
            Layout.fillWidth: true
            text: card.toolName + ": " + card.toolSummary
            wrapMode: Text.WrapAnywhere
            maximumLineCount: 4
            elide: Text.ElideRight
            font.family: "monospace"
            font.pixelSize: Theme.fontSmall
            color: Theme.textDim
        }

        Label {
            Layout.fillWidth: true
            visible: !card.isPending && card.answerNote !== ""
            text: card.answerNote
            wrapMode: Text.Wrap
            color: Theme.textDim
            font.italic: true
        }

        RowLayout {
            Layout.fillWidth: true
            visible: card.isPending

            TextField {
                id: note
                Layout.fillWidth: true
                placeholderText: "Note for the worker (optional)"
            }
            Button {
                text: "Deny"
                onClicked: studio.answerApproval(card.approvalId, false, note.text)
            }
            Button {
                text: "Allow"
                highlighted: true
                onClicked: studio.answerApproval(card.approvalId, true, note.text)
            }
        }
    }
}
