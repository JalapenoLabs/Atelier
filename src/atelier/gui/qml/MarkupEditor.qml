// The markup editor: a render or a frozen viewport frame under a drawing layer. Pen, arrow and box strokes in a few
// colours, numbered pins with notes, undo and clear, and Send, which stores the capture as feedback and hands it to
// the source's worker with the note typed beside it.
//
// Marks are kept in coordinates from 0 to 1 across the image (in `studio.markupEditor`), so the canvas redraws them
// at any size and the flattened PNG the worker reads matches what is on screen.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: editor
    readonly property var markup: studio.markupEditor
    // The stroke being drawn, in image coordinates from 0 to 1.
    property var draft: []
    readonly property var toolNames: ["pen", "arrow", "box", "pin"]
    readonly property var toolLabels: ({"pen": "Pen", "arrow": "Arrow", "box": "Box", "pin": "Pin"})

    color: Theme.viewportBackground

    function toImage(x, y) {
        return [(x - image.paintedX) / image.paintedWidth, (y - image.paintedY) / image.paintedHeight]
    }

    function isOnImage(point) {
        return point[0] >= 0 && point[0] <= 1 && point[1] >= 0 && point[1] <= 1
    }

    RowLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            Image {
                id: image
                anchors.fill: parent
                anchors.margins: Theme.gap
                source: editor.markup.imageUrl
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                mipmap: true
                smooth: true
                // Where the picture is drawn inside the item, which is letterboxed around it.
                readonly property real paintedX: (width - paintedWidth) / 2
                readonly property real paintedY: (height - paintedHeight) / 2
                onStatusChanged: canvas.requestPaint()
            }

            Canvas {
                id: canvas
                anchors.fill: image

                Connections {
                    target: editor.markup
                    function onChanged() {
                        canvas.requestPaint()
                    }
                }
                onWidthChanged: requestPaint()
                onHeightChanged: requestPaint()

                function toCanvas(point) {
                    return [image.paintedX + point[0] * image.paintedWidth, image.paintedY + point[1] * image.paintedHeight]
                }

                function drawStroke(context, tool, color, points) {
                    if (points.length < 2)
                        return
                    const size = Math.max(image.paintedWidth, image.paintedHeight)
                    context.strokeStyle = color
                    context.lineWidth = Math.max(2, 0.005 * size)
                    context.lineCap = "round"
                    context.lineJoin = "round"
                    const first = toCanvas(points[0])
                    const last = toCanvas(points[points.length - 1])
                    context.beginPath()
                    if (tool === "pen") {
                        context.moveTo(first[0], first[1])
                        for (let index = 1; index < points.length; index++) {
                            const next = toCanvas(points[index])
                            context.lineTo(next[0], next[1])
                        }
                    } else if (tool === "box") {
                        context.rect(Math.min(first[0], last[0]), Math.min(first[1], last[1]),
                                     Math.abs(last[0] - first[0]), Math.abs(last[1] - first[1]))
                    } else if (tool === "arrow") {
                        context.moveTo(first[0], first[1])
                        context.lineTo(last[0], last[1])
                        const angle = Math.atan2(last[1] - first[1], last[0] - first[0])
                        const head = 0.025 * size
                        for (const side of [-1, 1]) {
                            const barb = angle + Math.PI + side * 28 * Math.PI / 180
                            context.moveTo(last[0], last[1])
                            context.lineTo(last[0] + head * Math.cos(barb), last[1] + head * Math.sin(barb))
                        }
                    }
                    context.stroke()
                }

                onPaint: {
                    const context = getContext("2d")
                    context.reset()
                    if (image.status !== Image.Ready)
                        return
                    for (const stroke of editor.markup.strokes)
                        drawStroke(context, stroke.tool, stroke.color, stroke.points)
                    drawStroke(context, editor.markup.tool, editor.markup.color, editor.draft)
                }

                // Pins are items rather than paint, so they stay crisp and can be told apart.
                Repeater {
                    model: editor.markup.pinRows

                    Rectangle {
                        required property int number
                        required property real position_x
                        required property real position_y
                        readonly property real pinRadius: Math.max(10, 0.016 * Math.max(image.paintedWidth, image.paintedHeight))
                        readonly property var centre: canvas.toCanvas([position_x, position_y])
                        x: centre[0] - pinRadius
                        y: centre[1] - pinRadius
                        width: pinRadius * 2
                        height: pinRadius * 2
                        radius: pinRadius
                        color: editor.markup.pinColor
                        border.color: editor.markup.pinInkColor
                        border.width: Math.max(1.5, pinRadius * 0.18)

                        Label {
                            anchors.centerIn: parent
                            text: parent.number
                            color: editor.markup.pinInkColor
                            font.bold: true
                            font.pixelSize: parent.pinRadius * 1.2
                        }
                    }
                }
            }

            MouseArea {
                anchors.fill: image
                enabled: image.status === Image.Ready && !editor.markup.isSending
                cursorShape: Qt.CrossCursor

                onPressed: mouse => {
                    const point = editor.toImage(mouse.x, mouse.y)
                    if (!editor.isOnImage(point))
                        return
                    if (editor.markup.tool === "pin") {
                        editor.markup.addPin(point[0], point[1])
                        return
                    }
                    editor.draft = [point]
                }
                onPositionChanged: mouse => {
                    if (editor.draft.length === 0)
                        return
                    const point = editor.toImage(mouse.x, mouse.y)
                    const clamped = [Math.min(1, Math.max(0, point[0])), Math.min(1, Math.max(0, point[1]))]
                    editor.draft = editor.markup.tool === "pen" ? editor.draft.concat([clamped]) : [editor.draft[0], clamped]
                    canvas.requestPaint()
                }
                onReleased: {
                    if (editor.draft.length > 1)
                        editor.markup.addStroke(editor.markup.tool, editor.draft)
                    editor.draft = []
                    canvas.requestPaint()
                }
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

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 2

                    Repeater {
                        model: editor.toolNames

                        ToolButton {
                            required property string modelData
                            text: editor.toolLabels[modelData]
                            checkable: true
                            checked: editor.markup.tool === modelData
                            onClicked: editor.markup.setTool(modelData)
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6

                    Repeater {
                        model: editor.markup.colors

                        Rectangle {
                            required property string modelData
                            width: 24
                            height: 24
                            radius: 12
                            color: modelData
                            border.color: editor.markup.color === modelData ? Theme.text : "transparent"
                            border.width: 2

                            MouseArea {
                                anchors.fill: parent
                                onClicked: editor.markup.setColor(parent.modelData)
                            }
                        }
                    }
                    Item {
                        Layout.fillWidth: true
                    }
                    ToolButton {
                        text: "Undo"
                        enabled: editor.markup.canUndo
                        onClicked: editor.markup.undo()
                    }
                    ToolButton {
                        text: "Clear"
                        enabled: editor.markup.canUndo
                        onClicked: editor.markup.clear()
                    }
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
                        enabled: !editor.markup.isSending && (editor.markup.canUndo || promptField.text.trim() !== "")
                        onClicked: editor.markup.send(promptField.text)
                    }
                }
            }
        }
    }
}
