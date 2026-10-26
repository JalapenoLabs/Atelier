// The window's colours and sizes in one place. The Fusion palette set in app.py uses the same colours.
pragma Singleton
import QtQuick

QtObject {
    readonly property color window: "#1b1e24"
    readonly property color panel: "#20242c"
    readonly property color panelRaised: "#272b35"
    readonly property color field: "#15171c"
    readonly property color border: "#30353f"
    readonly property color text: "#e4e7ee"
    readonly property color textDim: "#9aa1b2"
    readonly property color accent: "#6aa8ff"
    readonly property color selection: "#2d4a73"
    readonly property color viewportBackground: "#2a2e36"
    readonly property color overlay: "#cc15171c"

    readonly property int gap: 8
    readonly property int fontSmall: 11
    readonly property int fontNormal: 13
    readonly property int fontTitle: 15

    readonly property var statusColors: ({
        "draft": "#8a909c",
        "in-review": "#e0b341",
        "changes-requested": "#e0645a",
        "approved": "#5cc285"
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
