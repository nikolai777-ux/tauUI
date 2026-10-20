"""Tabs above the graph (like Houdini's network tabs): each tab holds its own graph; all tabs live in one file."""

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout, QLineEdit, QStackedWidget, QTabBar, QToolButton, QVBoxLayout, QWidget)

from .view import NodeScene, NodeView

STYLE = """
#GraphTabs { background: #2b2b2b; }
QTabBar { background: #2b2b2b; }
QTabBar::tab { background: #333; color: #aaa; border: 1px solid #222; border-bottom: none;
               padding: 4px 8px 4px 10px; margin-right: 1px; }
QTabBar::tab:selected { background: #464646; color: #eee; }
QTabBar::tab:hover:!selected { background: #3b3b3b; }
QToolButton#AddTab { background: transparent; color: #ccc; border: none; font-size: 16px; font-weight: bold;
                     padding: 0 6px; }
QToolButton#AddTab:hover { color: #fff; background: #3b3b3b; }
QLineEdit#TabRename { background: #1e1e1e; color: #fff; border: 1px solid #ffc83c; padding: 0 4px; }
"""


class GraphDoc:
    """One tab: its name, scene and view."""

    def __init__(self, name):
        self.name = name
        self.scene = NodeScene()
        self.view = NodeView(self.scene)

    def close(self):
        for node in self.scene.nodes():  # lets Modality nodes cancel running jobs
            node.remove()
        self.view.deleteLater()
        self.scene.deleteLater()


class GraphTabs(QWidget):
    current_changed = Signal(object)    # GraphDoc now shown
    close_requested = Signal(object)    # GraphDoc whose × was clicked (the window decides)
    add_requested = Signal()
    changed = Signal()                  # tabs renamed or reordered (worth saving)

    def __init__(self):
        super().__init__(objectName="GraphTabs")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet(STYLE)
        self.docs = []
        self._editor = None

        self.bar = QTabBar()
        self.bar.setTabsClosable(True)
        self.bar.setMovable(True)
        self.bar.setExpanding(False)
        self.bar.setDocumentMode(True)
        self.bar.setFocusPolicy(Qt.NoFocus)
        self.bar.installEventFilter(self)
        self.add_button = QToolButton(objectName="AddTab", text="+", toolTip="New tab (Ctrl+T)")
        self.add_button.setFocusPolicy(Qt.NoFocus)
        self.stack = QStackedWidget()

        top = QHBoxLayout()
        top.setContentsMargins(0, 2, 0, 0)
        top.setSpacing(2)
        top.addWidget(self.bar)
        top.addWidget(self.add_button)
        top.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(top)
        layout.addWidget(self.stack, 1)

        self.add_button.clicked.connect(self.add_requested)
        self.bar.currentChanged.connect(self._on_current)
        self.bar.tabCloseRequested.connect(lambda i: self.close_requested.emit(self._doc_at(i)))
        self.bar.tabMoved.connect(self._on_moved)

    def _doc_at(self, index):
        return self.bar.tabData(index)

    def _index_of(self, doc):
        for i in range(self.bar.count()):
            if self._doc_at(i) is doc:
                return i
        return -1

    def current(self):
        i = self.bar.currentIndex()
        return self._doc_at(i) if i >= 0 else None

    def unique_name(self, base="graph"):
        taken = {d.name for d in self.docs}
        n = 1
        while f"{base}{n}" in taken:
            n += 1
        return f"{base}{n}"

    def add(self, doc):
        self.docs.append(doc)
        self.stack.addWidget(doc.view)
        self.bar.blockSignals(True)  # the first tab becomes current before its data is set
        i = self.bar.addTab(doc.name)
        self.bar.setTabData(i, doc)
        self.bar.setCurrentIndex(i)
        self.bar.blockSignals(False)
        self._on_current(i)
        return doc

    def remove(self, doc):
        self.docs.remove(doc)
        self.stack.removeWidget(doc.view)
        self.bar.removeTab(self._index_of(doc))
        doc.close()

    def select(self, doc):
        self.bar.setCurrentIndex(self._index_of(doc))

    def _on_current(self, index):
        doc = self._doc_at(index) if index >= 0 else None
        if doc is not None:
            self.stack.setCurrentWidget(doc.view)
            doc.view.setFocus()
        self.current_changed.emit(doc)

    def _on_moved(self, _from, _to):
        self.docs = [self._doc_at(i) for i in range(self.bar.count())]
        self.changed.emit()

    # ---- renaming: double-click a tab ------------------------------------
    def eventFilter(self, obj, event):
        if obj is self.bar and event.type() == QEvent.MouseButtonDblClick:
            i = self.bar.tabAt(event.position().toPoint())
            if i >= 0:
                self.rename(self._doc_at(i))
                return True
        return super().eventFilter(obj, event)

    def rename(self, doc):
        i = self._index_of(doc)
        if i < 0:
            return
        self._finish_rename(False)
        rect = self.bar.tabRect(i)
        editor = QLineEdit(doc.name, self.bar, objectName="TabRename")
        rect.setWidth(max(rect.width(), 120))
        editor.setGeometry(rect.adjusted(2, 2, -2, -2))
        editor.doc = doc
        editor.selectAll()
        editor.show()
        editor.setFocus()
        editor.editingFinished.connect(lambda: self._finish_rename(True))  # Enter or clicking elsewhere
        QShortcut(QKeySequence(Qt.Key_Escape), editor, lambda: self._finish_rename(False),
                  context=Qt.WidgetShortcut)
        self._editor = editor

    def _finish_rename(self, commit):
        editor, self._editor = self._editor, None
        if editor is None:
            return
        name = editor.text().strip()
        doc = editor.doc
        editor.hide()
        editor.deleteLater()
        if commit and name and name != doc.name and doc in self.docs:
            doc.name = name
            self.bar.setTabText(self._index_of(doc), name)
            self.changed.emit()
            self.current_changed.emit(self.current())  # window title shows the tab name
        if doc in self.docs:
            doc.view.setFocus()
