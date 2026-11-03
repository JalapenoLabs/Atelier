// A voice note's take that is not words yet (gui/voice.py): being recorded, being transcribed, or failed with its error
// and its recording kept, to retry or discard.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: spokenTake

    // "focus" or "bulk": where the note is.
    required property string place
    required property int takeIndex
    // "recording", "transcribing" or "failed".
    required property string takeState
    required property string takeError
    readonly property bool isFailed: takeState === "failed"

    implicitHeight: content.implicitHeight + 12
    radius: 4
    color: isFailed ? Theme.dangerSurface : Theme.field
    border.width: isFailed ? 1 : 0
    border.color: Theme.danger

    ColumnLayout {
        id: content
        anchors.fill: parent
        anchors.margins: 6
        spacing: 4

        Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: ({
                "recording": "Recording…",
                "transcribing": "Transcribing…",
                "failed": "Could not transcribe this take: " + spokenTake.takeError
            })[spokenTake.takeState] || spokenTake.takeState
            color: spokenTake.isFailed ? Theme.text : Theme.textDim
            font.pixelSize: Theme.fontNormal
            font.italic: !spokenTake.isFailed
        }
        RowLayout {
            visible: spokenTake.isFailed
            spacing: 6

            Button {
                text: "Retry"
                focusPolicy: Qt.NoFocus
                onClicked: studio.voiceNotes.retryTake(spokenTake.place, spokenTake.takeIndex)
                ToolTip.visible: hovered
                ToolTip.text: "Transcribe the kept recording again"
            }
            Button {
                text: "Discard"
                focusPolicy: Qt.NoFocus
                onClicked: studio.voiceNotes.discardTake(spokenTake.place, spokenTake.takeIndex)
                ToolTip.visible: hovered
                ToolTip.text: "Drop this take and its recording"
            }
        }
    }
}
