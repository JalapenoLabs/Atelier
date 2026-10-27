// The 3D viewport: the selected export on a grid in Unreal units, beside a 180 cm figure, navigated like Blender
// (ViewportNavigation.qml), with studio or clay lighting, wireframe, collision hulls and camera bookmarks.
//
// The stage has one primary viewport, bound to the studio's model, with its controls and remembered settings. Compare
// shows two more, each given its own model and stats, sharing one camera (see ComparePage.qml).
//
// Frames. Qt Quick 3D's scene is Y up; Unreal is Z up. Both load paths (the FBX through assimp, the .glb from
// Blender) arrive Y up in centimetres, so a Qt scene point (x, y, z) is the Unreal point (x, z, y). Bookmarks and
// the stats are kept in Unreal's frame and converted only here.
import QtCore
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick3D
import QtQuick3D.AssetUtils
import QtQuick3D.Helpers
import Atelier

Item {
    id: viewport

    // What to show. The primary viewport follows the studio's stage; compare's viewports are given their own.
    property bool isPrimary: true
    property string modelUrl: studio.modelUrl
    property var modelStats: studio.stats
    // Framing happens once per key: reloading the same export keeps the camera.
    property string frameKey: studio.selectedExport
    property bool framesOnLoad: true
    // Where the view looks from. Compare gives both its viewports one camera, so they always show the same view.
    property ViewportCamera viewCamera: ownCamera
    // Whether the numpad and F keys drive this viewport.
    property bool handlesKeys: isPrimary && visible

    // Display settings, remembered per machine; a launch flag overrides one for this run.
    property string lighting: "studio"
    property bool wireframe: false
    property bool showCollision: false
    property bool showScale: true
    property bool showGrid: true
    property bool preferDirect: false

    // The loaded model's meshes, and each one's own materials so clay can be undone.
    property var renderModels: []
    property var collisionModels: []
    property var originalMaterials: []
    property vector3d boundsMin: Qt.vector3d(0, 0, 0)
    property vector3d boundsMax: Qt.vector3d(0, 0, 0)
    property bool hasModel: false
    // The export the camera was last framed on. Reloading the same export (the .glb replacing the direct load, or
    // the file rewritten on disk) keeps the reviewer's camera; a different export is framed afresh.
    property string framedExport: ""
    readonly property real largestExtent: Math.max(boundsMax.x - boundsMin.x, boundsMax.y - boundsMin.y,
                                                   boundsMax.z - boundsMin.z)
    // A 10 cm grid suits props; anything over 3 m reads better on a 1 m grid.
    readonly property real gridIntervalCm: largestExtent > 300 ? 100 : 10

    property int loadAttempts: 0

    // Only the primary viewport remembers its display settings; compare's take them from it. Each setting is copied
    // in once at start and written back on every change.
    readonly property var rememberedNames: ["lighting", "wireframe", "showCollision", "showScale", "showGrid",
                                            "preferDirect"]

    Loader {
        id: rememberedLoader
        active: viewport.isPrimary
        sourceComponent: Settings {
            category: "viewport"
            property string lighting: "studio"
            property bool wireframe: false
            property bool showCollision: false
            property bool showScale: true
            property bool showGrid: true
            property bool preferDirect: false
        }
    }

    function remember() {
        const remembered = rememberedLoader.item
        if (!remembered)
            return
        for (const name of rememberedNames)
            remembered[name] = viewport[name]
    }

    onWireframeChanged: remember()
    onShowScaleChanged: remember()
    onShowGridChanged: remember()

    Component.onCompleted: {
        if (!isPrimary) {
            reload()
            return
        }
        const remembered = rememberedLoader.item
        for (const name of rememberedNames)
            viewport[name] = remembered[name]
        for (const name of ["lighting", "wireframe", "collision", "direct"]) {
            if (launchOptions[name] === undefined)
                continue
            const target = {"collision": "showCollision", "direct": "preferDirect"}[name] || name
            viewport[target] = launchOptions[name]
        }
        studio.setDirectPreferred(preferDirect)
        reload()
    }

    onPreferDirectChanged: {
        remember()
        if (isPrimary)
            studio.setDirectPreferred(preferDirect)
    }
    onLightingChanged: {
        remember()
        applyDisplay()
    }
    onShowCollisionChanged: {
        remember()
        applyDisplay()
    }
    onModelStatsChanged: {
        if (hasModel)
            updateBounds()
    }
    onModelUrlChanged: {
        if (!isPrimary)
            reload()
    }

    // The primary viewport reloads on every new model revision, even when the URL is unchanged (the file was
    // rewritten on disk).
    Connections {
        target: studio
        enabled: viewport.isPrimary
        function onModelChanged() {
            viewport.reload()
        }
    }

    function toQt(unrealPoint) {
        return Qt.vector3d(unrealPoint.x, unrealPoint.z, unrealPoint.y)
    }

    function toUnreal(qtPoint) {
        return Qt.vector3d(qtPoint.x, qtPoint.z, qtPoint.y)
    }

    // Load whatever the studio has put on the stage. Clearing the source first makes an unchanged URL reload.
    function reload() {
        hasModel = false
        renderModels = []
        collisionModels = []
        originalMaterials = []
        loader.source = ""
        if (!modelUrl)
            return
        loader.source = modelUrl
        loadAttempts = 0
        Qt.callLater(afterLoad)
    }

    function afterLoad() {
        if (loader.status === RuntimeLoader.Empty && loadAttempts++ < 10) {
            Qt.callLater(afterLoad)
            return
        }
        if (loader.status !== RuntimeLoader.Success) {
            reportLoad(false, loader.errorString || "the model did not load")
            return
        }

        const found = {render: [], collision: []}
        collectModels(loader, found)
        if (found.render.length === 0) {
            reportLoad(false, "the file loaded but holds no meshes")
            return
        }
        renderModels = found.render
        collisionModels = found.collision
        originalMaterials = found.render.map(model => Array.from(model.materials))
        applyDisplay()
        hasModel = true
        updateBounds()
        reportLoad(true, "")
    }

    // Only the stage's model decides the load path and readiness; compare's report nothing.
    function reportLoad(isLoaded, errorText) {
        if (isPrimary)
            studio.reportModelLoad(isLoaded, errorText)
        else if (!isLoaded)
            console.warn("Compare could not load " + modelUrl + ": " + errorText)
    }

    // Bounds come from Blender's measurement once it has arrived: it leaves collision hulls out and is the same
    // for both load paths. Until then they are measured from the loaded meshes, whose bounds Qt only knows once
    // a frame has rendered, so the measurement repeats each frame until it finds them.
    function updateBounds() {
        const stats = modelStats
        if (stats.boundsMin !== undefined) {
            boundsMin = toQt(Qt.vector3d(stats.boundsMin[0], stats.boundsMin[1], stats.boundsMin[2]))
            boundsMax = toQt(Qt.vector3d(stats.boundsMax[0], stats.boundsMax[1], stats.boundsMax[2]))
        } else {
            measureBounds()
        }

        const hasBounds = boundsMax.x > boundsMin.x || boundsMax.y > boundsMin.y || boundsMax.z > boundsMin.z
        if (hasBounds && framesOnLoad && framedExport !== frameKey) {
            frameThreeQuarter(launchOptions.frame === "figure")
            if (launchOptions.view)
                viewCamera.showAxisView(launchOptions.view)
            framedExport = frameKey
        }
        boundsWatch.running = !hasBounds
    }

    function collectModels(node, found) {
        for (let index = 0; index < node.children.length; ++index) {
            const child = node.children[index]
            if (child.geometry !== undefined && child.materials !== undefined) {
                const isCollision = (child.objectName || "").startsWith("UCX_")
                ;(isCollision ? found.collision : found.render).push(child)
            }
            collectModels(child, found)
        }
    }

    // The scene-space bounds of the render meshes (collision hulls left out), from each mesh's own bounds.
    function measureBounds() {
        let minimum = Qt.vector3d(Infinity, Infinity, Infinity)
        let maximum = Qt.vector3d(-Infinity, -Infinity, -Infinity)
        for (const model of renderModels) {
            const local = model.bounds
            for (let corner = 0; corner < 8; ++corner) {
                const point = model.mapPositionToScene(Qt.vector3d(
                    corner & 1 ? local.maximum.x : local.minimum.x,
                    corner & 2 ? local.maximum.y : local.minimum.y,
                    corner & 4 ? local.maximum.z : local.minimum.z))
                minimum = Qt.vector3d(Math.min(minimum.x, point.x), Math.min(minimum.y, point.y),
                                      Math.min(minimum.z, point.z))
                maximum = Qt.vector3d(Math.max(maximum.x, point.x), Math.max(maximum.y, point.y),
                                      Math.max(maximum.z, point.z))
            }
        }
        if (minimum.x > maximum.x)
            return
        boundsMin = minimum
        boundsMax = maximum
    }

    function applyDisplay() {
        for (let index = 0; index < renderModels.length; ++index)
            renderModels[index].materials = lighting === "clay" ? [clayMaterial] : originalMaterials[index]
        for (const hull of collisionModels) {
            hull.materials = [collisionMaterial]
            hull.visible = showCollision
        }
    }

    // Fit the model's bounds in view from where the camera looks now (Home, numpad .). With `withFigure`, the
    // 180 cm figure beside it is framed too, to judge the model's size against it.
    function frame(withFigure) {
        let minimum = boundsMin
        let maximum = boundsMax
        if (withFigure) {
            const figureMin = Qt.vector3d(scaleFigure.x - 30, 0, scaleFigure.z - 15)
            const figureMax = Qt.vector3d(scaleFigure.x + 30, scaleFigure.heightCm, scaleFigure.z + 15)
            minimum = Qt.vector3d(Math.min(minimum.x, figureMin.x), Math.min(minimum.y, figureMin.y),
                                  Math.min(minimum.z, figureMin.z))
            maximum = Qt.vector3d(Math.max(maximum.x, figureMax.x), Math.max(maximum.y, figureMax.y),
                                  Math.max(maximum.z, figureMax.z))
        }
        viewCamera.frame(minimum.plus(maximum).times(0.5), maximum.minus(minimum).length() / 2)
    }

    // Frame from the default three-quarter view, on the -X -Y side and slightly above (F, and on first showing).
    function frameThreeQuarter(withFigure) {
        viewCamera.resetDirection()
        frame(withFigure)
    }

    function applyBookmark(targetX, targetY, targetZ, yaw, pitch, distance) {
        viewCamera.place(toQt(Qt.vector3d(targetX, targetY, targetZ)), yaw, pitch, distance)
    }

    function saveBookmark(name) {
        const target = toUnreal(viewCamera.pivot)
        studio.saveBookmark(name, target.x, target.y, target.z, viewCamera.yaw, viewCamera.pitch, viewCamera.distance)
    }

    // Freeze the 3D view as it is, camera recorded, and open it in the markup editor.
    function markUpFrame() {
        const framePath = studio.markupEditor.newFramePath()
        const target = toUnreal(viewCamera.pivot)
        const yaw = viewCamera.yaw
        const pitch = viewCamera.pitch
        const distance = viewCamera.distance
        const label = studio.selectedName + " viewport frame"
        view.grabToImage(result => {
            if (!result.saveToFile(framePath)) {
                console.warn("Could not save the viewport frame to " + framePath)
                return
            }
            studio.markupEditor.openFrame(framePath, label, target.x, target.y, target.z, yaw, pitch, distance)
        })
    }

    ViewportCamera {
        id: ownCamera
    }

    View3D {
        id: view
        anchors.fill: parent
        camera: viewport.viewCamera.isOrthographic ? orthographicCamera : perspectiveCamera

        environment: SceneEnvironment {
            clearColor: Theme.viewportBackground
            backgroundMode: SceneEnvironment.Color
            antialiasingMode: SceneEnvironment.MSAA
            antialiasingQuality: SceneEnvironment.High
            debugSettings: DebugSettings {
                wireframeEnabled: viewport.wireframe
            }

            // Unreal units: 1 = 1 cm, on the ground plane (Unreal Z = 0).
            InfiniteGrid {
                visible: viewport.showGrid
                gridInterval: viewport.gridIntervalCm
            }
        }

        // The orbit pivot, never drawn. The cameras and the light rig hang off it, so the lighting stays the same
        // from every side: a key from above left, a fill from the right, a rim from behind.
        Node {
            id: orbitOrigin
            position: viewport.viewCamera.pivot
            eulerRotation: Qt.vector3d(viewport.viewCamera.pitch, viewport.viewCamera.yaw, 0)

            PerspectiveCamera {
                id: perspectiveCamera
                z: viewport.viewCamera.distance
                fieldOfView: viewport.viewCamera.fieldOfView
                clipNear: Math.max(0.1, z * 0.002)
                clipFar: Math.max(100000, z * 50)
            }

            // Scaled to show the same height at the pivot as perspective does, so switching keeps the model's size.
            // It stands well back along the same line, so nothing between it and the pivot is clipped.
            OrthographicCamera {
                id: orthographicCamera
                z: viewport.viewCamera.distance + 50000
                horizontalMagnification: verticalMagnification
                // Before the first layout the view is 0 px high, and a magnification must be positive.
                verticalMagnification: Math.max(view.height, 1) / viewport.viewCamera.viewHeight
                clipNear: 1
                clipFar: z * 2
            }

            DirectionalLight {
                eulerRotation: Qt.vector3d(-38, -32, 0)
                brightness: 1.15
                ambientColor: Qt.rgba(0.16, 0.16, 0.18, 1)
            }
            DirectionalLight {
                eulerRotation: Qt.vector3d(-8, 55, 0)
                brightness: 0.45
            }
            DirectionalLight {
                eulerRotation: Qt.vector3d(-25, 165, 0)
                brightness: 0.8
            }
        }

        // Off the model's +X -Y corner (Qt z is Unreal Y), feet on the ground. From the default camera on the
        // -X -Y side that corner is about as far away as the model's centre, so the figure reads at true scale.
        ScaleFigure {
            id: scaleFigure
            visible: viewport.showScale && viewport.hasModel
            x: viewport.boundsMax.x + 40
            z: viewport.boundsMin.z - 40
        }

        RuntimeLoader {
            id: loader
        }

        PrincipledMaterial {
            id: clayMaterial
            baseColor: "#b9b9b9"
            roughness: 0.75
            metalness: 0
        }

        PrincipledMaterial {
            id: collisionMaterial
            baseColor: "#55d17f"
            opacity: 0.28
            alphaMode: PrincipledMaterial.Blend
            cullMode: Material.NoCulling
            lighting: PrincipledMaterial.NoLighting
        }
    }

    ViewportNavigation {
        anchors.fill: parent
        viewCamera: viewport.viewCamera
        handlesKeys: viewport.handlesKeys
        onFrameRequested: viewport.frame(false)
        onThreeQuarterFrameRequested: (withFigure) => viewport.frameThreeQuarter(withFigure)
    }

    FrameAnimation {
        id: boundsWatch
        running: false
        onTriggered: viewport.updateBounds()
    }

    StatsOverlay {
        visible: viewport.isPrimary && studio.selectedExport !== ""
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.margins: Theme.gap * 1.5
        gridIntervalCm: viewport.gridIntervalCm
        collisionCount: viewport.collisionModels.length
    }

    ViewportToolbar {
        visible: viewport.isPrimary
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: Theme.gap * 1.5
        viewport: viewport
    }

    ColumnLayout {
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: Theme.gap * 1.5
        spacing: 2

        // Which view this is, as Blender's viewport says it: "Front orthographic", "User perspective".
        Label {
            visible: viewport.hasModel
            text: viewport.viewCamera.viewLabel
            color: Theme.text
            font.pixelSize: Theme.fontSmall
        }

        Label {
            visible: viewport.isPrimary
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            text: "Middle or left drag to orbit, Shift+drag or right drag to pan, Ctrl+drag or wheel to zoom.  "
                  + "Numpad: 1 front, 3 right, 7 top (Ctrl+ opposite), 5 orthographic, 2 4 6 8 orbit, 9 flip, "
                  + ". or Home to frame.  F three-quarter view, Shift+F with the figure."
            color: Theme.textDim
            font.pixelSize: Theme.fontSmall
        }
    }

    BusyIndicator {
        anchors.centerIn: parent
        visible: running
        running: viewport.isPrimary
                 ? !viewport.hasModel && studio.selectedExport !== "" && studio.conversionState === "running"
                 : !viewport.hasModel && viewport.visible
    }
}
