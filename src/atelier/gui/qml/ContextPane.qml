// The right pane: the selected source's worker chat, the project's references rail, and the source's README.
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

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        TabBar {
            id: tabs
            Layout.fillWidth: true

            TabButton {
                text: "Chat"
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

            Item {
                Label {
                    anchors.centerIn: parent
                    width: parent.width - 40
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    color: Theme.textDim
                    text: "The worker chat for this source appears here once Claude Code workers are set up."
                }
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
