import math
import os
from collections import defaultdict

from PySide6.QtCore import QEvent, QLineF, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPen, QUndoStack
from PySide6.QtWidgets import QApplication, QGraphicsPathItem, QGraphicsScene, QGraphicsView, QLineEdit, QWidget

from . import model_info as mi
from . import undo
from .edge import Edge, bezier_path
from .node import INPUT, OUTPUT, Node, Socket, types_compatible
from .registry import NODE_TYPES
from .tab_menu import TabMenu

BG_COLOR = QColor(38, 38, 38)
GRID_MINOR = QColor(48, 48, 48)
GRID_MAJOR = QColor(28, 28, 28)
GRID_STEP = 40  # scene units between grid lines (x2 of the first 20, user's choice)
GRID_MAJOR_EVERY = 5


class InlineRename(QLineEdit):
    """Line edit laid over a node for double-click renaming. Enter/click away commits, Esc cancels."""

    def __init__(self, parent, node, on_commit):
        super().__init__(node.name, parent)
        self._on_commit = on_commit
        self._done = False
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#1e1e1e; color:#fff; border:1px solid #ffc83c;")
        self.editingFinished.connect(self._commit)
        # a click anywhere else commits too (a click on the graph doesn't take the keyboard focus from the field,
        # so editingFinished alone left it waiting for Enter)
        QApplication.instance().installEventFilter(self)

    def eventFilter(self, obj, event):
        if (not self._done and event.type() == QEvent.MouseButtonPress
                and not (isinstance(obj, QWidget) and (obj is self or self.isAncestorOf(obj)))):
            self._commit()
        return False

    def _end(self):
        self._done = True
        QApplication.instance().removeEventFilter(self)
        self.hide()
        self.deleteLater()

    def _commit(self):
        if not self._done:
            had_focus = self.hasFocus()  # Enter, not a click elsewhere
            self._end()
            self._on_commit(self.text())
            if had_focus:
                self.parentWidget().parentWidget().setFocus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._end()
            self.parentWidget().parentWidget().setFocus()  # viewport -> view
            return
        super().keyPressEvent(event)


