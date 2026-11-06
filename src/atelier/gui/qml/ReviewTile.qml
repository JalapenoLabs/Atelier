// One ticket in the review grid: its hero image (a thumbnail), id, summary, tags and delivery round, its checkbox, and
// its decision job's badge. Selection and clicks belong to the grid (ReviewPage.qml); the tile only draws them, apart
// from the checkbox and Retry on a failed job, which take their own clicks.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: tile

    required property int index
    required property string ticket
    required property string summary
    required property var tags
    required property string thumbnail_url
    required property string hero_note
    required property string round_label
    required property string job_state
    required property string job_label
    required property string job_error
    required property bool is_selected

    property bool isCursor: false
    // The grid has keyboard focus, so the cursor shows as the focus colour.
    property bool isGridFocused: false

    readonly property color jobColor: Theme.jobStateColor(job_state)

    Rectangle {
        id: card
        anchors.fill: parent
        anchors.margins: Theme.gap / 2
        radius: 6
        color: tile.is_selected ? Theme.selection : Theme.panelRaised
        border.width: tile.isCursor ? 2 : 1
        border.color: tile.isCursor && tile.isGridFocused ? Theme.accent : Theme.border

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 6
            spacing: 4

            Rectangle {
                Layout.fillWidth: true
                Layout.fillHeight: true
                radius: 4
                color: Theme.viewportBackground
                clip: true

                Image {
                    id: hero
                    anchors.fill: parent
                    source: tile.thumbnail_url
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    // The thumbnail is already about tile size; no need to keep a second copy of it decoded.
                    cache: false
                    smooth: true
                }

                Label {
                    anchors.centerIn: parent
                    width: parent.width - 16
                    visible: tile.thumbnail_url === "" || hero.status === Image.Error
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    text: tile.hero_note
                    color: Theme.textDim
                    font.pixelSize: Theme.fontSmall
                }

                // Top left, as the first thing on the row. The click is taken here rather than by the box, which would
                // not say which modifiers were held, and the box only shows what the board says.
                CheckBox {
                    objectName: "tileCheckbox"
                    anchors.top: parent.top
                    anchors.left: parent.left
                    anchors.margins: 4
                    padding: 2
                    checked: tile.is_selected
                    focusPolicy: Qt.NoFocus
                    background: Rectangle {
                        radius: 4
                        color: tile.is_selected ? Theme.accent : Theme.overlay
                        border.color: Theme.textDim
                    }

                    MouseArea {
                        anchors.fill: parent
                        cursorShape: Qt.PointingHandCursor
                        onClicked: mouse => studio.reviewBoard.checkTile(tile.index, mouse.modifiers)
                    }
                }

                // The decision job's state, top right; a failed one can be retried and says why on hover.
                Rectangle {
                    anchors.top: parent.top
                    anchors.right: parent.right
                    anchors.margins: 6
                    visible: tile.job_state !== ""
                    width: badgeRow.implicitWidth + 12
                    height: badgeRow.implicitHeight + 6
                    radius: height / 2
                    color: Theme.overlay
                    border.color: tile.jobColor

                    RowLayout {
                        id: badgeRow
                        anchors.centerIn: parent
                        spacing: 6

                        Label {
                            text: tile.job_label
                            color: tile.jobColor
                            font.pixelSize: Theme.fontSmall
                            font.bold: true
                        }
                        Label {
                            visible: tile.job_state === "failed"
                            text: "Retry"
                            color: Theme.accent
                            font.pixelSize: Theme.fontSmall
                            font.bold: true
                            font.underline: retryHover.hovered

                            HoverHandler {
                                id: retryHover
                                cursorShape: Qt.PointingHandCursor
                            }
                            TapHandler {
                                onTapped: studio.reviewBoard.retryJob(tile.ticket)
                            }
                        }
                    }

                    HoverHandler {
                        id: badgeHover
                    }
                    ToolTip.visible: badgeHover.hovered && tile.job_error !== ""
                    ToolTip.text: tile.job_error
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 6

                Label {
                    text: tile.ticket
                    color: tile.is_selected ? Theme.selectionText : Theme.text
                    font.pixelSize: Theme.fontNormal
                    font.bold: true
                }
                Label {
                    Layout.fillWidth: true
                    text: tile.summary
                    color: tile.is_selected ? Theme.selectionText : Theme.textDim
                    font.pixelSize: Theme.fontSmall
                    elide: Text.ElideRight
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 6

                Flow {
                    Layout.fillWidth: true
                    Layout.preferredHeight: tagLabelMetrics.height + 4
                    spacing: 4
                    clip: true

                    Repeater {
                        model: tile.tags

                        delegate: Rectangle {
                            required property string modelData
                            width: tagLabel.implicitWidth + 10
                            height: tagLabel.implicitHeight + 4
                            radius: 3
                            color: Theme.field

                            Label {
                                id: tagLabel
                                anchors.centerIn: parent
                                text: parent.modelData
                                color: Theme.textDim
                                font.pixelSize: Theme.fontSmall
                            }
                        }
                    }
                }
                Label {
                    visible: text !== ""
                    text: tile.round_label
                    color: tile.is_selected ? Theme.selectionText : Theme.textDim
                    font.pixelSize: Theme.fontSmall
                }
            }
        }
    }

    TextMetrics {
        id: tagLabelMetrics
        font.pixelSize: Theme.fontSmall
        text: "Tag"
    }
}
