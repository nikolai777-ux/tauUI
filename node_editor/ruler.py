"""Number field with a ruler: type a value, or drag the scale under the marker (like a tuning dial).

Drag left/right = scrub (Shift fine, Ctrl coarse), wheel = one tick, double-click the ruler = the default value.
Ticks click as they pass the marker (File > Preferences > UI turns the sound off). The value is committed when
the drag ends, so one drag is one edit.
"""

import math
import os

from PySide6.QtCore import QPointF, QRectF, QRegularExpression, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QRegularExpressionValidator
from PySide6.QtWidgets import QHBoxLayout, QLineEdit, QSizePolicy, QWidget

ASSETS = os.path.join(os.path.dirname(__file__), "assets")
TICK_PX = 9          # screen distance between two ticks
MAJOR_EVERY = 5      # every 5th tick is taller and labelled
MARKER = QColor(230, 90, 80)


class _Sound:
    """Two short click samples, loaded once (QtMultimedia is imported only when a ruler first ticks)."""
    effects = None

    @classmethod
    def play(cls, major):
        from .settings import tick_sound
        if not tick_sound():
            return
        if cls.effects is None:
            from PySide6.QtMultimedia import QSoundEffect
            cls.effects = {}
            for key, name in ((False, "tick.wav"), (True, "tick_major.wav")):
                e = QSoundEffect()
                e.setSource(QUrl.fromLocalFile(os.path.join(ASSETS, name)))
                e.setVolume(0.6)
                cls.effects[key] = e
        cls.effects[major].play()


class Ruler(QWidget):
    """The scale. value is in the parameter's own units; step = one tick."""

    scrubbed = Signal(float)   # while dragging / wheeling
    released = Signal(float)   # a drag ended: commit

    def __init__(self, value, step, minimum, maximum, default, is_int):
        super().__init__()
        self.value, self.step, self.min, self.max = value, step, minimum, maximum
        self.default, self.is_int = default, is_int
        self._drag = None
        self.locked = False  # linked to another parameter: shows the value, can't be changed here
        self._last_tick = self._tick_index(value)
        self.setCursor(Qt.SizeHorCursor)
        self.setToolTip("Drag to change (Shift: fine, Ctrl: coarse) · wheel: one tick · double-click: default")

    # ---- values -------------------------------------------------------------
    def _tick_index(self, v):
        return math.floor(v / self.step + 1e-9)

    def _clean(self, v):
        v = max(self.min, min(self.max, v))
        return int(round(v)) if self.is_int else round(v, 6)

    def set_value(self, v, sound=False):
        self.value = v
        tick = self._tick_index(v)
        if sound and tick != self._last_tick:
            _Sound.play(tick % MAJOR_EVERY == 0)
        self._last_tick = tick
        self.update()

    def _fine_coarse(self, modifiers):
        if modifiers & Qt.ShiftModifier:
            return 1 if self.is_int else self.step / 10, 0.1
        if modifiers & Qt.ControlModifier:
            return self.step * 10, 10.0
        return self.step, 1.0

    # ---- mouse --------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and not self.locked:
            self._drag = (e.position().x(), self.value)

    def mouseMoveEvent(self, e):
        if self._drag is None:
            return
        x0, v0 = self._drag
        snap, speed = self._fine_coarse(e.modifiers())
        v = v0 - (e.position().x() - x0) / TICK_PX * self.step * speed  # the scale moves with the mouse
        v = self._clean(round(v / snap) * snap)
        if v != self.value:
            self.set_value(v, sound=True)
            self.scrubbed.emit(v)

    def mouseReleaseEvent(self, e):
        if self._drag is not None:
            self._drag = None
            self.released.emit(self.value)

    def mouseDoubleClickEvent(self, e):
        if self.locked:
            return
        self.set_value(self._clean(self.default))
        self.scrubbed.emit(self.value)
        self.released.emit(self.value)

    def wheelEvent(self, e):
        if self.locked:
            return e.ignore()
        snap, _ = self._fine_coarse(e.modifiers())
        direction = 1 if e.angleDelta().y() > 0 else -1
        v = self._clean(round(self.value / snap) * snap + direction * snap)
        if v != self.value:
            self.set_value(v, sound=True)
            self.scrubbed.emit(v)
            self.released.emit(v)
        e.accept()

    # ---- drawing ------------------------------------------------------------
    def _label(self, v):
        if self.is_int:
            return str(int(round(v)))
        return f"{v:.2f}".rstrip("0").rstrip(".") if abs(v) < 100 else f"{v:.0f}"

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(68, 68, 68)))
        p.setBrush(QColor(58, 58, 58) if self.locked else QColor(30, 30, 30))  # grey when linked
        p.drawRoundedRect(r, 2, 2)
        cx = r.center().x()
        base = r.top() + r.height() * 0.52   # ticks hang from the top; labels below
        font = QFont(self.font())
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.72))
        p.setFont(font)
        first = self._tick_index(self.value) - int(r.width() / 2 / TICK_PX) - 1
        count = int(r.width() / TICK_PX) + 3
        labels_end = -1e9  # right edge of the last label drawn - long numbers skip a label rather than overlap
        for k in range(first, first + count):
            t = k * self.step
            x = cx + (t - self.value) / self.step * TICK_PX
            if x < r.left() + 2 or x > r.right() - 2:
                continue
            outside = t < self.min - 1e-9 or t > self.max + 1e-9
            major = k % MAJOR_EVERY == 0
            fade = (1 - min(1.0, abs(x - cx) / (r.width() / 2))) ** 1.6  # darker towards both ends, like a dial
            shade = 55 if outside else int(52 + 185 * fade)
            p.setPen(QPen(QColor(shade, shade, shade), 1.2 if major else 1))
            p.drawLine(QPointF(x, r.top() + 3), QPointF(x, base if major else r.top() + 3 + (base - r.top() - 3) * 0.55))
            if major and not outside:
                text = self._label(t)
                half = p.fontMetrics().horizontalAdvance(text) / 2 + 3
                if x - half > labels_end and x - half >= r.left() + 2 and x + half <= r.right() - 2:  # whole labels only
                    p.setPen(QColor(shade, shade, shade))
                    p.drawText(QRectF(x - half, base, 2 * half, r.bottom() - base), Qt.AlignCenter, text)
                    labels_end = x + half
        p.setPen(QPen(MARKER, 2))
        p.drawLine(QPointF(cx, r.top() + 1), QPointF(cx, base + 1))


