// The tools of a `Drawing` (gui/markup.py): which tool draws next, its colour and size, and undo, redo and clear.
// It flows into as many rows as its width needs, so it fits the markup editor's side panel and focus mode's toolbar.
import QtQuick
import QtQuick.Controls

Flow {
    id: tools

    // The `Drawing` the tools act on.
    required property var drawing
    // The tools on offer, in order; focus mode leaves pins out, since the reviewer's words carry the notes.
    property var toolNames: ["pen", "highlighter", "eraser", "arrow", "box", "pin"]
    readonly property var toolLabels: ({
        "pen": "Pen",
        "highlighter": "Highlighter",
        "eraser": "Eraser",
        "arrow": "Arrow",
        "box": "Box",
        "pin": "Pin"
    })
    readonly property var sizeLabels: ({"small": "S", "medium": "M", "large": "L"})
    // A key shown with each tool and action, where the page around the tools gives it one.
    property var shortcutHints: ({})

    function hint(name) {
        return shortcutHints[name] ? " (" + shortcutHints[name] + ")" : ""
    }

    spacing: 4

    // Undo and redo answer to the usual keys while the tools are on screen. A text field with the keyboard keeps its
    // own undo, because it takes the shortcut before the window does.
    Shortcut {
        sequences: [StandardKey.Undo]
        enabled: tools.visible
        onActivated: tools.drawing.undo()
    }
    Shortcut {
        sequences: [StandardKey.Redo]
        enabled: tools.visible
        onActivated: tools.drawing.redo()
    }

    Repeater {
        model: tools.toolNames

        ToolButton {
            required property string modelData
            text: tools.toolLabels[modelData]
            checkable: true
            checked: tools.drawing.tool === modelData
            focusPolicy: Qt.NoFocus
            onClicked: tools.drawing.setTool(modelData)
            ToolTip.visible: hovered && tools.hint(modelData) !== ""
            ToolTip.text: tools.toolLabels[modelData] + tools.hint(modelData)
        }
    }

    // Colours, sizes and history each wrap as a group, so a narrow panel never splits one across lines.
    Row {
        leftPadding: 10

        Repeater {
            model: tools.drawing.colors

            // A swatch as tall as a tool button, so a row of them lines up with the buttons.
            Item {
                id: swatch
                required property string modelData
                width: 28
                height: 36

                Rectangle {
                    anchors.centerIn: parent
                    width: 24
                    height: 24
                    radius: 12
                    color: swatch.modelData
                    border.color: tools.drawing.color === swatch.modelData ? Theme.text : Theme.border
                    border.width: tools.drawing.color === swatch.modelData ? 3 : 1
                }

                MouseArea {
                    anchors.fill: parent
                    onClicked: tools.drawing.setColor(swatch.modelData)
                }
            }
        }
    }

    Row {
        leftPadding: 10

        Repeater {
            model: tools.drawing.sizes

            ToolButton {
                required property string modelData
                text: tools.sizeLabels[modelData]
                checkable: true
                checked: tools.drawing.size === modelData
                focusPolicy: Qt.NoFocus
                onClicked: tools.drawing.setSize(modelData)
                ToolTip.visible: hovered
                ToolTip.text: "Line size " + modelData + tools.hint(modelData)
            }
        }
    }

    Row {
        leftPadding: 10

        ToolButton {
            text: "Undo"
            enabled: tools.drawing.canUndo
            focusPolicy: Qt.NoFocus
            onClicked: tools.drawing.undo()
            ToolTip.visible: hovered
            ToolTip.text: "Undo (Ctrl+Z)"
        }
        ToolButton {
            text: "Redo"
            enabled: tools.drawing.canRedo
            focusPolicy: Qt.NoFocus
            onClicked: tools.drawing.redo()
            ToolTip.visible: hovered
            ToolTip.text: "Redo (Ctrl+Shift+Z)"
        }
        ToolButton {
            text: "Clear"
            enabled: tools.drawing.hasMarks
            focusPolicy: Qt.NoFocus
            onClicked: tools.drawing.clear()
        }
    }
}
