// The Review mode: the project's review queue as a grid of tiles, one per ticket, with a tab per configured queue.
//
// Keyboard first: arrows move the cursor (Shift extends the selection), Space selects, Ctrl+A selects the tab, Esc
// clears, A approves and C requests changes on the selection (or the tile under the cursor), Enter opens it in focus
// mode, F5 reads the queue from the tracker again. With the mouse: a click selects one tile, Ctrl+click toggles one,
// Shift+click selects a range, dragging on the grid draws a selection box (with Ctrl, added to the selection), and a
// double click opens a tile.
//
// The grid itself takes no mouse input (`interactive: false`), so one MouseArea beneath it handles clicks, the box and
// the wheel, and a tile's own Retry button still gets its clicks. The box selects by the grid's geometry, which also
// covers tiles scrolled out of view, whose delegates do not exist.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: reviewPage
    color: Theme.window

    readonly property var board: studio.reviewBoard
    readonly property bool isFocusOpen: board.focusedTicket !== ""

    // Give the grid the keyboard whenever the page or the grid comes back into view.
    function focusGrid() {
        if (isFocusOpen)
            focusPage.forceActiveFocus()
        else
            grid.forceActiveFocus()
    }

    // Later, once the stack has switched pages: an item not yet shown cannot take the focus.
    onVisibleChanged: if (visible) Qt.callLater(focusGrid)
    onIsFocusOpenChanged: Qt.callLater(focusGrid)

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        // Tabs per queue, the selection, the decisions and refresh.
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: 44
            color: Theme.panel
            visible: reviewPage.board.isConfigured && !reviewPage.isFocusOpen

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
                spacing: 8

                Repeater {
                    model: reviewPage.board.tabRows

                    delegate: Button {
                        required property string name
                        required property int count
                        text: name + "  " + count
                        flat: reviewPage.board.currentTab !== name
                        highlighted: reviewPage.board.currentTab === name
                        focusPolicy: Qt.NoFocus
                        onClicked: {
                            reviewPage.board.selectTab(name)
                            grid.forceActiveFocus()
                        }
                    }
                }

                Item {
                    Layout.fillWidth: true
                }

                Label {
                    visible: reviewPage.board.selectedCount > 0
                    text: reviewPage.board.selectedCount + " selected"
                    color: Theme.textDim
                    font.pixelSize: Theme.fontSmall
                }
                Button {
                    text: "Approve"
                    focusPolicy: Qt.NoFocus
                    enabled: grid.count > 0
                    onClicked: decisionPopup.openFor("approve")
                    ToolTip.visible: hovered
                    ToolTip.text: "A"
                }
                Button {
                    text: "Request changes"
                    focusPolicy: Qt.NoFocus
                    enabled: grid.count > 0
                    onClicked: decisionPopup.openFor("changes")
                    ToolTip.visible: hovered
                    ToolTip.text: "C"
                }
                Button {
                    text: reviewPage.board.isRefreshing ? "Refreshing…" : "Refresh"
                    focusPolicy: Qt.NoFocus
                    enabled: !reviewPage.board.isRefreshing
                    onClicked: reviewPage.board.refresh()
                    ToolTip.visible: hovered
                    ToolTip.text: "Read the queue from the tracker again (F5)"
                }
            }
        }

        // The last refresh failed: the tiles are the refresh before it's.
        Rectangle {
            Layout.fillWidth: true
            implicitHeight: errorLabel.implicitHeight + 12
            visible: reviewPage.board.error !== "" && !reviewPage.isFocusOpen
            color: Theme.dangerSurface

            Label {
                id: errorLabel
                anchors.fill: parent
                anchors.margins: 6
                anchors.leftMargin: 12
                wrapMode: Text.WordWrap
                text: "The queue could not be refreshed: " + reviewPage.board.error
                color: Theme.text
                font.pixelSize: Theme.fontSmall
            }
        }

        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: reviewPage.isFocusOpen ? 1 : 0

            Item {
                id: gridArea

                // Clicks, the selection box and the wheel; beneath the grid, so tiles' own buttons come first.
                MouseArea {
                    id: gridMouse
                    anchors.fill: grid
                    acceptedButtons: Qt.LeftButton

                    property point pressPoint
                    property bool isBoxing: false

                    // The tiles a box between two points (in the grid's content coordinates) covers, by index.
                    function indicesInBox(left, top, right, bottom) {
                        const columns = Math.max(1, Math.floor(grid.width / grid.cellWidth))
                        const firstRow = Math.max(0, Math.floor(top / grid.cellHeight))
                        const lastRow = Math.floor(bottom / grid.cellHeight)
                        const firstColumn = Math.max(0, Math.floor(left / grid.cellWidth))
                        const lastColumn = Math.min(columns - 1, Math.floor(right / grid.cellWidth))
                        const indices = []
                        for (let row = firstRow; row <= lastRow; row++) {
                            for (let column = firstColumn; column <= lastColumn; column++) {
                                const index = row * columns + column
                                if (index < grid.count)
                                    indices.push(index)
                            }
                        }
                        return indices
                    }

                    function contentPoint(x, y) {
                        return Qt.point(x + grid.contentX - grid.originX, y + grid.contentY - grid.originY)
                    }

                    onPressed: mouse => {
                        grid.forceActiveFocus()
                        pressPoint = Qt.point(mouse.x, mouse.y)
                        isBoxing = false
                    }
                    onPositionChanged: mouse => {
                        const isFar = Math.abs(mouse.x - pressPoint.x) + Math.abs(mouse.y - pressPoint.y) > 6
                        if (!isBoxing && isFar) {
                            isBoxing = true
                            reviewPage.board.beginBoxSelection((mouse.modifiers & Qt.ControlModifier) !== 0)
                        }
                        if (!isBoxing)
                            return
                        const start = contentPoint(pressPoint.x, pressPoint.y)
                        const end = contentPoint(mouse.x, mouse.y)
                        reviewPage.board.updateBoxSelection(indicesInBox(
                            Math.min(start.x, end.x), Math.min(start.y, end.y),
                            Math.max(start.x, end.x), Math.max(start.y, end.y)))
                        selectionBox.x = gridMouse.x + Math.min(pressPoint.x, mouse.x)
                        selectionBox.y = gridMouse.y + Math.min(pressPoint.y, mouse.y)
                        selectionBox.width = Math.abs(mouse.x - pressPoint.x)
                        selectionBox.height = Math.abs(mouse.y - pressPoint.y)
                    }
                    onReleased: mouse => {
                        if (isBoxing) {
                            isBoxing = false
                            reviewPage.board.endBoxSelection()
                            return
                        }
                        const point = contentPoint(mouse.x, mouse.y)
                        const index = grid.indexAt(point.x, point.y)
                        if (index < 0)
                            reviewPage.board.clearSelection()
                        else
                            reviewPage.board.clickTile(index, mouse.modifiers)
                    }
                    onDoubleClicked: mouse => {
                        const point = contentPoint(mouse.x, mouse.y)
                        const index = grid.indexAt(point.x, point.y)
                        if (index >= 0)
                            reviewPage.board.openTicket(index)
                    }
                    onWheel: wheel => {
                        const bottom = Math.max(grid.originY, grid.originY + grid.contentHeight - grid.height)
                        grid.contentY = Math.min(bottom, Math.max(grid.originY, grid.contentY - wheel.angleDelta.y))
                    }
                }

                GridView {
                    id: grid
                    objectName: "reviewGrid"
                    anchors.fill: parent
                    anchors.margins: Theme.gap
                    clip: true
                    interactive: false
                    keyNavigationEnabled: false
                    focus: true
                    model: reviewPage.board.tileRows
                    currentIndex: reviewPage.board.cursorIndex

                    // As many tiles of about 250 pixels as fit across, sharing the width.
                    readonly property int columns: Math.max(1, Math.floor(width / 250))
                    cellWidth: Math.floor(width / columns)
                    cellHeight: Math.round(cellWidth * 0.75) + 64

                    ScrollBar.vertical: ScrollBar {}

                    delegate: ReviewTile {
                        width: grid.cellWidth
                        height: grid.cellHeight
                        isCursor: index === grid.currentIndex
                        isGridFocused: grid.activeFocus
                    }

                    onCurrentIndexChanged: if (currentIndex >= 0) positionViewAtIndex(currentIndex, GridView.Contain)

                    Keys.onPressed: event => {
                        const isShift = (event.modifiers & Qt.ShiftModifier) !== 0
                        const isCtrl = (event.modifiers & Qt.ControlModifier) !== 0
                        const keySteps = {
                            [Qt.Key_Left]: -1,
                            [Qt.Key_Right]: 1,
                            [Qt.Key_Up]: -grid.columns,
                            [Qt.Key_Down]: grid.columns,
                            [Qt.Key_Home]: -grid.count,
                            [Qt.Key_End]: grid.count
                        }
                        event.accepted = true
                        if (event.key in keySteps)
                            reviewPage.board.moveCursor(keySteps[event.key], isShift)
                        else if (event.key === Qt.Key_Space)
                            reviewPage.board.toggleAtCursor()
                        else if (event.key === Qt.Key_A && isCtrl)
                            reviewPage.board.selectAll()
                        else if (event.key === Qt.Key_A)
                            decisionPopup.openFor("approve")
                        else if (event.key === Qt.Key_C && !isCtrl)
                            decisionPopup.openFor("changes")
                        else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter)
                            reviewPage.board.openAtCursor()
                        else if (event.key === Qt.Key_Escape)
                            reviewPage.board.clearSelection()
                        else if (event.key === Qt.Key_F5)
                            reviewPage.board.refresh()
                        else
                            event.accepted = false
                    }
                }

                Rectangle {
                    id: selectionBox
                    visible: gridMouse.isBoxing
                    color: Theme.selectionBox
                    border.color: Theme.accent
                }

                Label {
                    anchors.centerIn: parent
                    width: Math.min(560, parent.width - 80)
                    visible: grid.count === 0
                    horizontalAlignment: Text.AlignHCenter
                    wrapMode: Text.WordWrap
                    color: Theme.textDim
                    text: !reviewPage.board.isConfigured
                          ? "This project has no review queue. A [review] table in its atelier.toml links it to its "
                            + "ticket tracker."
                          : !reviewPage.board.hasRefreshed ? "Reading the review queue…"
                          : "Nothing is waiting for review in " + reviewPage.board.currentTab + "."
                }
            }

            ReviewFocusPage {
                id: focusPage
            }
        }
    }

    DecisionPopup {
        id: decisionPopup
        onClosed: reviewPage.focusGrid()
    }
}
