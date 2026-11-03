// The drawing layer over an image: the image fitted to the item, the strokes and pins of a `Drawing` (gui/markup.py)
// over it, and the mouse drawing the next. The markup editor and review focus mode both draw through it.
//
// Marks are kept in coordinates from 0 to 1 across the image, so the canvas redraws them at any size and a flattened
// PNG matches what is on screen. The canvas is a transparent layer over the image: an eraser stroke clears the marks
// under it (`destination-out`) and leaves the image alone, exactly as flattening does.
import QtQuick
import QtQuick.Controls

Item {
    id: surface

    // The `Drawing` shown and drawn on.
    required property var drawing
    // Whether the mouse draws; off while the drawing is being sent.
    property bool interactive: true
    // The stroke being drawn, in image coordinates from 0 to 1.
    property var draft: []
    readonly property bool isImageReady: image.status === Image.Ready

    function toImage(x, y) {
        return [(x - image.paintedX) / image.paintedWidth, (y - image.paintedY) / image.paintedHeight]
    }

    function isOnImage(point) {
        return point[0] >= 0 && point[0] <= 1 && point[1] >= 0 && point[1] <= 1
    }

    Image {
        id: image
        anchors.fill: parent
        source: surface.drawing.imageUrl
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
            target: surface.drawing
            function onChanged() {
                canvas.requestPaint()
            }
        }
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()

        function toCanvas(point) {
            return [image.paintedX + point[0] * image.paintedWidth, image.paintedY + point[1] * image.paintedHeight]
        }

        function drawStroke(context, tool, color, width, points) {
            if (points.length < 2)
                return
            const size = Math.max(image.paintedWidth, image.paintedHeight)
            context.globalAlpha = tool === "highlighter" ? surface.drawing.highlighterOpacity : 1
            context.globalCompositeOperation = tool === "eraser" ? "destination-out" : "source-over"
            // An eraser clears whatever it covers, so its colour does not matter.
            context.strokeStyle = color
            context.lineWidth = Math.max(tool === "pen" ? 2 : 1, width * size)
            context.lineCap = "round"
            context.lineJoin = "round"
            const first = toCanvas(points[0])
            const last = toCanvas(points[points.length - 1])
            context.beginPath()
            if (tool === "box") {
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
            } else {
                // A pen, highlighter or eraser: the whole path, stroked once so a highlighter never darkens itself.
                context.moveTo(first[0], first[1])
                for (let index = 1; index < points.length; index++) {
                    const next = toCanvas(points[index])
                    context.lineTo(next[0], next[1])
                }
            }
            context.stroke()
        }

        onPaint: {
            const context = getContext("2d")
            context.reset()
            if (image.status !== Image.Ready)
                return
            for (const stroke of surface.drawing.strokes)
                drawStroke(context, stroke.tool, stroke.color, stroke.width, stroke.points)
            drawStroke(context, surface.drawing.tool, surface.drawing.color, surface.drawing.strokeWidth, surface.draft)
            context.globalAlpha = 1
            context.globalCompositeOperation = "source-over"
        }

        // Pins are items rather than paint, so they stay crisp, sit above every stroke, and can be told apart.
        Repeater {
            model: surface.drawing.pinRows

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
                color: surface.drawing.pinColor
                border.color: surface.drawing.pinInkColor
                border.width: Math.max(1.5, pinRadius * 0.18)

                Label {
                    anchors.centerIn: parent
                    text: parent.number
                    color: surface.drawing.pinInkColor
                    font.bold: true
                    font.pixelSize: parent.pinRadius * 1.2
                }
            }
        }
    }

    MouseArea {
        anchors.fill: image
        enabled: image.status === Image.Ready && surface.interactive
        cursorShape: Qt.CrossCursor

        onPressed: mouse => {
            const point = surface.toImage(mouse.x, mouse.y)
            if (!surface.isOnImage(point))
                return
            if (surface.drawing.tool === "pin") {
                surface.drawing.addPin(point[0], point[1])
                return
            }
            surface.draft = [point]
        }
        onPositionChanged: mouse => {
            if (surface.draft.length === 0)
                return
            const point = surface.toImage(mouse.x, mouse.y)
            const clamped = [Math.min(1, Math.max(0, point[0])), Math.min(1, Math.max(0, point[1]))]
            const isPath = ["pen", "highlighter", "eraser"].includes(surface.drawing.tool)
            surface.draft = isPath ? surface.draft.concat([clamped]) : [surface.draft[0], clamped]
            canvas.requestPaint()
        }
        onReleased: {
            if (surface.draft.length > 1)
                surface.drawing.addStroke(surface.drawing.tool, surface.draft)
            surface.draft = []
            canvas.requestPaint()
        }
    }
}
