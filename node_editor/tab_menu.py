from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import QFrame, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout

STYLE = """
QFrame#TabMenu { background: #2b2b2b; border: 1px solid #555; }
QLineEdit { background: #1e1e1e; color: #ddd; border: 1px solid #444; padding: 4px; }
QListWidget { background: #2b2b2b; color: #ddd; border: none; outline: none; }
QListWidget::item { padding: 3px 6px; }
QListWidget::item:selected { background: #4a6a8a; }
"""


class TabMenu(QFrame):
    """Houdini-style popup: type to filter, Enter/click to create a node."""

    node_chosen = Signal(str)  # emits the node type_name

    def __init__(self, parent, node_types):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("TabMenu")
        self.setStyleSheet(STYLE)
        self.node_types = node_types

        self.search = QLineEdit(placeholderText="Search nodes...")
        self.list = QListWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addWidget(self.search)
        layout.addWidget(self.list)
        self.setFixedWidth(220)

        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        self.list.itemClicked.connect(self._choose)
        self.list.itemActivated.connect(self._choose)

    def popup(self, global_pos):
        self.search.clear()
        self._filter("")
        self.move(global_pos)
        self.show()
        self.search.setFocus()

    def _filter(self, text):
        self.list.clear()
        text = text.lower().strip()
        for type_name, cls in sorted(self.node_types.items(), key=lambda kv: kv[1].label):
            if text in cls.label.lower() or text in type_name:
                item = QListWidgetItem(cls.label)
                item.setData(Qt.UserRole, type_name)
                self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)
        # size to the real row height (the padding makes rows taller than the font), up to 14 rows, then scroll
        row_h = self.list.sizeHintForRow(0) if self.list.count() else 24
        rows = max(1, min(self.list.count(), 14))
        self.list.setFixedHeight(rows * row_h + 2 * self.list.frameWidth() + 4)
        self.adjustSize()

    def _choose(self, item):
        self.hide()
        self.node_chosen.emit(item.data(Qt.UserRole))

    def eventFilter(self, obj, event):
        # Arrow keys / Enter in the search box drive the list.
        if obj is self.search and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up):
                row = self.list.currentRow() + (1 if key == Qt.Key_Down else -1)
                if 0 <= row < self.list.count():
                    self.list.setCurrentRow(row)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                if self.list.currentItem():
                    self._choose(self.list.currentItem())
                return True
            if key == Qt.Key_Tab:
                self.hide()
                return True
        return super().eventFilter(obj, event)
