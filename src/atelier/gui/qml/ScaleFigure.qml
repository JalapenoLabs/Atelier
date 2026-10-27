// A 180 cm human stand-in for judging scale, standing on the ground (Unreal Z = 0) with its feet at its origin.
// Built from Qt's primitive meshes, which are 100 units (cm here) across at scale 1, in Qt's Y-up scene frame.
import QtQuick
import QtQuick3D

Node {
    id: figure

    readonly property real heightCm: 180

    PrincipledMaterial {
        id: figureMaterial
        baseColor: Theme.scaleFigure
        roughness: 0.8
        opacity: 0.85
        alphaMode: PrincipledMaterial.Blend
    }

    // Legs: 84 cm, 14 cm thick.
    Model {
        source: "#Cylinder"
        position: Qt.vector3d(-11, 42, 0)
        scale: Qt.vector3d(0.14, 0.84, 0.14)
        materials: [figureMaterial]
    }
    Model {
        source: "#Cylinder"
        position: Qt.vector3d(11, 42, 0)
        scale: Qt.vector3d(0.14, 0.84, 0.14)
        materials: [figureMaterial]
    }

    // Torso: hips to shoulders, 84 to 150 cm.
    Model {
        source: "#Cube"
        position: Qt.vector3d(0, 117, 0)
        scale: Qt.vector3d(0.42, 0.66, 0.22)
        materials: [figureMaterial]
    }

    // Arms: shoulder to wrist, 62 cm.
    Model {
        source: "#Cylinder"
        position: Qt.vector3d(-27, 118, 0)
        scale: Qt.vector3d(0.09, 0.62, 0.09)
        materials: [figureMaterial]
    }
    Model {
        source: "#Cylinder"
        position: Qt.vector3d(27, 118, 0)
        scale: Qt.vector3d(0.09, 0.62, 0.09)
        materials: [figureMaterial]
    }

    // Neck and head: the head's top is at exactly 180 cm.
    Model {
        source: "#Cylinder"
        position: Qt.vector3d(0, 154, 0)
        scale: Qt.vector3d(0.1, 0.08, 0.1)
        materials: [figureMaterial]
    }
    Model {
        source: "#Sphere"
        position: Qt.vector3d(0, 168, 0)
        scale: Qt.vector3d(0.21, 0.24, 0.23)
        materials: [figureMaterial]
    }
}