class NodeScene(QGraphicsScene):
    node_renamed = Signal(object)
    node_changed = Signal(object)  # a node's parameters changed
    modified = Signal()            # anything worth saving changed (nodes, wires, params, positions)

    def __init__(self):
        super().__init__()
        self.setSceneRect(-50000, -50000, 100000, 100000)
        self._name_counters = defaultdict(int)
        self._propagating = False
        self.undo_stack = QUndoStack(self)  # this tab's undo history (the window's QUndoGroup follows the tab)
        self._node_list = None  # nodes(), kept until a node is added / removed
        self._by_name = {}      # node_named()'s map, rebuilt when a lookup misses (adds, removals, renames)
        self.node_changed.connect(self._node_changed)
        from . import links
        self.node_changed.connect(lambda node: links.propagate(self, node))  # linked parameters follow
        self.selectionChanged.connect(self._selection_changed)

    def _selection_changed(self):
        # link lines show only for selected nodes: the whole graph is redrawn only when there are any
        if any(getattr(n, "links", None) for n in self.nodes()):
            self.update()

    def graph_changed(self):
        """Let nodes that depend on others (e.g. Modality) re-check their inputs - after wiring, adding, removing."""
        self.modified.emit()
        if self._propagating:
            return
        self._propagating = True
        try:
            for node in self.nodes():
                node.upstream_changed()
        finally:
            self._propagating = False

    def wiring_changed(self, source, target):
        """A wire from `source` into `target` came or went: only the two ends and what `target` feeds can be
        affected (re-checking the whole graph per wire made opening a 450-node graph take 40 s)."""
        self.modified.emit()
        if self._propagating:
            return
        self._propagating = True
        try:
            for n in [source, target] + self.downstream(target) + [
                    n for n in self.nodes() if n.watches_graph() and n not in (source, target)]:
                if n.scene() is self:
                    n.upstream_changed()
        finally:
            self._propagating = False

    def _node_changed(self, node):
        """One node's parameters changed: only what it feeds (wire by wire, onwards) can be affected - plus nodes
        that watch others without wires (an Executor and what it built). Re-checking the whole graph on every
        keystroke made typing take ~1 s per key in a 450-node graph."""
        self.modified.emit()
        if self._propagating:
            return
        self._propagating = True
        try:
            for n in self.downstream(node) + [n for n in self.nodes() if n is not node and n.watches_graph()]:
                n.upstream_changed()
        finally:
            self._propagating = False

    def downstream(self, node):
        """The nodes fed by `node`, directly or further on, nearest first (each once)."""
        seen, order, todo = {node}, [], [node]
        while todo:
            for sock in todo.pop(0).output_sockets:
                for e in sock.edges:
                    n = e.target.node
                    if n not in seen:
                        seen.add(n)
                        order.append(n)
                        todo.append(n)
        return order

    def best_socket(self, node, from_socket):
        """For a wire dropped on a node's body: the first compatible socket, free ones first."""
        candidates = [s for s in node.sockets if self.can_connect(from_socket, s)]
        free = [s for s in candidates if s.multi or not s.edges]
        return (free or candidates or [None])[0]

    # ---- nodes / edges -------------------------------------------------
    def nodes(self):
        """The nodes in this graph (scanning every item each call was the slowest thing in a big graph)."""
        if self._node_list is None:
            self._node_list = [i for i in self.items() if isinstance(i, Node)]
        return list(self._node_list)

    def node_named(self, name):
        """The node called `name`, or None."""
        n = self._by_name.get(name)
        if n is None or n.name != name or n.scene() is not self:
            self._by_name = {x.name: x for x in self.nodes()}
            n = self._by_name.get(name)
        return n

    def addItem(self, item):
        super().addItem(item)
        if isinstance(item, Node):
            self._node_list = None

    def removeItem(self, item):
        super().removeItem(item)
        if isinstance(item, Node):
            self._node_list = None

    def create_node(self, type_name, pos: QPointF) -> Node:
        cls = NODE_TYPES[type_name]
        taken = {n.name for n in self.nodes()}
        while True:
            self._name_counters[type_name] += 1
            name = f"{type_name}{self._name_counters[type_name]}"
            if name not in taken:
                break
        node = cls(name)
        node.setPos(pos)
        self.addItem(node)
        self.clearSelection()
        node.setSelected(True)
        self.modified.emit()
        self.undo_stack.push(undo.AddNodes(self, [node], f"Add {name}"))
        return node

    def unique_name(self, name, taken=None):
        """name, or - if taken - its base with the first free number ('box' / 'box1' -> 'box2')."""
        if taken is None:
            taken = {n.name for n in self.nodes()}
        if name not in taken:
            return name
        base = name.rstrip("0123456789") or name
        i = 1
        while f"{base}{i}" in taken:
            i += 1
        return f"{base}{i}"

    def rename_node(self, node, new_name):
        """Rename, keeping names unique (Houdini-style: 'box' -> 'box1' if taken)."""
        new_name = "_".join(new_name.split())  # no whitespace in names
        if not new_name or new_name == node.name:
            self.node_renamed.emit(node)  # lets editors revert invalid input
            return
        new_name = self.unique_name(new_name, {n.name for n in self.nodes() if n is not node})
        if not undo.applying():
            self.undo_stack.push(undo.Rename(node, node.name, new_name))
        node.name = new_name
        node.update()
        self.node_renamed.emit(node)
        self.modified.emit()

    def can_connect(self, a: Socket, b: Socket) -> bool:
        if a is None or b is None or a.kind == b.kind or a.node is b.node:
            return False
        src, dst = (a, b) if a.kind == OUTPUT else (b, a)
        if not types_compatible(src.type, dst.type) or not dst.node.accepts(dst, src):
            return False
        if any(e.target is dst for e in src.edges):
            return False  # already connected
        return src.node not in dst.node.downstream_nodes()  # no cycles

    def connect_user(self, a: Socket, b: Socket):
        """A wire drawn by the user: connected like connect(), recorded for undo (with any wire it replaced)."""
        if not self.can_connect(a, b):
            return None
        before = undo.edge_state((a, b))
        edge = self.connect(a, b)
        if edge is not None:
            after = undo.edge_state(before.keys() | {a, b})
            self.undo_stack.push(undo.EdgesEdit(self, before, after, f"Connect {edge.source.node.name} → "
                                                                      f"{edge.target.node.name}"))
        return edge

    def connect(self, a: Socket, b: Socket):
        if not self.can_connect(a, b):
            return None
        src, dst = (a, b) if a.kind == OUTPUT else (b, a)
        for sock in (src, dst):
            if not sock.multi:
                for e in list(sock.edges):
                    e.remove()
        edge = Edge(src, dst)
        self.addItem(edge)
        self.wiring_changed(src.node, dst.node)
        return edge

    def delete_selected(self, verb="Delete"):
        items = self.selectedItems()
        nodes = [i for i in items if isinstance(i, Node)]
        edges = [i for i in items if isinstance(i, Edge)]
        if not nodes and not edges:
            return
        what = ", ".join(n.name for n in nodes[:3]) + ("…" if len(nodes) > 3 else "") if nodes else             f"{len(edges)} wire{'s' if len(edges) != 1 else ''}"
        self.undo_stack.push(undo.RemoveItems(self, nodes, edges, f"{verb} {what}"))  # push does the deleting

    # ---- background grid -----------------------------------------------
    def drawBackground(self, painter, rect):
        painter.fillRect(rect, BG_COLOR)
        left = math.floor(rect.left() / GRID_STEP) * GRID_STEP
        top = math.floor(rect.top() / GRID_STEP) * GRID_STEP
        minor, major = [], []
        x = left
        while x < rect.right():
            line = QLineF(x, rect.top(), x, rect.bottom())
            (major if x % (GRID_STEP * GRID_MAJOR_EVERY) == 0 else minor).append(line)
            x += GRID_STEP
        y = top
        while y < rect.bottom():
            line = QLineF(rect.left(), y, rect.right(), y)
            (major if y % (GRID_STEP * GRID_MAJOR_EVERY) == 0 else minor).append(line)
            y += GRID_STEP
        painter.setPen(QPen(GRID_MINOR, 0))
        painter.drawLines(minor)
        painter.setPen(QPen(GRID_MAJOR, 0))
        painter.drawLines(major)
        # parameter links: while one of the two nodes is selected, a thin straight line between them
        # (centre to centre), a little lighter than the background
        from . import links
        pen = QPen(QColor(150, 150, 150, 120), 1.2)
        pen.setCosmetic(True)  # the same thin line at any zoom
        painter.setPen(pen)
        for src, node in links.pairs(self):
            if src.isSelected() or node.isSelected():
                painter.drawLine(src.scenePos(), node.scenePos())  # node bodies are centred on their position


