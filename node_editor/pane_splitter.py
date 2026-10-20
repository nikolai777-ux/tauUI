"""Houdini-style divider: drag to resize; small arrows fold a side away and bring it back."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QSplitter, QSplitterHandle, QToolButton, QVBoxLayout

HANDLE_WIDTH = 12
ARROW_STYLE = """
QToolButton { background: #3a3a3a; border: 1px solid #222; border-radius: 2px; padding: 0; }
QToolButton:hover { background: #4a6a8a; }
"""


class ArrowHandle(QSplitterHandle):
    def __init__(self, orientation, splitter):
        super().__init__(orientation, splitter)
        horizontal = orientation == Qt.Horizontal
        first, second = ("left", "right") if horizontal else ("top", "bottom")
        self.first_btn = self._arrow(Qt.LeftArrow if horizontal else Qt.UpArrow, horizontal,
                                     f"Fold the {first} side away / bring the {second} side back",
                                     lambda: splitter.shift(-1))
        self.second_btn = self._arrow(Qt.RightArrow if horizontal else Qt.DownArrow, horizontal,
                                      f"Fold the {second} side away / bring the {first} side back",
                                      lambda: splitter.shift(+1))
        box = QVBoxLayout(self) if horizontal else QHBoxLayout(self)
        box.setContentsMargins(1, 0, 1, 0) if horizontal else box.setContentsMargins(0, 1, 0, 1)
        box.setSpacing(3)
        box.addStretch()
        box.addWidget(self.first_btn)
        box.addWidget(self.second_btn)
        box.addStretch()

    def _arrow(self, arrow, horizontal, tip, slot):
        b = QToolButton(self)
        b.setArrowType(arrow)
        b.setFixedSize(HANDLE_WIDTH - 2, 18) if horizontal else b.setFixedSize(18, HANDLE_WIDTH - 2)
        b.setToolTip(tip)
        b.setCursor(Qt.ArrowCursor)
        b.setStyleSheet(ARROW_STYLE)
        b.clicked.connect(slot)
        return b

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(40, 40, 40))
        p.setPen(QColor(25, 25, 25))
        if self.orientation() == Qt.Horizontal:
            p.drawLine(0, 0, 0, self.height())
            p.drawLine(self.width() - 1, 0, self.width() - 1, self.height())
        else:
            p.drawLine(0, 0, self.width(), 0)
            p.drawLine(0, self.height() - 1, self.width(), self.height() - 1)
        p.end()


class PaneSplitter(QSplitter):
    """Two panes side by side (or one over the other). The arrows move the divider all the way in their
    direction, or back to where it was if that side was already folded away."""

    def __init__(self, left, right, right_width=340, left_width=None, orientation=Qt.Horizontal):
        """The side given a size keeps it when the window resizes (right/bottom by default); the other
        stretches. With Qt.Vertical, left = top and right = bottom."""
        super().__init__(orientation)
        self.setHandleWidth(HANDLE_WIDTH)
        self.setChildrenCollapsible(True)
        self.addWidget(left)
        self.addWidget(right)
        self._fixed_left = left_width is not None
        self._width = left_width if self._fixed_left else right_width
        self.setStretchFactor(0, 0 if self._fixed_left else 1)
        self.setStretchFactor(1, 1 if self._fixed_left else 0)
        self._saved = None

    def _default_sizes(self, total):
        rest = max(0, total - self._width)
        return [self._width, rest] if self._fixed_left else [rest, self._width]

    def createHandle(self):
        return ArrowHandle(self.orientation(), self)

    def showEvent(self, event):
        super().showEvent(event)
        if self._saved is None:  # first show: give the fixed side its default size
            self.setSizes(self._default_sizes(sum(self.sizes())))
            self._saved = self.sizes()

    def shift(self, direction):
        left, right = self.sizes()
        total = left + right
        if direction > 0:  # ▶ / ▼
            if left == 0:
                self._restore(total)
            elif right > 0:
                self._saved = [left, right]
                self.setSizes([total, 0])
        else:  # ◀ / ▲
            if right == 0:
                self._restore(total)
            elif left > 0:
                self._saved = [left, right]
                self.setSizes([0, total])

    def _restore(self, total):
        self.setSizes(self._saved if self._saved and all(self._saved) else self._default_sizes(total))

    def is_right_folded(self):
        return self.sizes()[1] == 0

    def is_left_folded(self):
        return self.sizes()[0] == 0
