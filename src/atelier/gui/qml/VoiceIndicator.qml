// Voice notes' state for one place a note is spoken into (gui/voice.py): a red light and a level meter while a take
// is recording there, otherwise the talk key's hint, or why voice notes are off.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

RowLayout {
    id: indicator

    // "focus" or "bulk": where this indicator's note is.
    required property string place
    readonly property var voice: studio.voiceNotes
    readonly property bool isRecordingHere: voice.isRecording && voice.recordingPlace === place

    spacing: 6

    Rectangle {
        Layout.alignment: Qt.AlignVCenter
        visible: indicator.isRecordingHere
        width: 10
        height: 10
        radius: 5
        color: Theme.danger

        SequentialAnimation on opacity {
            running: indicator.isRecordingHere
            loops: Animation.Infinite
            NumberAnimation { to: 0.35; duration: 500 }
            NumberAnimation { to: 1; duration: 500 }
        }
    }
    Label {
        objectName: "voiceIndicatorLabel"
        text: {
            if (indicator.isRecordingHere)
                return "Recording"
            if (!indicator.voice.isAvailable)
                return "Voice off"
            const verb = indicator.voice.isToggle ? "Press " : "Hold "
            return verb + indicator.voice.talkKeyLabel + " to talk"
        }
        color: indicator.isRecordingHere ? Theme.text : Theme.textDim
        font.pixelSize: Theme.fontSmall
        font.bold: indicator.isRecordingHere

        ToolTip.visible: hintHover.hovered
        ToolTip.text: indicator.voice.isAvailable
                      ? indicator.voice.retakeKeyLabel + " retakes the newest take, " + indicator.voice.checkpointKeyLabel
                        + " locks the takes so far (Ctrl with either while typing)"
                      : indicator.voice.unavailableReason

        HoverHandler {
            id: hintHover
        }
    }
    // The level meter: how loud the last moment was.
    Rectangle {
        objectName: "voiceLevelMeter"
        Layout.alignment: Qt.AlignVCenter
        visible: indicator.isRecordingHere
        width: 64
        height: 6
        radius: 3
        color: Theme.field

        Rectangle {
            width: parent.width * Math.max(0, Math.min(1, indicator.voice.level))
            height: parent.height
            radius: 3
            color: indicator.voice.level > 0.92 ? Theme.warning : Theme.success
        }
    }
}
