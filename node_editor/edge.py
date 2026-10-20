from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPainterPathStroker, QPen
from PySide6.QtWidgets import QGraphicsItem, QGraphicsPathItem

EDGE_COLOR = QColor(170, 170, 170)
EDGE_SELECTED_COLOR = QColor(255, 200, 60)


def bezier_path(start: QPointF, end: QPointF) -> QPainterPath:
    """Vertical S-curve from an output (top) down to an input (bottom)."""
    dy = max(abs(end.y() - start.y()) * 0.5, 40.0)
    path = QPainterPath(start)
    path.cubicTo(start + QPointF(0, dy), end - QPointF(0, dy), end)
    return path


class Edge(QGraphicsPathItem):
    """A connection from an output socket to an input socket."""

    def __init__(self, source, target):
        super().__init__()
        self.source = source  # output socket
        self.target = target  # input socket
        self.setZValue(-1)
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        source.edges.append(self)
        target.edges.append(self)
        target.update()  # the input dot takes the output's colour
        self.update_path()

    def update_path(self):
        self.setPath(bezier_path(self.source.scenePos(), self.target.scenePos()))

    def shape(self):
        stroker = QPainterPathStroker()
        stroker.setWidth(10)  # generous click target
        return stroker.createStroke(self.path())

    def paint(self, painter, option, widget=None):
        from .node import ANY
        if self.isSelected():
            color = EDGE_SELECTED_COLOR
        elif self.source.type == ANY and self.source.color_override is None:
            color = EDGE_COLOR
        else:
            color = self.source.display_color()  # coloured like its output socket
        painter.setPen(QPen(color, 2.0))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())

    def remove(self):
        for socket in (self.source, self.target):
            if self in socket.edges:
                socket.edges.remove(self)
            socket.update()
        scene = self.scene()
        if scene:
            scene.removeItem(self)
            scene.wiring_changed(self.source.node, self.target.node)
