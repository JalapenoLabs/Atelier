// The note for a review decision on the selected tickets (or the one under the cursor): approve with an optional
// short note, or request changes with one note that every ticket's comment is tailored from. The note holds the
// reviewer's takes, verbatim and in order (voice notes arrive there as takes), above the text being typed, which is
// the last take. Enter sends, Shift+Enter starts a new line, Esc closes and keeps the note for next time. While it is
// open, voice notes (gui/voice.py) speak takes into it: hold ` to talk, Ctrl+R retake, Ctrl+K checkpoint by default.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: popup

    // "approve" or "changes".
    property string verdict: "approve"
    property int ticketCount: 0
    readonly property var note: studio.reviewBoard.decisionNote
    readonly property bool isChanges: verdict === "changes"
    readonly property bool canSend: !isChanges || !note.isEmpty

    function openFor(newVerdict) {
        ticketCount = studio.reviewBoard.decisionCount()
        if (ticketCount === 0)
            return
        verdict = newVerdict
        open()
        noteField.forceActiveFocus()
    }

    function send() {
        if (canSend && studio.reviewBoard.submitDecision(verdict))
            close()
    }

    anchors.centerIn: Overlay.overlay
    width: Math.min(560, parent ? parent.width - 80 : 560)
    modal: true
    focus: true
    padding: 16
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

    Overlay.modal: Rectangle {
        color: Theme.modalDim
    }

    // Takes spoken now go into this note.
    Binding {
        target: studio.voiceNotes
        property: "isBulkNoteOpen"
        value: popup.opened
    }

    background: Rectangle {
        radius: 8
        color: Theme.panel
        border.color: popup.isChanges ? Theme.danger : Theme.success
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 10

        RowLayout {
            objectName: "bulkNoteHeader"
            Layout.fillWidth: true
            spacing: 10

            Label {
                Layout.fillWidth: true
                text: {
                    const tickets = popup.ticketCount === 1 ? "1 ticket" : popup.ticketCount + " tickets"
                    return popup.isChanges ? "Request changes on " + tickets : "Approve " + tickets
                }
                color: Theme.text
                font.pixelSize: Theme.fontTitle
                font.bold: true
            }
            VoiceIndicator {
                place: "bulk"
            }
        }

        Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.textDim
            font.pixelSize: Theme.fontSmall
            text: popup.isChanges
                  ? "Say what to change. One note goes to every ticket, and each ticket's comment is written for it."
                  : "A short note is optional; without one each ticket gets \"Approved.\""
        }

        // The takes so far, in order: what voice notes have added, words or on their way to words. Takes the checkpoint
        // locks are marked down their side.
        Repeater {
            model: popup.note.takes

            delegate: Item {
                id: takeRow
                required property int index
                required property var modelData
                readonly property bool isWords: modelData.state === "words"
                Layout.fillWidth: true
                implicitHeight: isWords ? takeLabel.implicitHeight + 12 : spokenTake.implicitHeight

                Rectangle {
                    anchors.fill: parent
                    visible: takeRow.isWords
                    radius: 4
                    color: Theme.field

                    Label {
                        id: takeLabel
                        anchors.fill: parent
                        anchors.margins: 6
                        anchors.leftMargin: 10
                        wrapMode: Text.WordWrap
                        text: takeRow.modelData.text
                        color: Theme.text
                        font.pixelSize: Theme.fontNormal
                    }
                }
                SpokenTake {
                    id: spokenTake
                    anchors.fill: parent
                    visible: !takeRow.isWords
                    place: "bulk"
                    takeIndex: takeRow.index
                    takeState: takeRow.modelData.state
                    takeError: takeRow.modelData.error
                }
                Rectangle {
                    visible: takeRow.modelData.isLocked
                    width: 3
                    height: parent.height
                    radius: 1
                    color: Theme.accent
                }
            }
        }

        ScrollView {
            Layout.fillWidth: true
            Layout.preferredHeight: popup.isChanges ? 120 : 64

            TextArea {
                id: noteField
                text: popup.note.draft
                onTextChanged: popup.note.draft = text
                wrapMode: TextEdit.Wrap
                font.pixelSize: Theme.fontNormal
                placeholderText: popup.isChanges ? "What should change?" : "Optional note"

                Keys.onPressed: event => {
                    const isEnter = event.key === Qt.Key_Return || event.key === Qt.Key_Enter
                    if (isEnter && !(event.modifiers & Qt.ShiftModifier)) {
                        event.accepted = true
                        popup.send()
                    }
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            Item {
                Layout.fillWidth: true
            }
            Button {
                text: "Cancel"
                onClicked: popup.close()
            }
            Button {
                text: popup.isChanges ? "Request changes" : "Approve"
                highlighted: true
                enabled: popup.canSend
                onClicked: popup.send()
            }
        }
    }
}
