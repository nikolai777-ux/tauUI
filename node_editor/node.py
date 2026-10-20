import time

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QGraphicsItem

from . import node_shapes

INPUT = "input"
OUTPUT = "output"

# ---- data types flowing through connections -------------------------------
ANY = "any"
MODEL = "model"
TEXT = "text"
IMAGE = "image"
AUDIO = "audio"
VIDEO = "video"
JSON = "json"  # a JSON file of structured results (e.g. a video's shots / chunks - its "format" line says which)
FILE = "file"  # any other file (PDF, text, archive...) - from a File node
GEOMETRY = "geometry"  # a 3D file (OBJ, FBX, glTF / GLB, PLY, STL...) - from a Geometry node
DATA = "data"  # the material a model works on: pictures, videos, sounds, JSON, other files - one, or a Data node's list
PROMPT = TEXT  # a prompt is just text
MATERIAL = (IMAGE, AUDIO, VIDEO, JSON, FILE, GEOMETRY, DATA)  # what a data input takes (a prompt goes to the prompt input)

TYPE_COLORS = {
    ANY: QColor(150, 150, 150),
    MODEL: QColor(80, 150, 240),
    TEXT: QColor(240, 190, 70),
    IMAGE: QColor(190, 120, 220),
    AUDIO: QColor(90, 200, 150),
    VIDEO: QColor(235, 120, 80),
    JSON: QColor(120, 205, 215),
    FILE: QColor(170, 160, 140),
    GEOMETRY: QColor(150, 200, 110),
    DATA: QColor(225, 220, 190),
}
TYPE_LABELS = {ANY: "Any", MODEL: "Model", TEXT: "Text", IMAGE: "Image", AUDIO: "Audio", VIDEO: "Video", JSON: "JSON",
               FILE: "File", GEOMETRY: "Geometry", DATA: "Data"}

# Every node body looks the same; only sockets (and wires) carry colour.
BODY_TOP = QColor(150, 150, 150)
BODY_BOTTOM = QColor(112, 112, 112)
BODY_BORDER = QColor(18, 18, 18)
BODY_DETAIL = QColor(78, 78, 78)
BODY_GROOVE = QColor(200, 200, 200)  # light "cut-out" strokes on icon-like shapes
SELECT_COLOR = QColor(255, 200, 60)
TYPE_TEXT = QColor(135, 135, 135)
NAME_TEXT = QColor(225, 225, 225)
INFO_TEXT = QColor(100, 160, 225)
PROBLEM_TEXT = QColor(225, 95, 90)
TYPE_FONT = QFont("Segoe UI", 8)
NAME_FONT = QFont("Segoe UI", 11)
INFO_FONT = QFont("Segoe UI", 8)

def types_compatible(out_type, in_type):
    if in_type == DATA:  # a data input takes any material (one item, or a list of them)
        return out_type in MATERIAL + (ANY,)
    if out_type == DATA:  # a list of material feeds data inputs and the nodes that take lists (Isolate, Delete...)
        return in_type == ANY
    return ANY in (out_type, in_type) or out_type == in_type