class NodeView(QGraphicsView):
    ZOOM_STEP = 1.15
    ZOOM_MIN, ZOOM_MAX = 0.2, 3.0
    DRAG_ZOOM_SPEED = 0.005  # per pixel of right-drag

    def __init__(self, scene: NodeScene):
        super().__init__(scene)
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        # only what changed is redrawn (a full redraw per change took ~0.3 s in a 450-node graph); nodes call
        # prepareGeometryChange before their size changes, so nothing is left behind
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setTransformationAnchor(QGraphicsView.NoAnchor)  # _zoom_by anchors itself
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setAcceptDrops(True)  # models dragged from the Library or Explorer
        self.centerOn(0, 0)

        self._pan_last = None
        self._zoom_last = None
        self._zoom_anchor_view = None
        self._zoom_anchor_scene = None
        self._drag_socket = None
        self._temp_edge = None
        self._lifted = None   # (input socket, wire) picked up by dragging a connected input
        self._faded = []      # an output's wires while a new wire is dragged from it (dropped on nothing: removed)
        self._spawn_pos = QPointF()
        self._press_positions = None  # node positions when the left button went down (one drag = one undo step)
        self.locked = ""  # set while an Executor builds (the reason): pan / zoom only, nothing is edited by hand

        self.tab_menu = TabMenu(self, NODE_TYPES)
        self.tab_menu.node_chosen.connect(self._create_from_menu)

    def drawForeground(self, painter, rect):
        """While an Executor builds: a line at the top saying the graph is locked (pan / zoom still work)."""
        super().drawForeground(painter, rect)
        if not self.locked:
            return
        painter.save()
        painter.resetTransform()
        text = f"Executor {self.locked} is building - the graph is locked (pan / zoom work; Stop is in its pane)"
        painter.setFont(QFont("Segoe UI", 9))
        w = painter.fontMetrics().horizontalAdvance(text) + 24
        box = QRectF(10, 10, w, 26)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(60, 30, 80, 220))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor(235, 225, 245))
        painter.drawText(box, Qt.AlignCenter, text)
        painter.restore()

    # ---- keyboard ------------------------------------------------------
    def focusNextPrevChild(self, next):
        return False  # keep Tab for ourselves

    def keyPressEvent(self, event):
        if self.locked:
            return
        if event.key() == Qt.Key_Tab:
            self._open_tab_menu()
            return
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.scene().delete_selected()
            return
        super().keyPressEvent(event)

    # ---- rename --------------------------------------------------------
    def mouseDoubleClickEvent(self, event):
        if self.locked:
            return
        item = self.itemAt(event.position().toPoint())
        if event.button() == Qt.LeftButton and isinstance(item, Node):
            self.start_rename(item)
            return
        super().mouseDoubleClickEvent(event)

    def start_rename(self, node):
        name = node.mapRectToScene(node.name_rect())
        rect = self.mapFromScene(name).boundingRect().adjusted(-4, -2, 60, 2)  # room to type a longer name
        editor = InlineRename(self.viewport(), node, lambda text: self._finish_rename(node, text))
        editor.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        editor.setFont(QFont("Segoe UI", max(6, round(11 * self.transform().m11()))))
        editor.setGeometry(rect)
        editor.selectAll()
        editor.show()
        editor.setFocus()

    def _finish_rename(self, node, text):
        self.scene().rename_node(node, text)

    def _open_tab_menu(self):
        view_pos = self.mapFromGlobal(QCursor.pos())
        if not self.viewport().rect().contains(view_pos):
            view_pos = self.viewport().rect().center()
        self._spawn_pos = self.mapToScene(view_pos)
        self.tab_menu.popup(self.viewport().mapToGlobal(view_pos))

    def _create_from_menu(self, type_name):
        self.scene().create_node(type_name, self._spawn_pos)
        self.setFocus()

    # ---- mouse: pan, connect -------------------------------------------
    def _socket_at(self, view_pos):
        """The socket under the cursor: within HIT_RADIUS screen pixels off a node's body, but ON a body only within
        the dot as drawn - zoomed out, a whole node is a few pixels tall and the generous radius would cover it, so
        a click meant to select the node grabbed a socket instead."""
        r = Socket.HIT_RADIUS
        hits = [i for i in self.items(QRect(view_pos.x() - r, view_pos.y() - r, 2 * r, 2 * r)) if isinstance(i, Socket)]
        pos = self.mapToScene(view_pos)
        socket = min(hits, key=lambda s: QLineF(s.scenePos(), pos).length(), default=None)  # close sockets: the nearest
        if socket is not None:
            body = self._node_at(view_pos)
            if body is not None and body.shape().contains(body.mapFromScene(pos)):
                drawn = (socket.node.SOCKET_RADIUS + 2) * self.transform().m11()  # the dot's radius on screen
                if QLineF(self.mapFromScene(socket.scenePos()), view_pos).length() > max(drawn, 3):
                    return None
        return socket

    def _node_at(self, view_pos):
        item = self.itemAt(view_pos)
        while item is not None and not isinstance(item, Node):
            item = item.parentItem()
        return item

    def _drop_target(self, view_pos):
        """Socket under the cursor, or the best matching socket of the node under the cursor."""
        socket = self._socket_at(view_pos)
        if socket or not self._drag_socket:
            return socket
        item = self._node_at(view_pos)
        if item is None or item is self._drag_socket.node:
            return None
        return self.scene().best_socket(item, self._drag_socket)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._zoom_last = event.position().toPoint()
            self._zoom_anchor_view = self._zoom_last
            self._zoom_anchor_scene = self.mapToScene(self._zoom_last)
            self.setCursor(Qt.SizeAllCursor)
            return
        if event.button() == Qt.MiddleButton:
            self._pan_last = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if event.button() == Qt.LeftButton and self.locked:
            return  # an Executor is building: nothing is picked, moved or wired by hand meanwhile
        if event.button() == Qt.LeftButton:
            socket = self._socket_at(event.position().toPoint())
            if socket:
                if socket.kind == INPUT and socket.edges:  # pick the wire up by its end (the last one on a multi input)
                    edge = socket.edges[-1]
                    self._lifted = (socket, edge)
                    edge.setVisible(False)
                    socket = edge.source
                elif socket.kind == OUTPUT:
                    self._faded = list(socket.edges)
                    for e in self._faded:
                        e.setOpacity(0.35)
                self._drag_socket = socket
                self._temp_edge = QGraphicsPathItem()
                self._temp_edge.setPen(QPen(QColor(255, 200, 60), 2, Qt.DashLine))
                self._temp_edge.setZValue(-1)
                self.scene().addItem(self._temp_edge)
                self._update_temp_edge(event.position().toPoint())
                return
            self._press_positions = {n: n.pos() for n in self.scene().nodes()}
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._zoom_last is not None:
            # Right-drag zoom: drag right/up to zoom in, left/down to zoom out.
            pos = event.position().toPoint()
            delta = (pos.x() - self._zoom_last.x()) - (pos.y() - self._zoom_last.y())
            self._zoom_last = pos
            self._zoom_by(math.exp(delta * self.DRAG_ZOOM_SPEED))
            return
        if self._pan_last is not None:
            pos = event.position().toPoint()
            delta = pos - self._pan_last
            self._pan_last = pos
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            return
        if self._drag_socket:
            self._update_temp_edge(event.position().toPoint())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton and self._zoom_last is not None:
            self._zoom_last = None
            self.unsetCursor()
            return
        if event.button() == Qt.MiddleButton and self._pan_last is not None:
            self._pan_last = None
            self.unsetCursor()
            return
        if event.button() == Qt.LeftButton and self._drag_socket:
            pos = event.position().toPoint()
            self._finish_wire(self._drop_target(pos), empty=self._node_at(pos) is None and not self._socket_at(pos))
            self.scene().removeItem(self._temp_edge)
            self._temp_edge = None
            self._drag_socket = None
            return
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton and self._press_positions is not None:
            before, self._press_positions = self._press_positions, None
            moved = {n: p for n, p in before.items() if n.scene() is self.scene() and n.pos() != p}
            if moved:
                names = ", ".join(n.name for n in list(moved)[:3]) + ("…" if len(moved) > 3 else "")
                self.scene().undo_stack.push(undo.MoveNodes(moved, {n: n.pos() for n in moved}, f"Move {names}"))

    def _finish_wire(self, target, empty):
        """A wire dragged from a socket was let go over `target` (a socket or None); `empty`: over no node at all.
        A picked-up input wire moves to the target or, dropped on nothing, is removed; a new wire from an output
        connects, or - dropped on nothing - the output's wires are removed. Each is one undo step."""
        scene = self.scene()
        lifted, self._lifted = self._lifted, None
        faded, self._faded = self._faded, []
        for e in faded:
            e.setOpacity(1.0)
        if lifted is not None:
            old_input, edge = lifted
            edge.setVisible(True)
            if target is old_input:
                return  # put back where it was
            new_input = target if target is not None and target.kind == INPUT                 and scene.can_connect(edge.source, target) else None
            if new_input is None and not empty:
                return  # over a node that can't take it: leave it as it was
            before = undo.edge_state((old_input, edge.source) + ((new_input,) if new_input else ()))
            edge.remove()
            if new_input is not None:
                scene.connect(edge.source, new_input)
            after = undo.edge_state(before.keys())
            what = f"{edge.source.node.name} → {(new_input or old_input).node.name}"
            scene.undo_stack.push(undo.EdgesEdit(scene, before, after,
                                                 f"Move wire {what}" if new_input else f"Disconnect {what}"))
            return
        if empty and self._drag_socket.kind == OUTPUT and faded:
            before = undo.edge_state((self._drag_socket,))
            for e in faded:
                e.remove()
            after = undo.edge_state(before.keys())
            scene.undo_stack.push(undo.EdgesEdit(scene, before, after, f"Disconnect {self._drag_socket.node.name}"))
            return
        scene.connect_user(self._drag_socket, target)

    def _update_temp_edge(self, view_pos):
        start = self._drag_socket.scenePos()
        target = self._drop_target(view_pos)
        if self.scene().can_connect(self._drag_socket, target):
            end = target.scenePos()  # snap to a valid socket
        else:
            end = self.mapToScene(view_pos)
        if self._drag_socket.kind == INPUT:
            start, end = end, start
        self._temp_edge.setPath(bezier_path(start, end))

    # ---- drops from the Library / Explorer ------------------------------
    PART_SPACING = 300  # scene units between the nodes made from one dropped model (room for their labels)

    @staticmethod
    def _models_in_drop(mime):
        """[(node name or None, path)] for the Model nodes a dropped path makes; [] if it isn't a model.
        A diffusers pipeline gives one per part its model_index.json lists (parts with weights only)."""
        urls = [u for u in mime.urls() if u.isLocalFile()]
        if len(urls) != 1:
            return []
        path = os.path.normpath(urls[0].toLocalFile())
        if os.path.isdir(path):
            parts = mi.pipeline_parts(path)
            if parts:
                return [(name, folder) for name, _, folder in parts]
            if not mi.model_root(path + os.sep + "x") == path:  # not one model's folder (no config.json etc.)
                files = sorted(f for f in os.listdir(path) if f.lower().endswith(mi.WEIGHT_EXT + mi.OTHER_WEIGHT_EXT))
                if len(files) > 1:  # a folder of separate weight files (e.g. a GGUF repo): one node per file
                    return [(os.path.splitext(f)[0], os.path.join(path, f)) for f in files]
            return [(mi.display_name(path), path)] if mi.has_weights(path) else []
        return [(None, path)] if path.lower().endswith(mi.WEIGHT_EXT + mi.OTHER_WEIGHT_EXT) else []

    @staticmethod
    def _online_in_drop(mime):
        """An online model's settings file dropped (from the Library's Online list), or None."""
        from . import online
        urls = [u for u in mime.urls() if u.isLocalFile()]
        return online.entry_for_file(urls[0].toLocalFile()) if len(urls) == 1 else None

    def dragEnterEvent(self, event):
        if not self.locked and (self._online_in_drop(event.mimeData()) or self._models_in_drop(event.mimeData())):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        entry = self._online_in_drop(event.mimeData())
        if entry is not None:  # a Model node running that online model
            event.acceptProposedAction()
            scene = self.scene()
            scene.undo_stack.beginMacro("Drop " + entry["label"])
            node = scene.create_node("model", self.mapToScene(event.position().toPoint()))
            with undo.record(node, "Online model"):
                node.set_param("runs_on", "online")
                node.set_param("provider", entry["provider"])
                node.set_param("online_model", entry["name"])
            scene.rename_node(node, entry["name"])
            scene.undo_stack.endMacro()
            scene.clearSelection()
            node.setSelected(True)
            scene.graph_changed()
            self.setFocus()
            return
        models = self._models_in_drop(event.mimeData())
        if not models:
            event.ignore()
            return
        event.acceptProposedAction()
        scene = self.scene()
        pos = self.mapToScene(event.position().toPoint())
        made = []
        scene.undo_stack.beginMacro("Drop " + ", ".join(name or os.path.basename(path) for name, path in models[:3])
                                    + ("…" if len(models) > 3 else ""))
        for i, (name, path) in enumerate(models):
            node = scene.create_node("model", pos + QPointF(i * self.PART_SPACING, 0))
            with undo.record(node, "File"):
                node.set_param("file", path)
            if name:
                scene.rename_node(node, name)
            made.append(node)
        scene.undo_stack.endMacro()
        scene.clearSelection()
        for node in made:
            node.setSelected(True)
        scene.graph_changed()
        self.setFocus()

    # ---- zoom ----------------------------------------------------------
    def wheelEvent(self, event):
        factor = self.ZOOM_STEP if event.angleDelta().y() > 0 else 1 / self.ZOOM_STEP
        pos = event.position().toPoint()
        self._zoom_by(factor, pos, self.mapToScene(pos))

    def _zoom_by(self, factor, anchor_view=None, anchor_scene=None):
        """Scale the view, keeping anchor_scene under anchor_view on screen."""
        if anchor_view is None:
            anchor_view, anchor_scene = self._zoom_anchor_view, self._zoom_anchor_scene
        current = self.transform().m11()
        factor = max(self.ZOOM_MIN, min(self.ZOOM_MAX, current * factor)) / current
        self.scale(factor, factor)
        drift = self.mapFromScene(anchor_scene) - anchor_view
        self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() + drift.x())
        self.verticalScrollBar().setValue(self.verticalScrollBar().value() + drift.y())

