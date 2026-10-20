"""Render every node shape as a real node into shape_gallery.png (for choosing shapes)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from PySide6.QtWidgets import QApplication, QGraphicsSimpleTextItem

from node_editor.node import Node
from node_editor.node_shapes import SHAPES
from node_editor.registry import NODE_TYPES
from node_editor.view import NodeScene

app = QApplication(sys.argv)
scene = NodeScene()

used_by = {}
for cls in NODE_TYPES.values():
    used_by.setdefault(cls.shape_name, []).append(cls)

COLS, DX, DY = 4, 330, 140
for i, name in enumerate(SHAPES):
    real = used_by.get(name, [None])[0]
    attrs = {"shape_name": name, "label": "shape"}
    if real is not None:  # show it exactly as that node type: its size and sockets
        attrs.update(WIDTH=real.WIDTH, HEIGHT=real.HEIGHT, OUTPUT_X=real.OUTPUT_X,
                     inputs=real.inputs, outputs=real.outputs, show_socket_labels=real.show_socket_labels)
    cls = type(f"Shape_{name}", (Node,), attrs)
    node = cls(name)
    node.setPos(QPointF((i % COLS) * DX, (i // COLS) * DY))
    scene.addItem(node)
    if name in used_by:
        tag = QGraphicsSimpleTextItem("used by: " + ", ".join(sorted(c.label for c in used_by[name])))
        tag.setBrush(QColor(255, 200, 60))
        tag.setFont(QFont("Segoe UI", 8))
        tag.setPos(node.pos() + QPointF(-node.WIDTH / 2, node.HEIGHT / 2 + 12))
        scene.addItem(tag)

rect = scene.itemsBoundingRect().adjusted(-40, -40, 40, 40)
img = QImage(int(rect.width()), int(rect.height()), QImage.Format_ARGB32)
img.fill(QColor(38, 38, 38))
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)
p.setRenderHint(QPainter.TextAntialiasing)
scene.render(p, QRectF(img.rect()), rect)
p.end()
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "shape_gallery.png")
img.save(out)
print("saved", out, img.size().toTuple())

