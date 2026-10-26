// The left pane: the project's folders, sources and their exports as a tree, with a text and a status filter.
// The keyboard's row lives in the studio rather than in the ListView, because every open or close rebuilds the rows.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    color: Theme.panel

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: Theme.gap
        spacing: Theme.gap

        Label {
            text: "Sources"
            font.pixelSize: Theme.fontTitle
            font.bold: true
            color: Theme.text
        }

        TextField {
            Layout.fillWidth: true
            placeholderText: "Filter by name, path or export"
            text: studio.filterText
            onTextEdited: studio.filterText = text
        }

        ComboBox {
            id: statusFilter
            Layout.fillWidth: true
            model: [""].concat(studio.statusValues)
            displayText: currentValue ? Theme.statusLabel(currentValue) : "All statuses"
            delegate: ItemDelegate {
                required property string modelData
                width: statusFilter.width
                text: modelData ? Theme.statusLabel(modelData) : "All statuses"
            }
            onActivated: studio.statusFilter = currentValue
        }

        ListView {
            id: tree
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: studio.sourceRows
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}
            activeFocusOnTab: true
            // The keys below drive the studio's cursor instead.
            keyNavigationEnabled: false

            Keys.onUpPressed: studio.moveTreeCursor(-1)
            Keys.onDownPressed: studio.moveTreeCursor(1)
            Keys.onLeftPressed: studio.collapseAtTreeCursor()
            Keys.onRightPressed: studio.expandAtTreeCursor()
            Keys.onReturnPressed: studio.activateTreeCursor()
            Keys.onEnterPressed: studio.activateTreeCursor()
            Keys.onSpacePressed: studio.activateTreeCursor()
            Keys.onPressed: (event) => {
                const pageStep = Math.max(1, Math.floor(tree.height / 28) - 1)
                if (event.key === Qt.Key_PageUp)
                    studio.moveTreeCursor(-pageStep)
                else if (event.key === Qt.Key_PageDown)
                    studio.moveTreeCursor(pageStep)
                else if (event.key === Qt.Key_Home)
                    studio.moveTreeCursor(-tree.count)
                else if (event.key === Qt.Key_End)
                    studio.moveTreeCursor(tree.count)
                else
                    return
                event.accepted = true
            }

            Connections {
                target: studio
                function onTreeCursorChanged() {
                    // After the rows' rebuild has laid out again, so the cursor's row exists to scroll to.
                    Qt.callLater(() => {
                        if (studio.treeCursorIndex >= 0)
                            tree.positionViewAtIndex(studio.treeCursorIndex, ListView.Contain)
                    })
                }
            }

            delegate: ItemDelegate {
                id: row
                required property int index
                required property string key
                required property string kind
                required property string name
                required property int depth
                required property string source
                required property bool is_expandable
                required property bool is_expanded
                required property string status
                required property int export_count

                readonly property bool isFolder: kind === "folder"
                readonly property bool isSource: kind === "source"
                readonly property bool isSelectedSource: source !== "" && source === studio.selectedSource

                width: tree.width
                height: 28
                // The tree keeps the keyboard focus, so a click never leaves it on a row.
                focusPolicy: Qt.NoFocus
                // An open selected source shows its selection on the export row instead.
                highlighted: kind === "export" ? isSelectedSource && key === studio.selectedExport
                                               : isSource && isSelectedSource && !is_expanded
                onClicked: {
                    tree.forceActiveFocus()
                    studio.activateTreeRow(key)
                }
                onDoubleClicked: {
                    if (isSource)
                        studio.toggleTreeRow(key)
                }

                // The keyboard's row, drawn apart from the selection.
                Rectangle {
                    anchors.fill: parent
                    visible: tree.activeFocus && row.index === studio.treeCursorIndex
                    color: "transparent"
                    border.color: Theme.accent
                    border.width: 1
                    radius: 2
                }

                contentItem: RowLayout {
                    spacing: 6

                    Item {
                        Layout.preferredWidth: row.depth * 14
                    }

                    Label {
                        Layout.preferredWidth: 12
                        text: row.is_expandable ? (row.is_expanded ? "▾" : "▸") : ""
                        color: Theme.textDim

                        // The arrow opens and closes without selecting.
                        MouseArea {
                            anchors.fill: parent
                            anchors.margins: -4
                            enabled: row.is_expandable
                            onClicked: {
                                tree.forceActiveFocus()
                                studio.toggleTreeRow(row.key)
                            }
                        }
                    }

                    Rectangle {
                        visible: row.isSource
                        Layout.preferredWidth: 8
                        Layout.preferredHeight: 8
                        radius: 4
                        color: Theme.statusColor(row.status)
                    }

                    Label {
                        Layout.fillWidth: true
                        text: row.name
                        elide: Text.ElideRight
                        color: row.isSource ? Theme.text : Theme.textDim
                        font.bold: row.isFolder || (row.isSource && row.isSelectedSource)
                    }

                    Label {
                        visible: row.isSource && row.export_count > 0
                        text: row.export_count
                        color: Theme.textDim
                        font.pixelSize: Theme.fontSmall
                    }
                }

                ToolTip.visible: hovered && !isFolder
                ToolTip.delay: 800
                ToolTip.text: isSource ? key + "\n" + Theme.statusLabel(status) + ", " + export_count + " exports" : key
            }

            Label {
                anchors.centerIn: parent
                visible: tree.count === 0
                text: studio.projectName ? "No sources match" : "Pick a project above"
                color: Theme.textDim
            }
        }
    }
}
