// The right pane: the selected source's worker chat, the project's references rail, and the source's README.
// Sending feedback from the markup editor switches it to the chat, where the turn it started streams.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: contextPane
    property alias currentTab: tabs.currentIndex
    readonly property var tabNames: ["chat", "refs", "readme"]

    color: Theme.panel

    Component.onCompleted: {
        if (launchOptions.tab)
            tabs.currentIndex = tabNames.indexOf(launchOptions.tab)
    }

    Connections {
        target: studio.markupEditor
        function onSent() {
            tabs.currentIndex = 0
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        TabBar {
            id: tabs
            Layout.fillWidth: true

            TabButton {
                text: studio.chatModel.isTurnRunning ? "Chat •" : "Chat"
            }
            TabButton {
                text: "Refs"
            }
            TabButton {
                text: "README"
            }
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: tabs.currentIndex

            ChatPane {
            }

            ReferencesRail {
            }

            ScrollView {
                id: readmeScroll
                clip: true
                contentWidth: availableWidth

                Label {
                    width: readmeScroll.availableWidth
                    padding: 12
                    wrapMode: Text.Wrap
                    textFormat: Text.MarkdownText
                    color: Theme.text
                    text: studio.readmeText || (studio.selectedSource ? "*This source has no README.*" : "")
                    onLinkActivated: link => Qt.openUrlExternally(link)
                }
            }
        }
    }
}
