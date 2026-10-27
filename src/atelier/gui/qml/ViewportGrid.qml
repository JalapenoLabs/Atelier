// The viewport's backdrop: the theme's background colour and the ground grid with its axes, in Unreal units.
//
// It is a View3D of its own, placed under the model's (as its child at z -1, so grabbing the model's view takes it
// along), seen through cameras that follow the model view's perspective and orthographic ones. Two reasons: Qt's
// InfiniteGrid has fixed colours, so the grid is a plane with a shader (shaders/grid.frag) that takes the theme's;
// and a plane in the model's own scene would turn into its two triangles' edges under the wireframe debug mode. The
// model's view clears to transparent, so the model is always drawn over the grid.
import QtQuick
import QtQuick3D

View3D {
    id: backdrop

    required property PerspectiveCamera perspectiveSource
    required property OrthographicCamera orthographicSource
    property bool isOrthographic: false
    property real gridIntervalCm: 10
    property bool showGrid: true

    // Far enough that the grid reaches the horizon from a model-sized orbit, scaled up as the camera climbs.
    readonly property real fadeDistance: Math.max(gridIntervalCm * 500, Math.abs(followCamera.y) * 25)

    camera: isOrthographic ? followOrthographic : followCamera
    environment: SceneEnvironment {
        clearColor: Theme.viewportBackground
        backgroundMode: SceneEnvironment.Color
        antialiasingMode: SceneEnvironment.MSAA
        antialiasingQuality: SceneEnvironment.High
    }

    PerspectiveCamera {
        id: followCamera
        position: backdrop.perspectiveSource.scenePosition
        rotation: backdrop.perspectiveSource.sceneRotation
        fieldOfView: backdrop.perspectiveSource.fieldOfView
        clipNear: backdrop.perspectiveSource.clipNear
        clipFar: backdrop.perspectiveSource.clipFar
    }

    OrthographicCamera {
        id: followOrthographic
        position: backdrop.orthographicSource.scenePosition
        rotation: backdrop.orthographicSource.sceneRotation
        horizontalMagnification: backdrop.orthographicSource.horizontalMagnification
        verticalMagnification: backdrop.orthographicSource.verticalMagnification
        clipNear: backdrop.orthographicSource.clipNear
        clipFar: backdrop.orthographicSource.clipFar
    }

    // Qt's #Rectangle is 100 units across in its XY plane; laid flat and kept under the camera, it covers the grid's
    // whole reach. The lines are measured in scene coordinates, so moving the plane never moves them.
    Model {
        visible: backdrop.showGrid
        source: "#Rectangle"
        x: followCamera.x
        z: followCamera.z
        eulerRotation.x: -90
        scale: Qt.vector3d(backdrop.fadeDistance / 50, backdrop.fadeDistance / 50, 1)
        materials: CustomMaterial {
            shadingMode: CustomMaterial.Unshaded
            sourceBlend: CustomMaterial.One
            destinationBlend: CustomMaterial.OneMinusSrcAlpha
            cullMode: Material.NoCulling
            vertexShader: "shaders/grid.vert"
            fragmentShader: "shaders/grid.frag"

            property real interval: backdrop.gridIntervalCm
            property real fadeDistance: backdrop.fadeDistance
            property color gridColor: Theme.viewportGrid
            property color axisXColor: Theme.viewportAxisX
            property color axisYColor: Theme.viewportAxisY
        }
    }
}
