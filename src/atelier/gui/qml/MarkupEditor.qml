// The markup editor: a render or a frozen viewport frame under a drawing layer (DrawingSurface.qml). Pen,
// highlighter, eraser, arrow and box strokes in a few colours and sizes, numbered pins with notes, undo, redo and
// clear (DrawingTools.qml), and Send, which stores the capture as feedback and hands it to the source's worker with
// the note typed beside it.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: editor
    readonly property var markup: studio.markupEditor

    color: Theme.viewportBackground

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            DrawingSurface {
                anchors.fill: parent
                anchors.margins: Theme.gap
                drawing: editor.markup
                interactive: !editor.markup.isSending
            }

            Label {
                anchors.top: parent.top
                anchors.left: parent.left
                anchors.margins: Theme.gap * 1.5
                text: "Marking up " + editor.markup.label
                color: Theme.text
                font.pixelSize: Theme.fontSmall
                padding: 6
                background: Rectangle {
                    color: Theme.overlay
                    radius: 4
                }
            }
        }

        // Tools, pins and sending.
        Rectangle {
            Layout.preferredWidth: 280
            Layout.fillHeight: true
            color: Theme.panel

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 10
                spacing: 8

                DrawingTools {
                    Layout.fillWidth: true
                    drawing: editor.markup
                }

                Label {
                    text: "Pins"
                    color: Theme.textDim
                    font.pixelSize: Theme.fontSmall
                }

                ListView {
                    id: pinList
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 4
                    model: editor.markup.pinRows

                    delegate: RowLayout {
                        required property int number
                        required property string note
                        width: pinList.width

                        Rectangle {
                            width: 22
                            height: 22
                            radius: 11
                            color: editor.markup.pinColor

                            Label {
                                anchors.centerIn: parent
                                text: number
                                color: editor.markup.pinInkColor
                                font.bold: true
                            }
                        }
                        TextField {
                            Layout.fillWidth: true
                            text: note
                            placeholderText: "What should change here?"
                            onTextEdited: editor.markup.setPinNote(number, text)
                            // Show a long note from its start, not scrolled to its end.
                            Component.onCompleted: cursorPosition = 0
                        }
                    }

                    Label {
                        anchors.fill: parent
                        visible: pinList.count === 0
                        wrapMode: Text.WordWrap
                        color: Theme.textDim
                        text: "Pick Pin and click the image to number a spot, then say what should change there."
                    }
                }

                TextArea {
                    id: promptField
                    Layout.fillWidth: true
                    Layout.preferredHeight: 90
                    wrapMode: TextArea.Wrap
                    placeholderText: "A note for the worker (optional)"
                }

                RowLayout {
                    Layout.fillWidth: true

                    Button {
                        text: "Cancel"
                        onClicked: editor.markup.close()
                    }
                    Item {
                        Layout.fillWidth: true
                    }
                    BusyIndicator {
                        Layout.preferredWidth: 22
                        Layout.preferredHeight: 22
                        running: editor.markup.isSending
                        visible: running
                    }
                    Button {
                        text: "Send to worker"
                        highlighted: true
                        enabled: !editor.markup.isSending && (editor.markup.hasMarks || promptField.text.trim() !== "")
                        onClicked: editor.markup.send(promptField.text)
                    }
                }
            }
        }
    }
}
