// The stats overlay in the viewport's corner: triangles, materials and bounds (from Blender's measurement of the
// export), and which load path put the model on screen.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: overlay
    property real gridIntervalCm: 10
    property int collisionCount: 0

    readonly property var stats: studio.stats
    readonly property bool hasStats: stats.triangles !== undefined

    function formatCount(value) {
        return Number(value).toLocaleString(Qt.locale(), "f", 0)
    }

    function formatCm(value) {
        return Number(value).toLocaleString(Qt.locale(), "f", value < 100 ? 1 : 0)
    }

    implicitWidth: column.implicitWidth + 20
    implicitHeight: column.implicitHeight + 14
    radius: 6
    color: Theme.overlay

    ColumnLayout {
        id: column
        anchors.centerIn: parent
        spacing: 2

        Label {
            visible: overlay.hasStats
            text: overlay.formatCount(overlay.stats.triangles) + " tris   "
                  + overlay.stats.materials + (overlay.stats.materials === 1 ? " material" : " materials")
            color: Theme.text
            font.pixelSize: Theme.fontNormal
        }

        Label {
            visible: overlay.hasStats
            text: overlay.formatCm(overlay.stats.sizeX) + " × " + overlay.formatCm(overlay.stats.sizeY)
                  + " × " + overlay.formatCm(overlay.stats.sizeZ) + " cm  (X × Y × Z)"
            color: Theme.text
            font.pixelSize: Theme.fontNormal
        }

        Label {
            visible: overlay.hasStats && (overlay.stats.collisionHulls > 0 || overlay.stats.isSkinned)
            text: [overlay.stats.isSkinned ? "skinned" : "",
                   overlay.stats.collisionHulls > 0
                   ? overlay.stats.collisionHulls + (overlay.stats.collisionHulls === 1 ? " collision hull"
                                                                                        : " collision hulls")
                   : ""].filter(part => part).join(", ")
            color: Theme.textDim
            font.pixelSize: Theme.fontSmall
        }

        Label {
            visible: studio.conversionState === "running" || studio.conversionState === "failed"
            text: studio.conversionState === "running" ? "Measuring with Blender…" : "Blender could not convert this export"
            color: studio.conversionState === "failed" ? Theme.danger : Theme.textDim
            font.pixelSize: Theme.fontSmall
        }

        Label {
            text: (studio.loadPath ? "Loaded as " + studio.loadPath : "Waiting for Blender") + ",  grid "
                  + overlay.gridIntervalCm + " cm"
            color: Theme.textDim
            font.pixelSize: Theme.fontSmall
        }
    }
}
