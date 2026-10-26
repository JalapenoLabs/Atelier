// The centre pane: the selected source's header, the stage (3D viewport, a render full size, compare, or the
// markup editor) and, under it, the render gallery and the revision filmstrip.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: stagePane
    color: Theme.window

    function restoreSplit(state) {
        stageSplit.restoreState(state)
    }

    function saveSplit() {
        return stageSplit.saveState()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Source header: name, status, export picker.
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 44
            color: Theme.panel
            visible: studio.selectedSource !== ""

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                spacing: 10

                Label {
                    text: studio.selectedName
                    font.pixelSize: Theme.fontTitle
                    font.bold: true
                    color: Theme.text
                }

                ComboBox {
                    id: statusPicker
                    Layout.preferredWidth: 190
                    model: studio.statusValues
                    currentIndex: studio.statusValues.indexOf(studio.selectedStatus)
                    displayText: Theme.statusLabel(currentText)
                    delegate: ItemDelegate {
                        required property string modelData
                        width: statusPicker.width
                        text: Theme.statusLabel(modelData)
                    }
                    onActivated: studio.setSourceStatus(currentValue)

                    Rectangle {
                        anchors.left: parent.left
                        anchors.leftMargin: -2
                        anchors.verticalCenter: parent.verticalCenter
                        width: 4
                        height: parent.height - 8
                        radius: 2
                        color: Theme.statusColor(studio.selectedStatus)
                    }
                }

                Item {
                    Layout.fillWidth: true
                }

                Label {
                    text: "Export"
                    color: Theme.textDim
                    visible: exportPicker.visible
                }

                ComboBox {
                    id: exportPicker
                    Layout.preferredWidth: 280
                    visible: exportPicker.count > 0
                    model: studio.exportRows
                    textRole: "name"
                    valueRole: "path"
                    onActivated: studio.selectExport(currentValue)

                    Connections {
                        target: studio
                        function onExportChanged() {
                            exportPicker.currentIndex = exportPicker.indexOfValue(studio.selectedExport)
                        }
                    }
                }
            }
        }

        SplitView {
            id: stageSplit
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Vertical

            StackLayout {
                SplitView.fillHeight: true
                SplitView.minimumHeight: 240
                currentIndex: studio.markupEditor.isOpen ? 4
                              : studio.isComparing ? 3
                              : studio.stageImage !== "" ? 1
                              : studio.selectedExport !== "" ? 0 : 2

                Viewport {
                    id: primaryViewport
                }

                ImageStage {
                }

                Rectangle {
                    color: Theme.viewportBackground

                    Label {
                        anchors.centerIn: parent
                        color: Theme.textDim
                        text: studio.selectedSource === ""
                              ? "Select a source"
                              : "This source has no exports or renders yet"
                    }
                }

                ComparePage {
                    primaryViewport: primaryViewport
                }

                MarkupEditor {
                }
            }

            RenderGallery {
                SplitView.preferredHeight: 150
                SplitView.minimumHeight: 90
            }

            Filmstrip {
                SplitView.preferredHeight: 150
                SplitView.minimumHeight: 90
            }
        }
    }
}
