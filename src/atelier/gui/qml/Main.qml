// The review window: a header with the project picker over three panes (sources, stage, context).
// Panel sizes, the context tab and the viewport's display settings are remembered per machine.
import QtCore
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    title: "Atelier"
    // Sized here rather than from Python: a hidden window (the screenshot path) ignores a later resize.
    width: launchOptions.windowWidth
    height: launchOptions.windowHeight
    color: Theme.window
    // Shown by app.py, unless it is rendering a screenshot of the hidden window.
    visible: false

    palette {
        window: Theme.window
        windowText: Theme.text
        base: Theme.field
        alternateBase: Theme.panelRaised
        text: Theme.text
        button: Theme.panelRaised
        buttonText: Theme.text
        highlight: Theme.selection
        highlightedText: "#ffffff"
        placeholderText: Theme.textDim
        toolTipBase: Theme.panelRaised
        toolTipText: Theme.text
        light: Theme.border
        midlight: Theme.panelRaised
        mid: Theme.border
        dark: Theme.window
        shadow: "#000000"
    }

    header: HeaderBar {
    }

    Settings {
        id: windowSettings
        category: "window"
        property var mainSplitState
        property var stageSplitState
        property int contextTab: 0
        property string lastProject: ""
    }

    Component.onCompleted: {
        mainSplit.restoreState(windowSettings.mainSplitState)
        stage.restoreSplit(windowSettings.stageSplitState)
        if (!launchOptions.project && windowSettings.lastProject)
            studio.openProject(windowSettings.lastProject)
    }

    Component.onDestruction: {
        windowSettings.mainSplitState = mainSplit.saveState()
        windowSettings.stageSplitState = stage.saveSplit()
    }

    Connections {
        target: studio
        function onProjectChanged() {
            windowSettings.lastProject = studio.projectName
        }
    }

    SplitView {
        id: mainSplit
        anchors.fill: parent
        orientation: Qt.Horizontal

        SourcePane {
            SplitView.preferredWidth: 280
            SplitView.minimumWidth: 200
        }

        Stage {
            id: stage
            SplitView.fillWidth: true
            SplitView.minimumWidth: 400
        }

        ContextPane {
            SplitView.preferredWidth: 360
            SplitView.minimumWidth: 260
            currentTab: windowSettings.contextTab
            onCurrentTabChanged: windowSettings.contextTab = currentTab
        }
    }

    // Until the daemon answers, the window has nothing to show: say so, and offer to start one.
    Rectangle {
        anchors.fill: parent
        visible: studio.daemonState !== "connected"
        color: Theme.window

        MouseArea {
            anchors.fill: parent
        }

        ColumnLayout {
            anchors.centerIn: parent
            width: Math.min(520, parent.width - 80)
            spacing: 14

            Label {
                Layout.fillWidth: true
                horizontalAlignment: Text.AlignHCenter
                text: studio.daemonState === "missing" ? "atelierd is not running"
                      : studio.daemonState === "starting" ? "Starting atelierd…" : "Connecting to atelierd…"
                font.pixelSize: Theme.fontTitle + 4
                font.bold: true
                color: Theme.text
            }
            Label {
                Layout.fillWidth: true
                visible: studio.daemonState === "missing"
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                color: Theme.textDim
                text: "The catalog, revisions and Claude Code workers live in Atelier's daemon, so they keep running "
                      + "when this window closes. Start it here in the background, or run `atelierd run` yourself "
                      + "(or `systemctl --user start atelierd` once `atelierd install` has set up its service)."
            }
            Label {
                Layout.fillWidth: true
                visible: studio.daemonNote !== ""
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WrapAnywhere
                color: Theme.textDim
                font.pixelSize: Theme.fontSmall
                text: studio.daemonNote
            }
            BusyIndicator {
                Layout.alignment: Qt.AlignHCenter
                running: studio.daemonState !== "missing"
                visible: running
            }
            RowLayout {
                Layout.alignment: Qt.AlignHCenter
                visible: studio.daemonState === "missing"
                spacing: 10

                Button {
                    text: "Try again"
                    onClicked: studio.connectDaemon()
                }
                Button {
                    text: "Start atelierd"
                    highlighted: true
                    onClicked: studio.startDaemon()
                }
            }
        }
    }
}
