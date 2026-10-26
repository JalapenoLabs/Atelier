// The left pane: the project's sources as a tree mirroring its folders, with a text and a status filter.
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
            placeholderText: "Filter by name or path"
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

            delegate: ItemDelegate {
                id: row
                required property string key
                required property string name
                required property int depth
                required property bool is_folder
                required property bool is_expanded
                required property string status
                required property int export_count

                width: tree.width
                height: 28
                highlighted: !is_folder && key === studio.selectedSource
                onClicked: is_folder ? studio.toggleFolder(key) : studio.selectSource(key)

                contentItem: RowLayout {
                    spacing: 6

                    Item {
                        Layout.preferredWidth: row.depth * 14
                    }

                    Label {
                        Layout.preferredWidth: 12
                        text: row.is_folder ? (row.is_expanded ? "▾" : "▸") : ""
                        color: Theme.textDim
                    }

                    Rectangle {
                        visible: !row.is_folder
                        Layout.preferredWidth: 8
                        Layout.preferredHeight: 8
                        radius: 4
                        color: Theme.statusColor(row.status)
                    }

                    Label {
                        Layout.fillWidth: true
                        text: row.name
                        elide: Text.ElideRight
                        color: row.is_folder ? Theme.textDim : Theme.text
                        font.bold: row.is_folder
                    }

                    Label {
                        visible: !row.is_folder && row.export_count > 0
                        text: row.export_count
                        color: Theme.textDim
                        font.pixelSize: Theme.fontSmall
                    }
                }

                ToolTip.visible: hovered && !is_folder
                ToolTip.delay: 800
                ToolTip.text: key + "\n" + Theme.statusLabel(status) + ", " + export_count + " exports"
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
