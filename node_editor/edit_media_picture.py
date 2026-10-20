"""The Edit Media node's picture: the result (a video: the edited frame at the playhead) - with a crop box in crop
mode, or a pen in draw mode.

Crop mode (the crop icon): the source image as it comes in, what will be cut away darkened, and a box with
corner / edge handles - drag an edge or a corner, drag inside to move it, drag outside it to draw a new one;
double-click = no crop. Like Photoshop, nothing changes until Enter: Enter applies the box and leaves crop mode,
Esc cancels. The crop is applied first (to the source's own pixels), so this view shows the source unrotated.

Draw mode (the pen icon): draw on the result in the node's pen colour and thickness; each stroke goes to the
node when the mouse is released and is burned into the image that goes downstream (e.g. to point at or outline
something for a model). Strokes are kept relative to the picture, so they follow a later resize.
"""

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import undo

HANDLE = 7          # handle size in screen pixels
GRAB = 9            # how close to an edge counts as grabbing it
BOX = QColor(255, 255, 255)


def stroke_path(points):
    path = QPainterPath(QPointF(*points[0]))
    for x, y in points[1:]:
        path.lineTo(x, y)
    return path


class MediaPicture(QWidget):
    def __init__(self, node, parent):
        super().__init__(parent)  # parented up front: showing itself must never make a (briefly flashing) window
        self.node = node
        self._pix, self._pix_path = QPixmap(), ""
        self._mode = None
        self._box = None          # crop box not applied yet (image pixels: left, top, right, bottom edges)
        self._drag = None         # (what, start image point, box at start)
        self._stroke = None       # the stroke being drawn (image pixels)
        self._drawn = []          # strokes handed to the node, shown until the new result arrives
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.refresh(node)

    # ---- state from the node -------------------------------------------------------------------------------
    @property
    def mode(self):
        return self.node.params.get("tools") or ""

    def refresh(self, node):
        self.node = node
        if self.mode != self._mode:  # left (or entered) a mode: a box not applied is dropped
            self._mode, self._box, self._drag = self.mode, None, None
        path = node.picture()  # crop mode: the source as it comes in; else the result (a video: the frame shown)
        if path != self._pix_path:
            self._pix, self._pix_path = (QPixmap(path) if path else QPixmap()), path
            self._drawn = []  # the new result has them burned in
            self.updateGeometry()
        self.setCursor(Qt.CrossCursor if self.mode in ("crop", "draw") else Qt.ArrowCursor)
        self.setVisible(not self._pix.isNull())
        self.update()

    def _node_box(self):
        p, w, h = self.node.params, self._pix.width(), self._pix.height()
        return [p["crop_left"], p["crop_top"], w - p["crop_right"], h - p["crop_bottom"]]

    # ---- geometry ---------------------------------------------------------------------------------------------
    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        if self._pix.isNull() or not self._pix.width():
            return 0
        return int(min(w * self._pix.height() / self._pix.width(), w * 1.25))  # portrait: fitted, not pane-long

    def sizeHint(self):
        return QSize(300, self.heightForWidth(300) or 1)

    def _image_rect(self):
        if self._pix.isNull():
            return QRectF()
        scale = min(self.width() / self._pix.width(), self.height() / self._pix.height())
        w, h = self._pix.width() * scale, self._pix.height() * scale
        return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)

    def _to_screen(self, box):
        r = self._image_rect()
        k = r.width() / self._pix.width()
        return QRectF(r.left() + box[0] * k, r.top() + box[1] * k, (box[2] - box[0]) * k, (box[3] - box[1]) * k)

    def _to_image(self, pos):
        r = self._image_rect()
        k = self._pix.width() / r.width()
        x = min(max((pos.x() - r.left()) * k, 0), self._pix.width())
        y = min(max((pos.y() - r.top()) * k, 0), self._pix.height())
        return QPointF(x, y)

    def _hit(self, pos):
        """Which part of the box is under the mouse: e.g. 'lt' (corner), 'r' (edge), 'move', or 'new'."""
        s = self._to_screen(self._box or self._node_box())
        near = lambda a, b: abs(a - b) <= GRAB  # noqa: E731
        inside_y = s.top() - GRAB <= pos.y() <= s.bottom() + GRAB
        inside_x = s.left() - GRAB <= pos.x() <= s.right() + GRAB
        part = ""
        if inside_y and near(pos.x(), s.left()):
            part += "l"
        elif inside_y and near(pos.x(), s.right()):
            part += "r"
        if inside_x and near(pos.y(), s.top()):
            part += "t"
        elif inside_x and near(pos.y(), s.bottom()):
            part += "b"
        if part:
            return part
        return "move" if s.contains(pos) else "new"

    # ---- mouse + keys -----------------------------------------------------------------------------------------
    def mousePressEvent(self, e):
        if self._pix.isNull() or e.button() != Qt.LeftButton:
            return
        self.setFocus()  # Enter / Esc come here
        if self.mode == "draw":
            pt = self._to_image(e.position())
            self._stroke = [(pt.x(), pt.y())]
            return
        if self.mode != "crop":
            return
        what = self._hit(e.position())
        box = self._box or self._node_box()
        start = self._to_image(e.position())
        if what == "move" and box == [0, 0, self._pix.width(), self._pix.height()]:
            what = "new"  # no crop yet: dragging draws the box (the whole picture can't be moved)
        if what == "new":
            box = [start.x(), start.y(), start.x(), start.y()]
            what = "rb"
        self._box = list(box)
        self._drag = (what, start, list(box))

    def mouseMoveEvent(self, e):
        if self.mode == "draw" and self._stroke is not None:
            pt = self._to_image(e.position())
            if (pt.x(), pt.y()) != self._stroke[-1]:
                self._stroke.append((pt.x(), pt.y()))
                self.update()
            return
        if self.mode != "crop" or self._pix.isNull():
            return
        if self._drag is None:
            cursors = {"l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
                       "lt": Qt.SizeFDiagCursor, "rb": Qt.SizeFDiagCursor, "rt": Qt.SizeBDiagCursor,
                       "lb": Qt.SizeBDiagCursor, "move": Qt.SizeAllCursor, "new": Qt.CrossCursor}
            self.setCursor(cursors[self._hit(e.position())])
            return
        what, start, box0 = self._drag
        pt = self._to_image(e.position())
        dx, dy = pt.x() - start.x(), pt.y() - start.y()
        w, h = self._pix.width(), self._pix.height()
        l, t, r, b = box0
        if what == "move":
            dx = min(max(dx, -l), w - r)
            dy = min(max(dy, -t), h - b)
            l, r, t, b = l + dx, r + dx, t + dy, b + dy
        else:
            if "l" in what: l = min(pt.x(), r - 1)
            if "r" in what: r = max(pt.x(), l + 1)
            if "t" in what: t = min(pt.y(), b - 1)
            if "b" in what: b = max(pt.y(), t + 1)
        self._box = [l, t, r, b]
        self.update()

    def mouseReleaseEvent(self, e):
        if self.mode == "draw" and self._stroke is not None:
            stroke, self._stroke = self._stroke, None
            if len(stroke) == 1:  # a click: a dot
                stroke.append((stroke[0][0] + 0.01, stroke[0][1]))
            w, h = self._pix.width(), self._pix.height()
            self._drawn.append((stroke, self.node.params["pen_color"], self.node.params["pen_width"]))
            with undo.record(self.node, "Draw"):
                self.node.add_stroke([(x / w, y / h) for x, y in stroke], w)
            self.update()
            return
        if self._drag is None:
            return
        self._drag = None
        l, t, r, b = self._box
        if r - l < 2 or b - t < 2:  # a click, not a box
            self._box = None
        self.update()  # the box waits for Enter

    def mouseDoubleClickEvent(self, e):
        if self.mode == "crop":  # no crop (still applied with Enter)
            self._box = [0, 0, self._pix.width(), self._pix.height()]
            self.update()

    def keyPressEvent(self, e):
        if self.mode == "crop" and e.key() in (Qt.Key_Return, Qt.Key_Enter):
            with undo.record(self.node, "Crop"):
                if self._box is not None:
                    l, t, r, b = (round(v) for v in self._box)
                    self.node.set_crop(l, t, self._pix.width() - r, self._pix.height() - b)
                self.node.set_param("tools", "")  # applied: leave crop mode (the crop icon pops out)
        elif self.mode in ("crop", "draw") and e.key() == Qt.Key_Escape:
            self._box = None
            with undo.record(self.node, "Leave " + self.mode + " mode"):
                self.node.set_param("tools", "")
        else:
            super().keyPressEvent(e)

    # ---- drawing -----------------------------------------------------------------------------------------------
    def paintEvent(self, _):
        if self._pix.isNull():
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        img = self._image_rect()
        p.drawPixmap(img, self._pix, QRectF(self._pix.rect()))
        if self.mode == "draw" or self._drawn:
            self._paint_strokes(p, img)
        if self.mode == "crop":
            self._paint_crop(p, img)

    def _paint_strokes(self, p, img):
        """Strokes not yet in the result: the one being drawn and those handed to the node."""
        p.setRenderHint(QPainter.Antialiasing)
        k = img.width() / self._pix.width()
        p.save()
        p.translate(img.topLeft())
        p.scale(k, k)
        pending = list(self._drawn)
        if self._stroke:
            pending.append((self._stroke, self.node.params["pen_color"], self.node.params["pen_width"]))
        for points, color, width in pending:
            p.setPen(QPen(QColor(color), width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(stroke_path(points))
        p.restore()

    def _paint_crop(self, p, img):
        p.setRenderHint(QPainter.Antialiasing)
        box = self._box or self._node_box()
        s = self._to_screen(box)
        shade = QColor(0, 0, 0, 150)  # what gets cut away
        for rect in (QRectF(img.left(), img.top(), img.width(), s.top() - img.top()),
                     QRectF(img.left(), s.bottom(), img.width(), img.bottom() - s.bottom()),
                     QRectF(img.left(), s.top(), s.left() - img.left(), s.height()),
                     QRectF(s.right(), s.top(), img.right() - s.right(), s.height())):
            if rect.width() > 0 and rect.height() > 0:
                p.fillRect(rect, shade)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))  # thirds, as in a camera / Photoshop crop
        for i in (1, 2):
            p.drawLine(QPointF(s.left() + s.width() * i / 3, s.top()), QPointF(s.left() + s.width() * i / 3, s.bottom()))
            p.drawLine(QPointF(s.left(), s.top() + s.height() * i / 3), QPointF(s.right(), s.top() + s.height() * i / 3))
        p.setPen(QPen(BOX, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawRect(s)
        p.setBrush(BOX)
        p.setPen(QPen(QColor(0, 0, 0), 1))
        for x in (s.left(), s.center().x(), s.right()):
            for y in (s.top(), s.center().y(), s.bottom()):
                if (x, y) != (s.center().x(), s.center().y()):
                    p.drawRect(QRectF(x - HANDLE / 2, y - HANDLE / 2, HANDLE, HANDLE))
        text = f"{round(box[2] - box[0])} × {round(box[3] - box[1])}"
        if self._box is not None:
            text += "   Enter ↵ apply · Esc cancel"
        tw = p.fontMetrics().horizontalAdvance(text) + 10
        th = p.fontMetrics().height() + 4
        tag = QRectF(min(max(s.center().x() - tw / 2, img.left() + 2), img.right() - tw - 2),
                     max(img.top() + 2, s.top() - th - 4), tw, th)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 170))
        p.drawRoundedRect(tag, 3, 3)
        p.setPen(QColor(255, 255, 255))
        p.drawText(tag, Qt.AlignCenter, text)