class RulerField(QWidget):
    """[ typed value ] [ ruler ] - edits one number parameter."""

    committed = Signal(object)  # the new value (int or float), when an edit is finished

    def __init__(self, value, kind, step, minimum, maximum, default):
        super().__init__()
        self.is_int = kind == "int"
        self.edit = QLineEdit()
        # digits, a minus, and for decimals either "." or "," (Qt's number validators drop a typed comma)
        self.minimum = minimum
        pattern = r"[+-]?\d*" if self.is_int else r"[+-]?\d*([.,]\d*)?"
        self.edit.setValidator(QRegularExpressionValidator(QRegularExpression(pattern), self.edit))
        self.edit.setFixedWidth(self.fontMetrics().horizontalAdvance("0000000000") + 14)  # room for a seed
        self.ruler = Ruler(value, step, minimum, maximum, default, self.is_int)
        # flush with the field: the row is the field's height (font + style, so it follows the UI scale) and the
        # ruler fills it top to bottom
        self.ruler.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        # ...and never taller: spare height in the pane (folded boxes, nothing else growing) went to the ruler row
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(self.edit.sizeHint().height())
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.edit)
        lay.addWidget(self.ruler, 1)
        self._show(value)
        self.ruler.scrubbed.connect(self._show)
        self.ruler.released.connect(self._commit)
        self.edit.editingFinished.connect(self._typed)

    def showEvent(self, event):
        super().showEvent(event)
        self.setFixedHeight(self.edit.sizeHint().height())  # the field's height once its style is applied

    def _format(self, v):
        return str(int(v)) if self.is_int else f"{float(v):.3f}"

    def _show(self, v):
        self.edit.setText(self._format(v))

    def _typed(self):
        if not self.edit.isModified():  # focus left without typing: nothing to commit (the text may be stale)
            return
        self.edit.setModified(False)
        text = self.edit.text().replace(",", ".").strip()
        # "+5" adds 5 to the value; so does "-5" subtract where the value can't be negative (e.g. a size)
        relative = text[:1] == "+" or (text[:1] == "-" and self.minimum is not None and self.minimum >= 0)
        try:
            v = float(text)
        except ValueError:
            self._show(self.ruler.value)
            return
        if relative:
            v = self.ruler.value + v
            if self.minimum is not None:
                v = max(self.minimum, v)
        v = int(round(v)) if self.is_int else v
        self.ruler.set_value(v)  # typing may go beyond the ruler's range on purpose
        self._show(v)
        self.committed.emit(v)

    def _commit(self, v):
        self.committed.emit(int(v) if self.is_int else float(v))

    def set_locked(self, on):
        """Linked to another parameter: greyed, can't be typed in, dragged or wheeled."""
        self.edit.setReadOnly(on)
        self.ruler.locked = on
        self.ruler.setCursor(Qt.ArrowCursor if on else Qt.SizeHorCursor)
        self.ruler.update()

    def set_value(self, v):
        """The node changed the value itself."""
        self.ruler.set_value(v)
        if not self.edit.isModified():  # unless the user is typing in it right now
            self._show(v)