KIND_EXT = {IMAGE: (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"),
            VIDEO: (".mp4", ".webm", ".mov", ".mkv", ".avi", ".m4v"),
            AUDIO: (".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac", ".opus"),
            JSON: (".json", ".shots", ".chunks"),  # .shots / .chunks: JSON files from before the "format" line
            GEOMETRY: (".obj", ".fbx", ".glb", ".gltf", ".ply", ".stl", ".dae", ".3ds", ".usd", ".usda", ".usdc",
                       ".usdz", ".abc", ".bvh")}


def item_kind(value, wire_type=ANY):
    """What one item of data is: the wire's own type when it says (image / video / audio / json / model / text),
    else from the file's extension ("file" for any other file, "text" for a plain string)."""
    if wire_type not in (ANY, DATA, FILE):  # a File node's file is what its extension says (a PDF, a picture...)
        return wire_type
    if not isinstance(value, str):
        return MODEL if hasattr(value, "model_path") else ANY
    import os
    ext = os.path.splitext(value)[1].lower()
    kind = next((k for k, exts in KIND_EXT.items() if ext in exts), None)
    if kind:
        return kind
    return "file" if os.path.isabs(value) or os.path.isfile(value) else TEXT



_METRICS = {}


def _metrics(font):
    """QFontMetrics per font, made once (the node texts use a few fixed fonts)."""
    key = font.key()
    m = _METRICS.get(key)
    if m is None:
        m = _METRICS[key] = QFontMetrics(font)
    return m

class Param:
    """A node parameter shown in the parameter panel.

    kind: float | int | string | bool | text (multi-line) | choice | file | image (file + preview)
      | icon (a button drawn as the icon named by default; with row, on a shared line)
    options: for choice, a list of (value, label) - or callable(node) -> that list (node may be None)
    filter: for file/image, the file-dialog filter
    visible: optional callable(node) -> bool to show the parameter only sometimes
    step, minimum, maximum: for int/float - one tick of the ruler and the range it scrubs over
    (typed values may go beyond it). Without a step the number gets a plain field.
    hint: what changing it does, in a few words - shown when the mouse rests on it
    row: parameters with the same row name share one line (ticks, icon buttons)
    enabled: optional callable(node) -> bool; shown but greyed out when False
    submit: for text - the action Enter runs (Shift+Enter starts a new line)
    lines: for text boxes - their default height in lines (without it a box stretches to fill the pane)
    """

    def __init__(self, name, label, kind, default, options=None, filter=None, visible=None, placeholder=None,
                 accept_folders=False, step=None, minimum=None, maximum=None, hint="", row=None, enabled=None,
                 submit=None, lines=None):
        self.hint = hint
        self.lines = lines
        self.row = row
        self.enabled = enabled
        self.submit = submit
        self.name = name
        self.label = label
        self.kind = kind
        self.default = default
        self.options = options or []
        self.filter = filter
        self.visible = visible or (lambda node: True)
        self.placeholder = placeholder  # grey hint text in an empty field (a string, or callable() -> str)
        self.accept_folders = accept_folders  # a file field that also takes a folder of such files (drop/paste)
        self.step, self.minimum, self.maximum = step, minimum, maximum

    def choices(self, node=None):
        """The (value, label) options now - for a node (e.g. the online models of its chosen provider)."""
        return list(self.options(node) if callable(self.options) else self.options)


def set_hints(cls, hints):
    """Hover hints for a node type's parameters ({name: a few words on what changing it does})."""
    for p in cls.parameters:
        if p.name in hints:
            p.hint = hints[p.name]


class Socket(QGraphicsItem):
    """Connection point on a node. Inputs sit on top, outputs on the bottom."""

    RADIUS = 6
    HIT_RADIUS = 10

    def __init__(self, node, kind, name, data_type=ANY, multi=True):
        super().__init__(node)
        self.node = node
        self.kind = kind
        self.name = name
        self.type = data_type
        self.multi = multi  # allow more than one edge on this socket
        self.color_override = None  # a colour other than the data type's (none use it now)
        self.edges = []
        self._hover = False
        self.setAcceptHoverEvents(True)
        self._update_tooltip()

    def set_type(self, data_type):
        if data_type == self.type:
            return
        self.type = data_type
        for edge in list(self.edges):  # drop connections that no longer fit
            if not types_compatible(edge.source.type, edge.target.type)                     or not edge.target.node.accepts(edge.target, edge.source):
                edge.remove()
            else:
                edge.update()
                edge.target.update()  # an input shows the colour of what feeds it
        self._update_tooltip()
        self.update()

    def _update_tooltip(self):
        self.setToolTip(f"{self.name} ({TYPE_LABELS.get(self.type, self.type)})")

    def boundingRect(self):
        r = self.HIT_RADIUS
        return QRectF(-r, -r, 2 * r, 2 * r)

    def shape(self):
        path = QPainterPath()
        path.addEllipse(self.boundingRect())
        return path

    def display_color(self):
        """An input that is connected takes the colour of the output feeding it (like its wire), unless that
        output is untyped and the input itself has a type."""
        if self.color_override:
            return self.color_override
        if self.kind == INPUT and self.edges:
            src = self.edges[0].source
            if src.type != ANY or src.color_override:
                return src.display_color()
        return TYPE_COLORS.get(self.type, TYPE_COLORS[ANY])

    def set_color(self, color):
        self.color_override = color
        self.update()
        for edge in self.edges:
            edge.update()

    def paint(self, painter, option, widget=None):
        r = self.node.SOCKET_RADIUS + (2 if self._hover else 0)
        fill = self.display_color()
        if self._hover:
            fill = fill.lighter(130)
        painter.setPen(QPen(QColor(255, 200, 60) if self._hover else QColor(20, 20, 20), 1.5))
        painter.setBrush(fill)
        painter.drawEllipse(QRectF(-r, -r, 2 * r, 2 * r))

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()


class Node(QGraphicsItem):
    """Base node: a box with inputs along the top and outputs along the bottom."""

    type_name = "node"
    label = "Node"
    color = QColor(75, 90, 110)
    shape_name = "rect"   # see node_shapes.SHAPES
    OUTPUT_X = None       # optional x positions (0..1 of width) for output sockets, e.g. under a speaker cone
    SOCKET_RADIUS = 6
    WIDTH = 100           # every node has the same width...
    HEIGHT = None         # ...and its height follows the shape's proportions (node_shapes.ASPECT)
    parameters = []            # list of Param, overridden by node types
    inputs = [("in", ANY)]     # (name, data type[, multi]) per input socket
    outputs = [("out", ANY)]   # (name, data type[, multi]) per output socket
    show_socket_labels = False  # input names above the sockets: True = always, "hover" = on hover/selection
    not_copied = ()  # parameters a copy starts without (e.g. a conversation that belongs to this node only)
    body_colors = (BODY_TOP, BODY_BOTTOM, BODY_DETAIL, BODY_GROOVE)  # uniform grey; a type may set its own
    SUBTITLE_FONT = QFont("Segoe UI", 8)
    LABEL_FONT = QFont("Segoe UI", 7)

    def __init__(self, name):
        super().__init__()
        self.name = name
        self.params = {p.name: p.default for p in self.parameters}
        self.links = {}  # param name -> (source node, source param name): values that follow another node's
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.progress = None  # 0..1 while working, drawn as a bar along the bottom
        if self.HEIGHT is None:
            self.HEIGHT = round(self.WIDTH / node_shapes.aspect(self.shape_name))
        self._hover = False
        self.setAcceptHoverEvents(True)
        self.input_sockets = self._make_sockets(INPUT, self.inputs)
        self.output_sockets = self._make_sockets(OUTPUT, self.outputs)

    def _make_sockets(self, kind, specs):
        """Spread sockets along the top (inputs) or bottom (outputs), sitting on the shape's outline."""
        path = self.body_path()
        sockets = []
        for i, (name, data_type, *rest) in enumerate(specs):
            # an input takes ONE wire (several things in: a Merge node; more than 5: a Merge into a Merge);
            # an output feeds any number of inputs
            s = Socket(self, kind, name, data_type, multi=kind == OUTPUT and (rest[0] if rest else True))
            x = -self.WIDTH / 2 + self.WIDTH * (i + 1) / (len(specs) + 1)
            if kind == OUTPUT and self.OUTPUT_X:
                x = -self.WIDTH / 2 + self.WIDTH * self.OUTPUT_X[i]
            y = node_shapes.edge_y(path, x, top=kind == INPUT)
            s.setPos(x, y - 2 if kind == INPUT else y + 2)
            sockets.append(s)
        return sockets

    @property
    def input(self):
        return self.input_sockets[0] if self.input_sockets else None

    @property
    def output(self):
        return self.output_sockets[0] if self.output_sockets else None

    @property
    def sockets(self):
        return self.input_sockets + self.output_sockets

    def edges(self):
        return [e for s in self.sockets for e in s.edges]

    # ---- parameters ----------------------------------------------------
    def set_param(self, name, value):
        self.prepareGeometryChange()  # the subtitle may change width
        self.params[name] = value
        self.param_changed(name)
        self.notify()

    def notify(self):
        """Redraw and tell the scene (parameter panel, downstream nodes) that this node changed."""
        self._layout_cache = None
        self.prepareGeometryChange()
        self.update()
        if self.scene():
            self.scene().node_changed.emit(self)

    def param_changed(self, name):
        """Hook for node types that react to parameter edits."""

    def accepts_param(self, key):
        """May a saved / pasted value for `key` be restored? (A node whose fields come from elsewhere - a service
        model's inputs - also keeps values for fields it will only know later.)"""
        return key in self.params

    output_is_list = False  # the output hands on several values at once (a Merge)

    def accepts(self, socket, source):
        """Hook: may output socket `source` connect to this node's input `socket`? (Types are checked already.)"""
        return True

    def params_restored(self, names):
        """Hook: undo / redo put these parameters back (set directly). By default the node reacts as to an edit."""
        for name in names:
            self.param_changed(name)

    def readded(self):
        """Hook: undo brought this deleted node back into the scene."""

    def upstream_changed(self):
        """Hook: something in the graph changed (connections, or the parameters of a node feeding this one)."""

    def watches_graph(self):
        """True for a node that depends on nodes NOT wired into it (an Executor and what it built): it hears every
        node's changes, not only those of the nodes feeding it."""
        return False

    def action(self, name):
        """Hook: a button parameter was pressed."""

    def sources(self, input_name):
        """Nodes connected to the named input, in connection order."""
        sock = next((s for s in self.input_sockets if s.name == input_name), None)
        return [e.source.node for e in sock.edges] if sock else []

    def value(self):
        """What this node outputs (text, a file path, a model node...). None = nothing yet."""
        return None

    def input_values(self, input_name):
        """Values arriving at an input as a flat list (a Merge node contributes its whole list; a Merge of Merges
        its lists' contents, in order)."""
        out = []

        def add(v):
            if isinstance(v, list):
                for item in v:
                    add(item)
            elif v is not None:
                out.append(v)
        for src in self.sources(input_name):
            add(src.value())
        return out

    def named_values(self):
        """[(node name, value)] this node outputs - the name lets a model be told which input is which."""
        v = self.value()
        return [(self.name, v)] if v not in (None, "") else []

    def input_named(self, input_name):
        """[(source node name, value)] arriving at an input, in connection order (through Merges too)."""
        return [pair for src in self.sources(input_name) for pair in src.named_values()]

    def data_items(self, input_name):
        """[(name, value, kind)] arriving at an input: each item with what it is (item_kind). A node that keeps a
        list of items (Data, Delete) hands its list on as it is; a Merge's items take the Merge's type."""
        sock = next((s for s in self.input_sockets if s.name == input_name), None)
        out = []
        for e in sock.edges if sock else []:
            src = e.source.node
            if hasattr(src, "list_items"):
                out += src.list_items()
            else:
                out += [(n, v, item_kind(v, e.source.type)) for n, v in src.named_values()]
        return out

    def subtitle(self):
        """Short grey text drawn to the right of the node (Houdini-style)."""
        return ""

    def info_rows(self):
        """Read-only (label, value) rows for the parameter panel."""
        return []

    # ---- drawing (Houdini-style: shaped body, name block to the right) ------
    def problem(self):
        """Short text when the node can't work as connected (drawn red, with a badge)."""
        return ""

    def _rect(self):
        return QRectF(-self.WIDTH / 2, -self.HEIGHT / 2, self.WIDTH, self.HEIGHT)

    def body_path(self):
        return node_shapes.body(self.shape_name, self._rect())

    def _text_lines(self):
        """(text, font, color) for the block right of the body: type, name, info line."""
        lines = [(self.label, TYPE_FONT, TYPE_TEXT), (self.name, NAME_FONT, NAME_TEXT)]
        problem = self.problem()
        info = problem or self.subtitle()
        if info:
            lines.append((info, INFO_FONT, PROBLEM_TEXT if problem else INFO_TEXT))
        return lines

    def _text_layout(self):
        """[(QRectF, text, font, color)] for each line, vertically centred on the body. Kept briefly (paint,
        boundingRect and shape all ask, many times per redraw - the texts ask the node's problem / subtitle);
        notify() and a rename clear it."""
        now = time.monotonic()
        cached = getattr(self, "_layout_cache", None)
        if cached is not None and cached[0] == self.name and now - cached[1] < 0.25:
            return cached[2]
        lines = self._text_lines()
        heights = [_metrics(f).height() for _, f, _ in lines]
        y = -sum(heights) / 2
        x = self._rect().right() + 12
        out = []
        for (text, font, color), h in zip(lines, heights):
            w = _metrics(font).horizontalAdvance(text) + 4
            out.append((QRectF(x, y, w, h), text, font, color))
            y += h
        self._layout_cache = (self.name, now, out)
        return out

    def name_rect(self):
        return next(r for r, text, *_ in self._text_layout() if text == self.name)

    def boundingRect(self):
        # everything paint() draws must lie inside, or moving the node leaves traces behind: the "!" badge
        # (radius 7 at the left edge - 2), the socket labels (up to 31 above a socket, two rows), the progress bar
        r = self.body_path().boundingRect().adjusted(-12, -22, 4, 10)
        for s in getattr(self, "input_sockets", ()):  # (asked once before the sockets exist)
            r = r.united(QRectF(s.x() - 32, s.y() - 36, 64, 26))
        for tr, *_ in self._text_layout():
            r = r.united(tr)
        return r.adjusted(0, 0, 2, 0)

    def shape(self):
        path = QPainterPath(self.body_path())
        path.addRect(self.name_rect())  # clicking / double-clicking the name also hits the node
        return path

    def paint_body_overlay(self, painter, rect):
        """Hook: paint over the body (clipped to it) before its detail lines - e.g. a mode's pattern."""

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self._rect()
        path = self.body_path()
        top, bottom, detail, groove_color = self.body_colors
        grad = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        grad.setColorAt(0, top)
        grad.setColorAt(1, bottom)
        painter.setBrush(QBrush(grad))
        painter.setPen(QPen(SELECT_COLOR, 2.5) if self.isSelected() else QPen(BODY_BORDER, 1.2))
        painter.drawPath(path)
        # zoomed far out the fine inner lines can't be seen: skipped (redrawing a 450-node graph got ~2x faster)
        detailed = option is None or option.levelOfDetailFromTransform(painter.worldTransform()) >= 0.4

        # faint inner lines that give the shape its character
        painter.save()
        if detailed:
            painter.setClipPath(path)
        self.paint_body_overlay(painter, rect)  # e.g. a mode drawn on the body, under the shape's lines
        painter.setPen(QPen(detail, 1))
        painter.setBrush(Qt.NoBrush)
        for line in (node_shapes.details(self.shape_name, rect) if detailed else ()):
            if isinstance(line, QPainterPath):
                painter.drawPath(line)
            else:
                painter.drawLine(line)
        paths, width = node_shapes.grooves(self.shape_name, rect) if detailed else ((), None)
        for groove in paths:
            if width is None:  # ready-made shapes (e.g. the traced brain folds), thickened a touch
                painter.setPen(QPen(groove_color, max(0.8, rect.height() * 0.012), Qt.SolidLine, Qt.RoundCap,
                                    Qt.RoundJoin))
                painter.setBrush(groove_color)
                painter.drawPath(groove)
                painter.setBrush(Qt.NoBrush)
            else:
                pen = QPen(groove_color, width)
                pen.setCapStyle(Qt.RoundCap)
                pen.setJoinStyle(Qt.RoundJoin)
                painter.setPen(pen)
                painter.drawPath(groove)
        painter.restore()

        if self.problem():  # red "!" badge on the left edge
            c = QPointF(rect.left() - 2, rect.center().y())
            painter.setPen(QPen(QColor(30, 30, 30), 1))
            painter.setBrush(PROBLEM_TEXT)
            painter.drawEllipse(c, 7, 7)
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(QFont("Segoe UI", 8, QFont.Bold))
            painter.drawText(QRectF(c.x() - 7, c.y() - 8, 14, 16), Qt.AlignCenter, "!")

        labels = self.show_socket_labels
        if labels is True or (labels == "hover" and (self._hover or self.isSelected())):
            painter.setFont(self.LABEL_FONT)
            painter.setPen(TYPE_TEXT)
            spacing = self.WIDTH / (len(self.input_sockets) + 1)
            widest = max((QFontMetrics(self.LABEL_FONT).horizontalAdvance(s.name) for s in self.input_sockets), default=0)
            crowded = widest + 4 > spacing
            for i, s in enumerate(self.input_sockets):
                lift = 10 if crowded and i % 2 else 0  # two rows so neighbouring names don't overlap
                painter.drawText(QRectF(s.x() - 30, s.y() - 21 - lift, 60, 12), Qt.AlignCenter, s.name)

        if self.progress is not None:
            bar = QRectF(rect.left() + 4, path.boundingRect().bottom() + 5, rect.width() - 8, 3)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(20, 20, 20))
            painter.drawRect(bar)
            painter.setBrush(SELECT_COLOR)
            painter.drawRect(QRectF(bar.left(), bar.top(), bar.width() * self.progress, bar.height()))

        for r, text, font, color in self._text_layout():
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, text)

    def hoverEnterEvent(self, event):
        self._hover = True
        self.update()

    def hoverLeaveEvent(self, event):
        self._hover = False
        self.update()

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for edge in self.edges():
                edge.update_path()
            if self.scene():
                self.scene().modified.emit()
        return super().itemChange(change, value)

    def downstream_nodes(self):
        """All nodes reachable by following output connections."""
        seen, stack = set(), [self]
        while stack:
            node = stack.pop()
            for sock in node.output_sockets:
                for edge in sock.edges:
                    nxt = edge.target.node
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
        return seen

    def remove(self):
        for edge in self.edges():
            edge.remove()
        if self.scene():
            from . import links
            links.forget(self.scene(), self)  # parameters that followed this node keep their values
            self.scene().removeItem(self)
