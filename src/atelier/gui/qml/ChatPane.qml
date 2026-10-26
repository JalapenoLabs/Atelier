// The Chat tab: the selected source's worker, its current session drawn natively (prompts, the worker's text, tool
// calls with their results and images, approval cards, turn results), and the composer. The chat sticks to its end
// while it streams, unless you scroll up to read.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: chatPane
    readonly property var chat: studio.chatModel
    readonly property var statusColors: ({
        "stopped": Theme.textDim,
        "starting": Theme.statusColor("in-review"),
        "idle": Theme.statusColor("approved"),
        "running": Theme.accent,
        "failed": Theme.statusColor("changes-requested")
    })

    Label {
        anchors.centerIn: parent
        visible: chatPane.chat.source === ""
        color: Theme.textDim
        text: "Select a source to talk to its worker."
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0
        visible: chatPane.chat.source !== ""

        // The worker's state and its actions.
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: header.implicitHeight + 12
            color: Theme.panelRaised

            ColumnLayout {
                id: header
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: 10
                anchors.rightMargin: 6
                spacing: 2

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 6

                    Rectangle {
                        width: 9
                        height: 9
                        radius: 4.5
                        color: chatPane.statusColors[chatPane.chat.status] || Theme.textDim
                    }
                    Label {
                        Layout.fillWidth: true
                        text: chatPane.chat.statusLabel
                        elide: Text.ElideRight
                        color: Theme.text
                    }
                    ToolButton {
                        text: "Blender"
                        onClicked: chatPane.chat.showInBlender()
                        ToolTip.visible: hovered
                        ToolTip.text: "Show the source's .blend in a windowed Blender (on Hyprland, in the silent special:atelier workspace)"
                    }
                    ToolButton {
                        text: "New session"
                        enabled: !chatPane.chat.isTurnRunning
                        onClicked: chatPane.chat.newSession()
                        ToolTip.visible: hovered
                        ToolTip.text: "Start the next prompt in a fresh Claude Code session"
                    }
                    ToolButton {
                        text: "Stop"
                        enabled: chatPane.chat.status !== "stopped"
                        onClicked: chatPane.chat.stopWorker()
                        ToolTip.visible: hovered
                        ToolTip.text: "Stop this worker's Claude Code and Blender; the next prompt resumes the session"
                    }
                }

                Label {
                    text: chatPane.chat.sessionLabel
                          + (chatPane.chat.blenderPort ? "   Blender MCP on :" + chatPane.chat.blenderPort : "")
                    color: Theme.textDim
                    font.pixelSize: Theme.fontSmall
                }
            }
        }

        ListView {
            id: transcript
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            spacing: 8
            topMargin: 8
            bottomMargin: 8
            model: chatPane.chat.rows
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar {}

            property bool isStuckToEnd: true
            onMovementEnded: isStuckToEnd = atYEnd
            onCountChanged: if (isStuckToEnd) Qt.callLater(positionViewAtEnd)
            onContentHeightChanged: if (isStuckToEnd) Qt.callLater(positionViewAtEnd)

            delegate: Item {
                id: entry
                required property int index
                required property string kind
                required property string text
                required property string detail
                required property string tool_name
                required property string tool_summary
                required property string tool_output
                required property bool is_error
                required property bool is_done
                required property var image_urls
                required property string approval_id
                required property string approval_status
                required property string revision_label
                required property string time

                width: transcript.width
                height: loader.item ? loader.item.implicitHeight : 0

                Loader {
                    id: loader
                    x: 10
                    width: entry.width - 20
                    sourceComponent: entry.kind === "user" ? userEntry
                                     : entry.kind === "assistant" ? assistantEntry
                                     : entry.kind === "tool" ? toolEntry
                                     : entry.kind === "approval" ? approvalEntry
                                     : noteEntry
                }

                Component {
                    id: userEntry

                    Item {
                        implicitHeight: bubble.height

                        Rectangle {
                            id: bubble
                            anchors.right: parent.right
                            width: Math.min(parent.width * 0.88, userColumn.implicitWidth + 20)
                            height: userColumn.implicitHeight + 16
                            radius: 8
                            color: Theme.selection

                            ColumnLayout {
                                id: userColumn
                                anchors.fill: parent
                                anchors.margins: 8
                                spacing: 6

                                Label {
                                    Layout.fillWidth: true
                                    Layout.maximumWidth: chatPane.width * 0.8
                                    text: entry.text
                                    wrapMode: Text.Wrap
                                    color: Theme.text
                                }
                                Thumbnails {
                                    urls: entry.image_urls
                                }
                                Label {
                                    id: briefToggle
                                    property bool isOpen: false
                                    text: (isOpen ? "▾ " : "▸ ") + "Brief  " + entry.time
                                    color: Theme.textDim
                                    font.pixelSize: Theme.fontSmall

                                    MouseArea {
                                        anchors.fill: parent
                                        onClicked: briefToggle.isOpen = !briefToggle.isOpen
                                    }
                                }
                                Label {
                                    Layout.fillWidth: true
                                    Layout.maximumWidth: chatPane.width * 0.8
                                    visible: briefToggle.isOpen
                                    text: entry.detail
                                    wrapMode: Text.Wrap
                                    color: Theme.textDim
                                    font.pixelSize: Theme.fontSmall
                                }
                            }
                        }
                    }
                }

                Component {
                    id: assistantEntry

                    Label {
                        text: entry.text
                        wrapMode: Text.Wrap
                        textFormat: Text.MarkdownText
                        color: Theme.text
                        onLinkActivated: link => Qt.openUrlExternally(link)
                    }
                }

                Component {
                    id: toolEntry

                    ColumnLayout {
                        spacing: 4

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6

                            Label {
                                text: !entry.is_done ? "…" : entry.is_error ? "✕" : "✓"
                                color: !entry.is_done ? Theme.accent
                                       : entry.is_error ? Theme.statusColor("changes-requested")
                                       : Theme.statusColor("approved")
                                font.bold: true
                            }
                            Label {
                                text: entry.tool_name.replace(/^mcp__(\w+)__/, "$1 · ")
                                color: Theme.text
                                font.bold: true
                                font.pixelSize: Theme.fontSmall
                            }
                            Label {
                                id: summary
                                Layout.fillWidth: true
                                property bool isOpen: false
                                text: entry.tool_summary
                                elide: Text.ElideRight
                                color: Theme.textDim
                                font.family: "monospace"
                                font.pixelSize: Theme.fontSmall

                                MouseArea {
                                    anchors.fill: parent
                                    enabled: entry.tool_output !== ""
                                    cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                                    onClicked: summary.isOpen = !summary.isOpen
                                }
                            }
                        }
                        Label {
                            Layout.fillWidth: true
                            visible: summary.isOpen || (entry.is_error && entry.tool_output !== "")
                            text: entry.tool_output
                            wrapMode: Text.WrapAnywhere
                            maximumLineCount: summary.isOpen ? 40 : 3
                            elide: Text.ElideRight
                            color: entry.is_error ? Theme.statusColor("changes-requested") : Theme.textDim
                            font.family: "monospace"
                            font.pixelSize: Theme.fontSmall
                        }
                        Thumbnails {
                            urls: entry.image_urls
                        }
                    }
                }

                Component {
                    id: approvalEntry

                    ApprovalCard {
                        approvalId: entry.approval_id
                        toolName: entry.tool_name
                        toolSummary: entry.tool_summary
                        reason: entry.text
                        status: entry.approval_status
                        answerNote: entry.tool_output
                    }
                }

                Component {
                    id: noteEntry

                    Label {
                        horizontalAlignment: Text.AlignHCenter
                        text: entry.kind === "result" ? "— " + entry.text + " · " + entry.time + " —" : entry.text
                        wrapMode: Text.Wrap
                        color: entry.is_error ? Theme.statusColor("changes-requested") : Theme.textDim
                        font.pixelSize: Theme.fontSmall
                        font.italic: entry.kind === "notice"
                    }
                }
            }

            Label {
                anchors.centerIn: parent
                width: parent.width - 40
                visible: transcript.count === 0
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                text: "No session yet. Your first prompt starts this source's worker: its own background Blender and a Claude Code session in the project."
                color: Theme.textDim
            }
        }

        Label {
            Layout.fillWidth: true
            Layout.leftMargin: 10
            Layout.rightMargin: 10
            visible: chatPane.chat.pinnedReferences.length > 0
            text: "Pinned to the next prompt: " + chatPane.chat.pinnedReferences.map(path => path.split("/").pop()).join(", ")
            elide: Text.ElideRight
            color: Theme.accent
            font.pixelSize: Theme.fontSmall
        }

        // The composer: Enter sends, Shift+Enter starts a new line.
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: composer.implicitHeight + 12
            color: Theme.panelRaised

            RowLayout {
                id: composer
                anchors.fill: parent
                anchors.margins: 6
                spacing: 6

                ScrollView {
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(120, Math.max(40, promptField.implicitHeight))

                    TextArea {
                        id: promptField
                        placeholderText: chatPane.chat.isTurnRunning ? "The worker is busy; interrupt it to redirect"
                                                                     : "Tell the worker what to change…"
                        wrapMode: TextArea.Wrap
                        Keys.onReturnPressed: event => {
                            if (event.modifiers & Qt.ShiftModifier) {
                                event.accepted = false
                                return
                            }
                            sendButton.clicked()
                        }
                    }
                }

                Button {
                    id: sendButton
                    visible: !chatPane.chat.isTurnRunning
                    text: "Send"
                    highlighted: true
                    enabled: promptField.text.trim() !== ""
                    onClicked: {
                        chatPane.chat.send(promptField.text)
                        promptField.clear()
                    }
                }
                Button {
                    visible: chatPane.chat.isTurnRunning
                    text: "Interrupt"
                    onClicked: chatPane.chat.interrupt()
                }
            }
        }
    }

    // A row of image thumbnails; clicking one opens it full size outside the app.
    component Thumbnails: Flow {
        property var urls: []
        Layout.fillWidth: true
        visible: urls.length > 0
        spacing: 4

        Repeater {
            model: parent.urls

            Image {
                required property string modelData
                source: modelData
                height: 140
                width: Math.min(220, implicitWidth * height / Math.max(1, implicitHeight))
                fillMode: Image.PreserveAspectFit
                asynchronous: true
                sourceSize.height: 280

                MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    onClicked: Qt.openUrlExternally(parent.source)
                }
            }
        }
    }
}
