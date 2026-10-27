// The window's colours and sizes in one place, and the only QML that reads the theme file.
//
// The colour tokens come from ThemeFile (atelier.gui.theme): the user's theme file over the built-in theme, reloaded
// live (see docs/theming.md). Translucent and blended shades are derived here from those tokens. The colours of the
// scene under review (clay, the scale figure, collision hulls, the lights' ambient) are fixed instead, so a model
// reads the same whatever theme the window wears. Main.qml feeds the tokens to the Qt Quick Controls palette.
pragma Singleton
import QtQuick
import Atelier

QtObject {
    readonly property bool isDark: ThemeFile.mode === "dark"

    readonly property color window: ThemeFile.colors.window
    readonly property color panel: ThemeFile.colors.panel
    readonly property color panelRaised: ThemeFile.colors.panelRaised
    readonly property color field: ThemeFile.colors.field
    readonly property color text: ThemeFile.colors.text
    readonly property color textDim: ThemeFile.colors.textDim
    readonly property color selectionText: ThemeFile.colors.selectionText
    readonly property color accent: ThemeFile.colors.accent
    readonly property color border: ThemeFile.colors.border
    readonly property color selection: ThemeFile.colors.selection
    readonly property color danger: ThemeFile.colors.danger
    readonly property color warning: ThemeFile.colors.warning
    readonly property color success: ThemeFile.colors.success
    readonly property color viewportBackground: ThemeFile.colors.viewportBackground
    readonly property color viewportGrid: ThemeFile.colors.viewportGrid
    readonly property color viewportAxisX: ThemeFile.colors.viewportAxisX
    readonly property color viewportAxisY: ThemeFile.colors.viewportAxisY

    // Derived shades: overlays on the viewport, the drop target over the references, an approval waiting for you.
    readonly property color overlay: Qt.alpha(field, 0.8)
    readonly property color dropHighlight: Qt.alpha(selection, 0.2)
    readonly property color pendingSurface: Qt.tint(window, Qt.alpha(warning, 0.14))
    readonly property color shadow: Qt.darker(window, 3)

    // The scene's own colours, deliberately not themed.
    readonly property color clay: "#b9b9b9"
    readonly property color scaleFigure: "#7f8da8"
    readonly property color collision: "#55d17f"
    readonly property color sceneAmbient: "#29292e"

    readonly property int gap: 8
    readonly property int fontSmall: 11
    readonly property int fontNormal: 13
    readonly property int fontTitle: 15

    readonly property var statusColors: ({
        "draft": ThemeFile.colors.statusDraft,
        "in-review": ThemeFile.colors.statusInReview,
        "changes-requested": ThemeFile.colors.statusChangesRequested,
        "approved": ThemeFile.colors.statusApproved
    })
    readonly property var statusLabels: ({
        "draft": "Draft",
        "in-review": "In review",
        "changes-requested": "Changes requested",
        "approved": "Approved"
    })

    function statusColor(status) {
        return statusColors[status] || textDim
    }

    function statusLabel(status) {
        return statusLabels[status] || status
    }
}
