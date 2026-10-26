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
}
