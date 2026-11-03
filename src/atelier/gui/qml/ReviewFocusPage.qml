// Focus mode: one ticket of the review queue large, decided from the keyboard (gui/focus.py, docs/viewer.md).
//
// The stage shows the delivery's images (hero first), the linked source's 3D view, its sounds and its other files,
// with a filmstrip to switch; a drawing replaces it while one is open. Beside it is the ticket's note: the reviewer's
// takes (each editable), the field being typed, the drawings the decision carries, and the delivery's own words.
//
// Keys, while no text field has the keyboard: Left and Right the previous and next ticket, Up and Down the previous and
// next entry of the filmstrip, A approve, C finish with changes, D draw on the image or the 3D view (D or Enter again
// keeps the drawing), B a blank canvas, N write in the note, Space play or pause a sound, Esc go back to the grid.
// Voice notes' keys (hold ` to talk, R retake, K checkpoint by default) are caught on the window by gui/voice.py, so
// they work while typing too; this page only tells it that focus mode is on screen.
// While drawing: P pen, H highlighter, E eraser, 1 to 3 the line size, Ctrl+Z and Ctrl+Shift+Z undo and redo, and
// Enter or Esc keep the drawing; only Discard drops it. Esc in a text field gives the keyboard back to the page.
//
// The images of this ticket and the next two are held decoded by invisible images (`prefetchSlots`), so the next
// ticket's images show at once.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtMultimedia

Rectangle {
    id: focusPage
    objectName: "reviewFocusPage"
    color: Theme.window

    readonly property var focusMode: studio.reviewFocus
    readonly property var tile: focusMode.tile
    readonly property var note: focusMode.note
    readonly property var drawing: focusMode.decisionDrawing
    readonly property bool isDrawing: drawing.isOpen
    readonly property string stageKind: isDrawing ? "drawing" : focusMode.currentKind

    // The stage's pages, by what it shows; anything else is the empty page.
    readonly property var stagePages: ({"image": 0, "model": 1, "audio": 2, "file": 3, "drawing": 4})

    function drawOrKeep() {
        if (isDrawing) {
            focusMode.finishDrawing()
            return
        }
        if (focusMode.currentKind === "model" && modelLoader.item) {
            const framePath = drawing.newFramePath()
            modelLoader.item.freezeFrame(framePath, () => focusMode.drawOnFrame(framePath))
            return
        }
        focusMode.drawOnCurrent()
    }

    function writeNote() {
        draftField.forceActiveFocus()
    }

    function togglePlayback() {
        if (focusMode.currentKind !== "audio")
            return
        if (player.playbackState === MediaPlayer.PlayingState)
            player.pause()
        else
            player.play()
    }

    // Takes spoken now go into this ticket's note.
    Binding {
        target: studio.voiceNotes
        property: "isFocusShown"
        value: focusPage.visible && focusPage.focusMode.isOpen
    }

    Connections {
        target: focusPage.focusMode
        function onWordsWanted() {
            focusPage.writeNote()
        }
    }

    Keys.onPressed: event => {
        // Ctrl belongs to the drawing tools' undo and redo shortcuts.
        if (event.modifiers & Qt.ControlModifier) {
            event.accepted = false
            return
        }
        const drawingKeys = {
            [Qt.Key_P]: () => drawing.setTool("pen"),
            [Qt.Key_H]: () => drawing.setTool("highlighter"),
            [Qt.Key_E]: () => drawing.setTool("eraser"),
            [Qt.Key_1]: () => drawing.setSize("small"),
            [Qt.Key_2]: () => drawing.setSize("medium"),
            [Qt.Key_3]: () => drawing.setSize("large"),
            [Qt.Key_Return]: () => focusMode.finishDrawing(),
            [Qt.Key_Enter]: () => focusMode.finishDrawing(),
            // The most reflexive key never throws a drawing away.
            [Qt.Key_Escape]: () => focusMode.finishDrawing()
        }
        const pageKeys = {
            [Qt.Key_Left]: () => focusMode.previousTicket(),
            [Qt.Key_Right]: () => focusMode.nextTicket(),
            [Qt.Key_Up]: () => focusMode.stepEntry(-1),
            [Qt.Key_Down]: () => focusMode.stepEntry(1),
            [Qt.Key_A]: () => focusMode.approve(),
            [Qt.Key_C]: () => focusMode.finishWithChanges(),
            [Qt.Key_D]: () => focusPage.drawOrKeep(),
            [Qt.Key_B]: () => focusMode.drawOnBlank(),
            [Qt.Key_N]: () => focusPage.writeNote(),
            [Qt.Key_Space]: () => focusPage.togglePlayback(),
            [Qt.Key_Escape]: () => focusMode.close()
        }
        const action = (isDrawing && drawingKeys[event.key]) || pageKeys[event.key]
        event.accepted = action !== undefined
        if (action)
            action()
    }

    MediaPlayer {
        id: player
        source: focusPage.focusMode.currentKind === "audio" ? focusPage.focusMode.currentUrl : ""
        audioOutput: AudioOutput {
        }
    }

    // The images of this ticket and the next two, held decoded so the stage finds them in Qt's image cache. Each slot
    // keeps its URL for as long as it is wanted, so moving on never lets go of an image about to be shown.
    Item {
        visible: false

        Repeater {
            model: focusPage.focusMode.prefetchSlotCount

            Image {
                required property int index
                source: focusPage.focusMode.prefetchSlots[index] || ""
                asynchronous: true
                // Qt's image cache tells images apart by how they fit too, so this matches the stage's image.
                fillMode: Image.PreserveAspectFit
            }
        }
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // The ticket, where it is in the tab, its decision's badge, and the decisions.
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 44
            color: Theme.panel

            Rectangle {
                anchors.bottom: parent.bottom
                width: parent.width
                height: 1
                color: Theme.border
            }

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 12
                spacing: 10

                Button {
                    text: "Back to the grid"
                    focusPolicy: Qt.NoFocus
                    onClicked: focusPage.focusMode.close()
                    ToolTip.visible: hovered
                    ToolTip.text: "Esc"
                }
                Label {
                    text: focusPage.focusMode.ticket
                    color: Theme.text
                    font.pixelSize: Theme.fontTitle
                    font.bold: true
                }
                Label {
                    Layout.fillWidth: true
                    text: focusPage.tile.summary || ""
                    color: Theme.textDim
                    font.pixelSize: Theme.fontNormal
                    elide: Text.ElideRight
                }
                Label {
                    visible: text !== ""
                    text: focusPage.tile.job_label || ""
                    color: Theme.text
                    font.pixelSize: Theme.fontSmall
                    topPadding: 4
                    bottomPadding: 4
                    leftPadding: 8
                    rightPadding: 8
                    background: Rectangle {
                        radius: 4
                        color: Qt.alpha(Theme.jobStateColor(focusPage.tile.job_state || ""), 0.35)
                    }
                    ToolTip.visible: badgeHover.hovered && (focusPage.tile.job_error || "") !== ""
                    ToolTip.text: focusPage.tile.job_error || ""

                    HoverHandler {
                        id: badgeHover
                    }
                }
                Label {
                    text: focusPage.focusMode.positionLabel
                    color: Theme.textDim
                    font.pixelSize: Theme.fontSmall
                }
                Button {
                    text: "Approve"
                    focusPolicy: Qt.NoFocus
                    enabled: !focusPage.focusMode.isDeciding
                    onClicked: focusPage.focusMode.approve()
                    ToolTip.visible: hovered
                    ToolTip.text: "A: approve, with the note and drawings if any, and open the next ticket"
                }
                Button {
                    text: "Finish with changes"
                    focusPolicy: Qt.NoFocus
                    enabled: !focusPage.focusMode.isDeciding
                    onClicked: focusPage.focusMode.finishWithChanges()
                    ToolTip.visible: hovered
                    ToolTip.text: "C: request changes with the note and drawings, and open the next ticket"
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: 0

            // The stage and its filmstrip.
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                spacing: 0

                // The drawing's tools, over the stage while a drawing is open.
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: drawingBar.implicitHeight + 12
                    visible: focusPage.isDrawing
                    color: Theme.panel

                    RowLayout {
                        id: drawingBar
                        anchors.fill: parent
                        anchors.margins: 6
                        anchors.leftMargin: 12
                        anchors.rightMargin: 12
                        spacing: 8

                        DrawingTools {
                            Layout.fillWidth: true
                            drawing: focusPage.drawing
                            toolNames: ["pen", "highlighter", "eraser", "arrow", "box"]
                            shortcutHints: ({
                                "pen": "P",
                                "highlighter": "H",
                                "eraser": "E",
                                "small": "1",
                                "medium": "2",
                                "large": "3"
                            })
                        }
                        Button {
                            text: "Discard"
                            focusPolicy: Qt.NoFocus
                            onClicked: focusPage.focusMode.discardDrawing()
                            ToolTip.visible: hovered
                            ToolTip.text: "Put the drawing away without keeping it"
                        }
                        Button {
                            text: "Keep drawing"
                            highlighted: true
                            focusPolicy: Qt.NoFocus
                            onClicked: focusPage.focusMode.finishDrawing()
                            ToolTip.visible: hovered
                            ToolTip.text: "D, Enter or Esc: keep it with this ticket's decision"
                        }
                    }
                }

                StackLayout {
                    id: stage
                    objectName: "focusStage"
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: focusPage.stagePages[focusPage.stageKind] ?? 5

                    // An image.
                    Rectangle {
                        color: Theme.viewportBackground

                        Image {
                            id: stageImage
                            anchors.fill: parent
                            anchors.margins: Theme.gap
                            source: focusPage.focusMode.currentKind === "image" ? focusPage.focusMode.currentUrl : ""
                            fillMode: Image.PreserveAspectFit
                            asynchronous: true
                            mipmap: true
                            smooth: true
                        }
                        BusyIndicator {
                            anchors.centerIn: parent
                            running: stageImage.status === Image.Loading
                            visible: running
                        }
                        Label {
                            anchors.centerIn: parent
                            visible: stageImage.status === Image.Error
                            text: "Could not show " + focusPage.focusMode.currentName
                            color: Theme.textDim
                        }
                        StageCaption {
                            text: focusPage.focusMode.currentName
                                  + (stageImage.status === Image.Ready
                                     ? "   " + stageImage.sourceSize.width + " × " + stageImage.sourceSize.height : "")
                        }
                    }

                    // The linked source's 3D view, on the export the ticket names.
                    Rectangle {
                        color: Theme.viewportBackground

                        Loader {
                            id: modelLoader
                            anchors.fill: parent
                            active: focusPage.focusMode.currentKind === "model"
                            sourceComponent: Viewport {
                                isPrimary: false
                                modelUrl: studio.modelUrl
                                modelStats: studio.stats
                                frameKey: studio.selectedExport
                                handlesKeys: visible
                            }
                        }
                        StageCaption {
                            text: focusPage.focusMode.currentName + "   D freezes this view to draw on"
                        }
                    }

                    // A sound.
                    Rectangle {
                        color: Theme.viewportBackground

                        ColumnLayout {
                            anchors.centerIn: parent
                            spacing: 12

                            Label {
                                Layout.alignment: Qt.AlignHCenter
                                text: focusPage.focusMode.currentName
                                color: Theme.text
                                font.pixelSize: Theme.fontTitle
                            }
                            Button {
                                Layout.alignment: Qt.AlignHCenter
                                text: player.playbackState === MediaPlayer.PlayingState ? "Pause" : "Play"
                                focusPolicy: Qt.NoFocus
                                onClicked: focusPage.togglePlayback()
                                ToolTip.visible: hovered
                                ToolTip.text: "Space"
                            }
                            Label {
                                Layout.alignment: Qt.AlignHCenter
                                visible: player.duration > 0
                                text: Math.floor(player.position / 1000) + " s of "
                                      + Math.round(player.duration / 1000) + " s"
                                color: Theme.textDim
                                font.pixelSize: Theme.fontSmall
                            }
                            Label {
                                Layout.alignment: Qt.AlignHCenter
                                visible: player.error !== MediaPlayer.NoError
                                text: "Could not play it: " + player.errorString
                                color: Theme.danger
                                font.pixelSize: Theme.fontSmall
                            }
                        }
                    }

                    // A file Atelier does not show.
                    Rectangle {
                        color: Theme.viewportBackground

                        Label {
                            anchors.centerIn: parent
                            text: focusPage.focusMode.currentName + " is a file Atelier does not show"
                            color: Theme.textDim
                        }
                    }

                    // A drawing on an image, a frozen 3D frame, or a blank canvas.
                    Rectangle {
                        color: Theme.viewportBackground

                        DrawingSurface {
                            objectName: "focusDrawingSurface"
                            anchors.fill: parent
                            anchors.margins: Theme.gap
                            drawing: focusPage.drawing
                        }
                        StageCaption {
                            text: "Drawing on " + focusPage.drawing.label
                        }
                    }

                    // Nothing to show.
                    Rectangle {
                        color: Theme.viewportBackground

                        Label {
                            anchors.centerIn: parent
                            width: Math.min(520, parent.width - 40)
                            horizontalAlignment: Text.AlignHCenter
                            wrapMode: Text.WordWrap
                            text: focusPage.focusMode.emptyNote + ". B opens a blank canvas to sketch on."
                            color: Theme.textDim
                        }
                    }
                }

                // The filmstrip: everything the stage can show for this ticket.
                Rectangle {
                    Layout.fillWidth: true
                    implicitHeight: 112
                    visible: filmstrip.count > 1
                    color: Theme.panel

                    ListView {
                        id: filmstrip
                        objectName: "focusFilmstrip"
                        anchors.fill: parent
                        anchors.margins: Theme.gap
                        orientation: ListView.Horizontal
                        spacing: 6
                        clip: true
                        boundsBehavior: Flickable.StopAtBounds
                        model: focusPage.focusMode.stageRows
                        currentIndex: focusPage.focusMode.stageIndex
                        onCurrentIndexChanged: if (currentIndex >= 0) positionViewAtIndex(currentIndex, ListView.Contain)

                        delegate: Rectangle {
                            id: entry
                            required property int index
                            required property string kind
                            required property string name
                            required property string url
                            required property bool is_current

                            width: height * 1.3
                            height: filmstrip.height
                            radius: 4
                            color: is_current ? Theme.selection : Theme.panelRaised
                            border.width: is_current ? 2 : 0
                            border.color: Theme.accent

                            // The full image, shared with the stage and the slots ahead, so nothing is decoded twice.
                            Image {
                                anchors.fill: parent
                                anchors.margins: 4
                                anchors.bottomMargin: 20
                                visible: entry.kind === "image"
                                source: entry.kind === "image" ? entry.url : ""
                                fillMode: Image.PreserveAspectFit
                                asynchronous: true
                                mipmap: true
                            }
                            Label {
                                anchors.centerIn: parent
                                anchors.verticalCenterOffset: -8
                                visible: entry.kind !== "image"
                                text: ({"model": "3D", "audio": "Sound", "file": "File"})[entry.kind] || ""
                                color: Theme.text
                                font.bold: true
                            }
                            Label {
                                anchors.bottom: parent.bottom
                                anchors.left: parent.left
                                anchors.right: parent.right
                                anchors.margins: 4
                                horizontalAlignment: Text.AlignHCenter
                                elide: Text.ElideMiddle
                                text: entry.name
                                color: Theme.textDim
                                font.pixelSize: 10
                            }
                            MouseArea {
                                anchors.fill: parent
                                onClicked: {
                                    focusPage.focusMode.showEntry(entry.index)
                                    focusPage.forceActiveFocus()
                                }
                            }
                        }
                    }
                }
            }

            // The note: this ticket's words and drawings, and the delivery's own words.
            Rectangle {
                Layout.preferredWidth: 340
                Layout.fillHeight: true
                color: Theme.panel

                Rectangle {
                    anchors.left: parent.left
                    width: 1
                    height: parent.height
                    color: Theme.border
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8

                    // The note's header, with voice notes' recording indicator and level meter.
                    RowLayout {
                        objectName: "focusNotesHeader"
                        Layout.fillWidth: true
                        spacing: 8

                        Label {
                            text: "Notes"
                            color: Theme.text
                            font.bold: true
                        }
                        VoiceIndicator {
                            place: "focus"
                        }
                        Item {
                            Layout.fillWidth: true
                        }
                        Label {
                            readonly property int takeCount: focusPage.note.takes.length
                            readonly property int lockedCount: focusPage.note.lockedCount
                            text: (takeCount === 1 ? "1 take" : takeCount + " takes")
                                  + (lockedCount > 0 ? ", " + lockedCount + " locked" : "")
                            visible: takeCount > 0
                            color: Theme.textDim
                            font.pixelSize: Theme.fontSmall
                        }
                    }

                    ScrollView {
                        id: notesScroll
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        contentWidth: availableWidth

                        ColumnLayout {
                            width: notesScroll.availableWidth
                            spacing: 6

                            // The takes so far, in order: words, each correctable before the decision goes, or a
                            // spoken take on its way to words. Takes the checkpoint locks are marked down their side.
                            Repeater {
                                model: focusPage.note.takes

                                delegate: Item {
                                    id: takeRow
                                    required property int index
                                    required property var modelData
                                    readonly property bool isWords: modelData.state === "words"
                                    Layout.fillWidth: true
                                    implicitHeight: isWords ? takeField.implicitHeight : spokenTake.implicitHeight

                                    TextArea {
                                        id: takeField
                                        anchors.fill: parent
                                        visible: takeRow.isWords
                                        text: takeRow.modelData.text
                                        wrapMode: TextEdit.Wrap
                                        font.pixelSize: Theme.fontNormal
                                        leftPadding: 10
                                        background: Rectangle {
                                            radius: 4
                                            color: Theme.field
                                        }
                                        onEditingFinished: focusPage.note.setTake(takeRow.index, text)
                                        Keys.onEscapePressed: focusPage.forceActiveFocus()
                                    }
                                    SpokenTake {
                                        id: spokenTake
                                        anchors.fill: parent
                                        visible: !takeRow.isWords
                                        place: "focus"
                                        takeIndex: takeRow.index
                                        takeState: takeRow.modelData.state
                                        takeError: takeRow.modelData.error
                                    }
                                    Rectangle {
                                        visible: takeRow.modelData.isLocked
                                        width: 3
                                        height: parent.height
                                        radius: 1
                                        color: Theme.accent
                                        ToolTip.visible: lockHover.hovered
                                        ToolTip.text: "Locked by a checkpoint: a retake keeps it"

                                        HoverHandler {
                                            id: lockHover
                                        }
                                    }
                                }
                            }

                            TextArea {
                                id: draftField
                                objectName: "focusDraftField"
                                Layout.fillWidth: true
                                Layout.minimumHeight: 96
                                text: focusPage.note.draft
                                onTextChanged: focusPage.note.draft = text
                                wrapMode: TextEdit.Wrap
                                font.pixelSize: Theme.fontNormal
                                placeholderText: "What should change? N to write here, Esc when done"
                                Keys.onEscapePressed: focusPage.forceActiveFocus()
                            }

                            Label {
                                visible: drawingList.count > 0
                                text: "Drawings"
                                color: Theme.textDim
                                font.pixelSize: Theme.fontSmall
                            }

                            Flow {
                                Layout.fillWidth: true
                                spacing: 6

                                Repeater {
                                    id: drawingList
                                    model: focusPage.focusMode.drawingRows

                                    delegate: Rectangle {
                                        id: drawingTile
                                        required property string name
                                        required property string url
                                        width: 96
                                        height: 72
                                        radius: 4
                                        color: Theme.field

                                        Image {
                                            anchors.fill: parent
                                            anchors.margins: 3
                                            source: drawingTile.url
                                            sourceSize.width: 192
                                            fillMode: Image.PreserveAspectFit
                                            asynchronous: true
                                            // A drawing's file is replaced under the same name once the next round
                                            // starts over, so it is always read afresh.
                                            cache: false
                                        }
                                        ToolButton {
                                            anchors.top: parent.top
                                            anchors.right: parent.right
                                            width: 22
                                            height: 22
                                            padding: 0
                                            text: "×"
                                            focusPolicy: Qt.NoFocus
                                            // Backed, so it reads on a white sketch as well as on a dark render.
                                            background: Rectangle {
                                                radius: 11
                                                color: Theme.overlay
                                            }
                                            onClicked: focusPage.focusMode.removeDrawing(drawingTile.name)
                                            ToolTip.visible: hovered
                                            ToolTip.text: "Leave " + drawingTile.name + " out of the decision"
                                        }
                                    }
                                }
                            }

                            Label {
                                Layout.topMargin: 6
                                visible: focusPage.focusMode.deliveryText !== ""
                                text: "The delivery says"
                                color: Theme.textDim
                                font.pixelSize: Theme.fontSmall
                            }
                            Label {
                                Layout.fillWidth: true
                                visible: focusPage.focusMode.deliveryText !== ""
                                text: focusPage.focusMode.deliveryText
                                textFormat: Text.PlainText
                                wrapMode: Text.Wrap
                                maximumLineCount: 12
                                elide: Text.ElideRight
                                color: Theme.textDim
                                font.pixelSize: Theme.fontSmall
                            }
                        }
                    }

                    Label {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        color: Theme.textDim
                        font.pixelSize: Theme.fontSmall
                        text: focusPage.isDrawing
                              ? "P pen · H highlighter · E eraser · 1–3 size · Ctrl+Z undo · Enter or Esc keep"
                              : "← → ticket · ↑ ↓ image · A approve · C changes · D draw · B blank · N note · Esc grid"
                                + (studio.voiceNotes.isAvailable
                                   ? " · " + studio.voiceNotes.talkKeyLabel + " talk · "
                                     + studio.voiceNotes.retakeKeyLabel + " retake · "
                                     + studio.voiceNotes.checkpointKeyLabel + " checkpoint"
                                   : "")
                    }
                }
            }
        }
    }
}
