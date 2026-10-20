"""Library pane (left): ONLINE models first - each a settings file in online_models/ (+ Model > Online...), then the
LOCAL ones: the models in the Hugging Face store and the user's own models elsewhere on disk (added by path with
+ Model > Local... - listed where they are, never moved or copied). Picking one shows its details in the parameter
pane; everything shown comes from the model's own files.

The arrangement is yours and purely visual: labels (separators) you name, models and labels dragged or
moved up/down. Nothing about a model is inferred from where it sits."""

import os
import subprocess

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDrag, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QMenu, QMessageBox, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
    QWidget,
)

from . import costs, model_store, online, services
from .model_store import fmt_size
from .settings import hf_store, library_order, local_models, set_library_order, set_local_models


def _key(repo):
    """How the arrangement names a model: a store repo by its id, one of the user's own by its path."""
    return ("local", os.path.normcase(repo.folder)) if repo.kind == "local" else ("repo", repo.repo_id)

STYLE = """
QWidget#LibraryPane { background: #2d2d2d; }
QWidget#PaneTitle { background: #353535; border-bottom: 1px solid #1f1f1f; }
QLabel#PaneTitleText { color: #bbb; font-weight: bold; }
QLabel#Where { color: #888; padding: 3px 10px; }
QTreeWidget { background: #2d2d2d; color: #ddd; border: none; alternate-background-color: #313131; }
QTreeWidget::item:selected { background: #4a6a8a; }
QHeaderView::section { background: #333; color: #aaa; border: none; border-bottom: 1px solid #262626;
                       padding: 2px 6px; }
QToolButton { color: #ccc; border: none; padding: 0 4px; }
QToolButton:hover { color: #fff; background: #555; }
QToolButton:disabled { color: #666; }
QMenu { background: #2d2d2d; color: #ddd; border: 1px solid #555; }
QMenu::item:selected { background: #4a6a8a; }
QMenu::item:disabled { color: #666; }
"""
LABEL, REPO, PATH = "label", "repo", "path"  # what a tree row is: data(0, UserRole) = (kind, value, fs path)


def _kind(item):
    return item.data(0, Qt.UserRole)[0] if item else None


class LibraryTree(QTreeWidget):
    """Top-level rows (models and labels) can be dragged to a new place; files inside a model can't."""
    reordered = Signal()

    def __init__(self):
        super().__init__()
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.InternalMove)

    def startDrag(self, actions):
        """Top-level rows can be rearranged here; any model, folder or file can also be dropped elsewhere
        (a Model node's File field, the graph) as its path. We run the drag ourselves so a drop elsewhere
        never removes the row from the Library."""
        item = self.currentItem()
        if item is None:
            return
        kind, value, path = item.data(0, Qt.UserRole)
        mime = self.mimeData([item])  # Qt's own row data, for rearranging within the Library
        if kind == REPO and value.snapshot:
            path = value.snapshot  # the model as a whole (the folder with model_index.json / config.json)
        if kind in (REPO, PATH) and path:
            mime.setUrls([QUrl.fromLocalFile(path)])
            mime.setText(path)
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction | Qt.MoveAction, Qt.CopyAction)

    def dragMoveEvent(self, event):
        super().dragMoveEvent(event)
        if event.source() is self:
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        item = self.currentItem()
        if event.source() is not self or not item or item.parent() is not None:
            event.ignore()
            return
        pos = event.position().toPoint()
        target = self.itemAt(pos)
        if target is None:
            index = self.topLevelItemCount()
        elif target.parent() is None:
            index = self.indexOfTopLevelItem(target) + (pos.y() > self.visualItemRect(target).center().y())
        else:  # over a model's files: place after that model
            while target.parent():
                target = target.parent()
            index = self.indexOfTopLevelItem(target) + 1
        source = self.indexOfTopLevelItem(item)
        if index > source:
            index -= 1
        # Move it ourselves and report no drop action, so Qt doesn't also remove the dragged row.
        event.setDropAction(Qt.IgnoreAction)
        event.accept()
        if index != source:
            self.move_top_level(source, index)

    def move_top_level(self, source, index):
        item = self.topLevelItem(source)
        expanded = item.isExpanded()
        self.blockSignals(True)
        self.takeTopLevelItem(source)
        self.insertTopLevelItem(index, item)
        if _kind(item) == LABEL:
            item.setFirstColumnSpanned(True)
        item.setExpanded(expanded)
        self.setCurrentItem(item)
        self.blockSignals(False)
        self.reordered.emit()


class OnlineTree(QTreeWidget):
    """The online models: drag one onto the graph to make a Model node running it (the drag carries its file)."""

    def startDrag(self, actions):
        item = self.currentItem()
        e = online_entry_of(item)
        if e is None:
            return
        mime = self.mimeData([item])
        mime.setUrls([QUrl.fromLocalFile(e["path"])])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction, Qt.CopyAction)


def online_entry_of(item):
    """The online model file a row of the Online tree stands for (None for a service row)."""
    data = item.data(0, Qt.UserRole) if item is not None else None
    return online.entry(data[1]) if isinstance(data, (list, tuple)) and data[0] == "model" else None


def service_of(item):
    data = item.data(0, Qt.UserRole) if item is not None else None
    return services.entry(data[1]) if isinstance(data, (list, tuple)) and data[0] == "service" else None


class AddServiceDialog(QDialog):
    """+ Model > Service...: which service, and the Windows user variable holding its key (the key itself is never
    stored by the app)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QLineEdit
        self.setWindowTitle("Add an online service")
        self.protocol = QComboBox()
        for p, (label, _var, _what) in services.PROTOCOLS.items():
            if p != services.CLAUDE_CODE:
                self.protocol.addItem(label, p)
        self.var = QLineEdit()
        self.note = QLabel(wordWrap=True)
        form = QFormLayout(self)
        form.addRow("Service", self.protocol)
        form.addRow("Key variable", self.var)
        form.addRow(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        self.protocol.currentIndexChanged.connect(self._fill)
        self.var.textChanged.connect(self._note)
        self._fill()

    def _fill(self):
        self.var.setText(services.PROTOCOLS[self.protocol.currentData()][1])
        self._note()

    def _note(self):
        var = self.var.text().strip()
        has = bool(services.key({"key_env": var}))
        self.note.setText(services.PROTOCOLS[self.protocol.currentData()][2] + "\n\n" + (
            f"{var} is set - the app will check it." if has else
            f"{var or 'The variable'} isn't set yet: make a key on the service's site, then in a terminal: "
            f"setx {var or 'NAME'} <your key> - and refresh the Library."))


class BrowseDialog(QDialog):
    """Browse a service's models (search, or its collections) and add one as an online model file. Everything is
    asked in the background; picking a model shows its description, its inputs and its price."""

    def __init__(self, service, parent=None):
        super().__init__(parent)
        from PySide6.QtWidgets import QLineEdit, QPushButton, QTextBrowser
        self.service = service
        self.mod, self.token = services.protocol_module(service["protocol"]), services.key(service)
        self.setWindowTitle(f"{service['label']} - models")
        self.resize(820, 640)
        self.query = QLineEdit(placeholderText="Search, e.g. video edit, image to 3d, lip sync…")
        go = QPushButton("Search")
        self.collection = QComboBox()
        self.collection.addItem("Collections…", "")
        self.results = QTreeWidget()
        self.results.setHeaderLabels(["Model", "Runs"])
        self.results.setRootIsDecorated(False)
        self.results.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.results.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.details = QTextBrowser(openExternalLinks=True)
        self.status = QLabel()
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.add_button = buttons.addButton("Add to my online models", QDialogButtonBox.AcceptRole)
        self.add_button.setEnabled(False)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        top = QHBoxLayout()
        top.addWidget(self.query, 1)
        top.addWidget(go)
        top.addWidget(self.collection)
        v = QVBoxLayout(self)
        v.addLayout(top)
        v.addWidget(self.results, 3)
        v.addWidget(self.details, 2)
        v.addWidget(self.status)
        v.addWidget(buttons)
        go.clicked.connect(self._search)
        self.query.returnPressed.connect(self._search)
        self.collection.activated.connect(self._collection)
        self.results.currentItemChanged.connect(self._picked)
        self.chosen = None
        self.found = {}  # model id -> details (inputs, price) asked so far
        services.in_background(self.mod.collections, self.token, done=self._got_collections)

    def _got_collections(self, rows, err):
        if err:
            self.status.setText(f"collections: {err}")
            return
        for slug, name in rows or []:
            self.collection.addItem(name, slug)

    def _search(self):
        q = self.query.text().strip()
        if q:
            self.status.setText(f"searching “{q}”…")
            services.in_background(self.mod.search, self.token, q, done=self._list)

    def _collection(self, _i):
        slug = self.collection.currentData()
        if slug:
            self.status.setText(f"collection {self.collection.currentText()}…")
            services.in_background(self.mod.collection, self.token, slug, done=self._list)

    def _list(self, rows, err):
        self.results.clear()
        if err:
            self.status.setText(str(err))
            return
        for r in rows or []:
            item = QTreeWidgetItem([r["id"], f"{r['runs']:,}"])
            item.setData(0, Qt.UserRole, r)
            item.setToolTip(0, r["description"])
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            self.results.addTopLevelItem(item)
        self.status.setText(f"{len(rows or [])} models")

    def _picked(self, item, _prev=None):
        import html
        r = item.data(0, Qt.UserRole) if item is not None else None
        self.chosen = r
        self.add_button.setEnabled(r is not None)
        if r is None:
            return
        if r["id"] in self.found:
            return self._show(r["id"])
        self.details.setHtml(f"<b>{html.escape(r['id'])}</b><br>{html.escape(r['description'])}<br><br>"
                             "<i>asking for its inputs and price…</i>")

        def ask(mid=r["id"]):
            d = self.mod.model(self.token, mid)
            try:
                d["price"] = self.mod.price(mid)
            except Exception as e:  # the model is still usable without its price
                d["price"], d["price_error"] = None, str(e)
            return d
        services.in_background(ask, done=lambda d, err, mid=r["id"]: self._got(mid, d, err))

    def _got(self, mid, d, err):
        if err:
            if self.chosen and self.chosen["id"] == mid:
                self.details.setHtml(f"<b>{mid}</b><br>can't read it: {err}")
            return
        self.found[mid] = d
        if self.chosen and self.chosen["id"] == mid:
            self._show(mid)

    def _show(self, mid):
        import html
        d = self.found[mid]
        rows = [f"<b>{html.escape(mid)}</b> · <a href='{d['url']}'>page</a>"
                + (" · official model (priced per output)" if d.get("official") else ""),
                html.escape(d.get("description") or "")]
        price = costs.table_rows(d.get("price"))
        rows.append("<b>Price</b><br>" + ("<br>".join(f"{html.escape(m)} - {html.escape(c)}" for c, m in price)
                                          if price else "not shown by the service (billed by GPU time)"))
        ins = []
        for f in d.get("fields") or []:
            kind = f"{f['media']} file{'s' if f['multi'] else ''}" if f["type"] == "uri" else f["type"]
            extra = f" = {f['default']}" if f["default"] not in (None, "") else ""
            opts = f" [{', '.join(map(str, f['options']))}]" if f["options"] else ""
            name = f"<b>{f['name']}</b>" if f["required"] else f["name"]
            ins.append(f"{name} · {html.escape(kind + extra + opts)}")
        rows.append("<b>Inputs</b> (bold = required)<br>" + "<br>".join(ins))
        self.details.setHtml("<br><br>".join(rows))


class OnlineModelDialog(QDialog):
    """+ Model > Online...: a provider and one of its models - saved as a settings file in online_models/, named as
    the provider names the model (one file per model: the same model again is the one already there)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add an online model")
        online.status()  # asks the provider for its model list (in the background) if not asked yet
        self.provider = QComboBox()
        self.provider.addItem(services.PROTOCOLS[services.CLAUDE_CODE][0], services.CLAUDE_CODE)
        self.model = QComboBox(editable=True)
        self.model.setMinimumContentsLength(28)
        self.note = QLabel(wordWrap=True)
        form = QFormLayout(self)
        form.addRow("Provider", self.provider)
        form.addRow("Model", self.model)
        form.addRow(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        from PySide6.QtCore import QTimer
        self._timer = QTimer(self, interval=500)  # the model list may still be on its way
        self._timer.timeout.connect(self._fill)
        self._fill()
        self._timer.start()

    def _fill(self):
        options = list(online.MODEL_OPTIONS)
        if [self.model.itemData(i) for i in range(self.model.count())] != [v for v, _ in options]:
            self.model.blockSignals(True)
            self.model.clear()
            for value, label in options:
                self.model.addItem(label, value)
            self.model.blockSignals(False)
            self.model.setCurrentIndex(min(1, self.model.count() - 1))
        state = online._models
        if state["state"] in ("done", "failed"):
            self._timer.stop()
        listed = (f"{len(options) - 1} from Anthropic's list for your account" if state["state"] == "done"
                  else state["error"] or "asking for the list…")
        self.note.setText(f"Models: {listed}\nSaved as a file in {online.ONLINE_DIR} - edit it to change its "
                          "settings.")

    def result_entry(self):
        """(provider, model id, label): a picked model with the provider's own name for it; a typed id as it is."""
        index = self.model.currentIndex()
        typed = self.model.currentText().strip()
        model = self.model.itemData(index) if index >= 0 and self.model.itemText(index) == typed else typed
        return self.provider.currentData(), model or "", typed


class LibraryPane(QWidget):
    model_selected = Signal(object)   # a model_store.Repo, or None
    online_selected = Signal(object)  # an online model (online.entry dict)
    online_removed = Signal(str)      # an online model's name: Model nodes using it forget it

    def __init__(self, run_command):
        """run_command(text): runs a shell command where the user can see it (the Console)."""
        super().__init__(objectName="LibraryPane")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet(STYLE)
        self.run_command = run_command
        self.repos = []

        title = QWidget(objectName="PaneTitle")
        t = QHBoxLayout(title)
        t.setContentsMargins(10, 5, 6, 5)
        t.setSpacing(2)
        t.addWidget(QLabel("Library", objectName="PaneTitleText"), 1)
        self.btn_delete = QToolButton(text="Delete", toolTip="Delete the selected model from the store, "
                                                             "or remove the selected label")
        add = QToolButton(text="+ Model", toolTip="Add a service, an online model, or a model of your own on this "
                                                  "computer")
        add.setPopupMode(QToolButton.InstantPopup)
        add_menu = QMenu(add)
        add_menu.addAction("Service (Replicate, Google…)…", self.add_service)
        add_menu.addAction("Online model…", self.add_online)
        add_menu.addAction("Local model folder…", self.add_local_folder)
        add_menu.addAction("Local model file…", self.add_local_file)
        add.setMenu(add_menu)
        t.addWidget(add)
        for b, slot in ((QToolButton(text="+ Label", toolTip="Add a label (separator) above the selection"),
                         self.add_label),
                        (QToolButton(text="⟳", toolTip="Refresh"), self.refresh),
                        (self.btn_delete, self._delete)):
            b.clicked.connect(slot)
            t.addWidget(b)
        self.where = QLabel(objectName="Where")
        self.where.setWordWrap(True)

        self.online_tree = OnlineTree()
        self.online_tree.setHeaderLabels(["Online", ""])
        self.online_tree.setRootIsDecorated(True)
        self.online_tree.setIndentation(12)
        self.online_tree.setUniformRowHeights(True)
        self.online_tree.setDragEnabled(True)
        self.online_tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        oh = self.online_tree.header()
        oh.setStretchLastSection(False)
        oh.setSectionResizeMode(0, QHeaderView.Stretch)
        oh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.online_tree.setToolTip("Services (services/) and their online models (online_models/, one file each). "
                                    "Drag a model onto the graph for a Model node running it. Right-click for more.")
        services.on_status(lambda _name, _st: self.refresh_online())
        self.online_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.online_tree.customContextMenuRequested.connect(self._online_menu)
        self.online_tree.itemClicked.connect(self._online_clicked)
        self.online_tree.currentItemChanged.connect(
            lambda item, _p: QApplication.mouseButtons() == Qt.NoButton and self._online_clicked(item))

        self.tree = LibraryTree()
        self.tree.setHeaderLabels(["Local", "Size"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # long names are elided instead
        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)  # the name takes the pane's width
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.setToolTip("Drag models and labels to arrange them. Drag a model, folder or file onto a "
                             "Model node's File field or onto the graph. Double-click a file to show it in "
                             "Explorer, a label to rename it. Right-click for more.")
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.currentItemChanged.connect(self._current_changed)
        self.tree.itemClicked.connect(self._clicked)  # on release
        self.tree.itemDoubleClicked.connect(self._double_clicked)
        self.tree.reordered.connect(self._save_order)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(title)
        v.addWidget(self.online_tree)
        v.addWidget(self.where)
        v.addWidget(self.tree, 1)
        self.setMinimumWidth(0)
        self.refresh()

    def sizeHint(self):
        return QSize(280, 600)

    # ---- listing ---------------------------------------------------------
    def refresh(self):
        self.refresh_online()
        store = hf_store()
        current = self._top(self.tree.currentItem())
        selected = current.data(0, Qt.UserRole)[:2] if current else None
        expanded = {self.tree.topLevelItem(i).text(0) for i in range(self.tree.topLevelItemCount())
                    if self.tree.topLevelItem(i).isExpanded()}
        stored = model_store.scan(store)
        own = [model_store.read_local(p) for p in local_models()]
        self.repos = stored + own
        self.where.setText((f"{model_store.hub_dir(store)} · {len(stored)} models, "
                            f"{fmt_size(model_store.physical_size(stored))}" if stored else
                            f"{model_store.hub_dir(store)} · empty. Download with the Console: "
                            f"hf download <org>/<name>") + (f" · {len(own)} of your own" if own else ""))
        by_key = {_key(r): r for r in self.repos}
        rows, placed = [], set()
        for entry in library_order():  # your arrangement first; models it doesn't mention go at the end
            if entry.get("label") is not None:
                rows.append((LABEL, entry["label"]))
                continue
            key = ("local", os.path.normcase(entry["local"])) if entry.get("local") else ("repo", entry.get("repo"))
            if key in by_key and key not in placed:
                rows.append((REPO, by_key[key]))
                placed.add(key)
        rows += [(REPO, r) for r in self.repos if _key(r) not in placed]

        self.tree.blockSignals(True)
        self.tree.clear()
        new_current = None
        for kind, value in rows:
            item = self._label_item(value) if kind == LABEL else self._repo_item(value)
            self.tree.addTopLevelItem(item)
            if kind == LABEL:
                item.setFirstColumnSpanned(True)
            else:
                item.setExpanded(value.repo_id in expanded)
            if selected and selected[0] == kind and (value == selected[1] if kind == LABEL
                                                      else _key(value) == _key(selected[1])):
                new_current = item
        self.tree.setCurrentItem(new_current)
        self.tree.blockSignals(False)
        if _kind(new_current) == REPO:  # details may have changed (e.g. a download finished)
            self.model_selected.emit(new_current.data(0, Qt.UserRole)[1])

    def _label_item(self, text):
        item = QTreeWidgetItem([text, ""])
        item.setData(0, Qt.UserRole, (LABEL, text, None))
        font = QFont(item.font(0))
        font.setBold(True)
        item.setFont(0, font)
        item.setForeground(0, QBrush(QColor("#9fb8d0")))
        for col in (0, 1):
            item.setBackground(col, QBrush(QColor("#383838")))
        item.setFlags((item.flags() | Qt.ItemIsDragEnabled) & ~Qt.ItemIsDropEnabled)
        return item

    def _repo_item(self, repo):
        local = repo.kind == "local"
        item = QTreeWidgetItem([repo.repo_id, fmt_size(repo.total)])
        item.setData(0, Qt.UserRole, (REPO, repo, repo.folder))
        cls = repo.class_label
        item.setToolTip(0, (f"{repo.folder}\n(your own - listed where it is)" if local else repo.repo_id)
                        + f"\nTask: {repo.task or 'not declared'}\nClass: {cls[0] if cls else 'not recorded'}")
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        item.setFlags((item.flags() | Qt.ItemIsDragEnabled) & ~Qt.ItemIsDropEnabled)
        if local:
            item.setForeground(0, QBrush(QColor("#c8d8a8")))  # your own: a quiet green
        if repo.incomplete or any(c and not present for _, c, present in repo.components()):
            item.setForeground(0, Qt.yellow)
        if local and not repo.snapshot:
            item.setText(1, "missing")
            item.setForeground(0, QBrush(QColor("#d08070")))
        if local and os.path.isfile(repo.folder):
            return item  # a single file: nothing inside to list
        folders = {}
        for f in repo.files:
            parent = item
            if "/" in f.path:
                top = f.path.split("/")[0]
                if top not in folders:
                    folders[top] = self._file_item(top + "/", "", repo, os.path.join(repo.snapshot, top))
                    folders[top]._size = 0
                    item.addChild(folders[top])
                parent = folders[top]
                parent._size += f.size
            child = self._file_item(f.path.split("/", 1)[-1] if parent is not item else f.path, fmt_size(f.size),
                                    repo, os.path.join(repo.snapshot, *f.path.split("/")))
            if f.shared:
                child.setToolTip(0, "Shared with another model (stored once)")
            parent.addChild(child)
        for folder in folders.values():
            folder.setText(1, fmt_size(folder._size))
        return item

    @staticmethod
    def _file_item(name, size, repo, path):
        item = QTreeWidgetItem([name, size])
        item.setData(0, Qt.UserRole, (PATH, repo, path))
        item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
        item.setFlags((item.flags() | Qt.ItemIsDragEnabled) & ~Qt.ItemIsDropEnabled)  # drag out as a path
        return item

    @staticmethod
    def _top(item):
        while item is not None and item.parent() is not None:
            item = item.parent()
        return item

    def _current_changed(self, item, _previous=None):
        """Keyboard moves show details at once; a mouse press waits for the release (_clicked), so pressing
        and dragging a model to a field doesn't swap the parameter pane away from that field."""
        if QApplication.mouseButtons() == Qt.NoButton:
            self._emit_for(item)

    def _clicked(self, item, _column=0):
        self._emit_for(item)
        self.online_tree.blockSignals(True)
        self.online_tree.setCurrentItem(None)
        self.online_tree.blockSignals(False)

    def _emit_for(self, item):
        kind = _kind(item)
        self.model_selected.emit(item.data(0, Qt.UserRole)[1] if kind in (REPO, PATH) else None)

    def clear_selection(self):
        """Another pane took over the parameter pane: picking the same model again should show it again."""
        for tree in (self.tree, self.online_tree):
            tree.blockSignals(True)
            tree.setCurrentItem(None)
            tree.clearSelection()
            tree.blockSignals(False)

    # ---- online models (a settings file each) --------------------------------
    def refresh_online(self):
        """Services at the top (with their account / problem), each with its online models under it."""
        current = self.online_tree.currentItem()
        chosen = tuple(current.data(0, Qt.UserRole) or ()) if current else None
        found = online.entries()
        self.online_tree.blockSignals(True)
        self.online_tree.clear()
        grey = QBrush(QColor("#777"))
        for s in services.entries():
            st = services.status(s["name"])
            state = ("checking…" if st.get("state") == "checking" else st.get("account") or "ready"
                     if st.get("state") == "ok" else "problem")
            top = QTreeWidgetItem([s["label"], state])
            top.setData(0, Qt.UserRole, ("service", s["name"]))
            font = QFont(top.font(0))
            font.setBold(True)
            top.setFont(0, font)
            top.setToolTip(0, (s["path"] or "built in") + (f"\n{st.get('error')}" if st.get("error") else ""))
            if st.get("state") == "failed":
                top.setForeground(1, QBrush(QColor("#d08070")))
            self.online_tree.addTopLevelItem(top)
            mine = [e for e in found if e["provider"] == s["name"]]
            for e in mine:
                d = online.info(e)
                rows = costs.table_rows((d or {}).get("price"))
                price = rows[0][1].replace(" per ", "/").replace("second of output video", "s") if rows else ""
                item = QTreeWidgetItem([e["label"], price + (" …" if len(rows) > 1 else "")])
                item.setData(0, Qt.UserRole, ("model", e["name"]))
                item.setToolTip(0, f"{e['path']}\nModel id: {e['model'] or 'the provider default'}"
                                + (f"\n{e['error']}" if e["error"] else ""))
                if e["error"]:
                    item.setForeground(0, QBrush(QColor("#d08070")))
                top.addChild(item)
                if chosen == ("model", e["name"]):
                    self.online_tree.setCurrentItem(item)
            if not mine:
                hint = QTreeWidgetItem(["no models yet - right-click › Browse…" if hasattr(
                    services.protocol_module(s["protocol"]), "search") else "none yet - + Model › Online…", ""])
                hint.setForeground(0, grey)
                hint.setFlags(Qt.NoItemFlags)
                top.addChild(hint)
            top.setExpanded(True)
            if chosen == ("service", s["name"]):
                self.online_tree.setCurrentItem(top)
        for e in found:  # files of a service that isn't set up (any more)
            if services.entry(e["provider"]) is None:
                item = QTreeWidgetItem([e["label"], e["provider"]])
                item.setData(0, Qt.UserRole, ("model", e["name"]))
                item.setForeground(0, QBrush(QColor("#d08070")))
                item.setToolTip(0, e["error"])
                self.online_tree.addTopLevelItem(item)
        self.online_tree.blockSignals(False)
        count = sum(1 + self.online_tree.topLevelItem(i).childCount()
                    for i in range(self.online_tree.topLevelItemCount()))
        rows = min(max(1, count), 10)
        row_h = max(self.online_tree.sizeHintForRow(0), self.fontMetrics().height() + 4)
        self.online_tree.setFixedHeight(self.online_tree.header().sizeHint().height() + rows * row_h + 4)

    def _online_clicked(self, item, _column=0):
        e = online_entry_of(item)
        s = service_of(item)
        if e is None and s is None:
            return
        self.tree.blockSignals(True)
        self.tree.setCurrentItem(None)
        self.tree.blockSignals(False)
        if e is not None:
            self.online_selected.emit(e)
        else:
            self.online_selected.emit({"service": s})

    def add_service(self):
        dialog = AddServiceDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        name = services.add(dialog.protocol.currentData(), key_env=dialog.var.text().strip() or None)
        services.status(name, refresh=True)
        self.refresh_online()

    def add_online(self):
        """+ Model > Online...: a service with a catalogue is browsed; Claude Code picks from Anthropic's list."""
        item = self.online_tree.currentItem()
        s = service_of(item) or (services.entry(online_entry_of(item)["provider"]) if online_entry_of(item) else None)
        browsable = [x for x in services.entries() if hasattr(services.protocol_module(x["protocol"]), "search")]
        if s is None or s["protocol"] == services.CLAUDE_CODE:
            if browsable and QApplication.keyboardModifiers() != Qt.ShiftModifier and s is None:
                menu = QMenu(self)
                menu.addAction(services.PROTOCOLS[services.CLAUDE_CODE][0] + "…", self._add_claude)
                for x in browsable:
                    menu.addAction(f"Browse {x['label']}…", lambda x=x: self.browse(x))
                menu.exec(self.cursor().pos())
                return
            return self._add_claude()
        if hasattr(services.protocol_module(s["protocol"]), "search"):
            return self.browse(s)
        QMessageBox.information(self, "Online model", f"{s['label']}: its models are added in the next step "
                                                      "(not browsable yet).")

    def _add_claude(self):
        dialog = OnlineModelDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        provider, model, label = dialog.result_entry()
        self._added(provider, model, label)

    def browse(self, s):
        if services.status(s["name"]).get("state") == "failed":
            QMessageBox.warning(self, s["label"], services.status(s["name"]).get("error", ""))
            return
        dialog = BrowseDialog(s, self)
        if dialog.exec() == QDialog.Accepted and dialog.chosen:
            self._added(s["name"], dialog.chosen["id"], dialog.chosen["id"].split("/")[-1])

    def _added(self, provider, model, label):
        same = next((e for e in online.entries() if e["provider"] == provider and e["model"] == model), None)
        name = same["name"] if same else online.add_entry(provider, model, label)  # one file per model
        e = online.entry(name)
        if e is not None and services.entry(provider) and services.entry(provider)["protocol"] != services.CLAUDE_CODE:
            online.refresh_info(e, done=lambda *_: self.refresh_online())
        self.refresh_online()
        self._select_online(("model", name))

    def _select_online(self, key):
        it = self.online_tree.invisibleRootItem()
        stack = [it.child(i) for i in range(it.childCount())]
        while stack:
            item = stack.pop()
            if tuple(item.data(0, Qt.UserRole) or ()) == key:
                self.online_tree.setCurrentItem(item)
                self._online_clicked(item)
                return
            stack += [item.child(i) for i in range(item.childCount())]

    def _online_menu(self, pos):
        item = self.online_tree.itemAt(pos)
        e, s = online_entry_of(item), service_of(item)
        menu = QMenu(self)
        if s is not None:
            if hasattr(services.protocol_module(s["protocol"]), "search"):
                menu.addAction(f"Browse {s['label']} models…", lambda: self.browse(s))
            elif s["protocol"] == services.CLAUDE_CODE:
                menu.addAction("Add a Claude model…", self._add_claude)
            if s["path"]:
                menu.addAction("Check the key again", lambda: (services.status(s["name"], refresh=True),
                                                               self.refresh_online()))
                menu.addSeparator()
                menu.addAction("Edit its settings file", lambda: subprocess.Popen(["notepad", s["path"]]))
                menu.addAction("Show in Explorer", lambda: subprocess.Popen(["explorer", "/select,", s["path"]]))
                menu.addSeparator()
                menu.addAction("Remove the service…", lambda: self._remove_service(s))
        elif e is not None:
            if services.entry(e["provider"]) and services.entry(e["provider"])["protocol"] != services.CLAUDE_CODE:
                menu.addAction("Ask its inputs and price again", lambda: online.refresh_info(
                    e, done=lambda *_: (self.refresh_online(), self._online_clicked(self.online_tree.currentItem()))))
                d = online.info(e)
                if d and d.get("url"):
                    menu.addAction("Open its page", lambda: __import__("webbrowser").open(d["url"]))
                menu.addSeparator()
            menu.addAction("Edit its settings file", lambda: subprocess.Popen(["notepad", e["path"]]))
            menu.addAction("Show in Explorer", lambda: subprocess.Popen(["explorer", "/select,", e["path"]]))
            menu.addSeparator()
            menu.addAction("Remove…", lambda: self._delete_online(e))
        else:
            menu.addAction("Add a service…", self.add_service)
        menu.exec(self.online_tree.viewport().mapToGlobal(pos))

    def _remove_service(self, s):
        mine = [e for e in online.entries() if e["provider"] == s["name"]]
        if QMessageBox.question(self, "Remove service",
                                f"Remove {s['label']}?\n\nIts file goes ({s['path']}). Its {len(mine)} online model "
                                f"file(s) stay and work again when the service is added back. Your key (in "
                                f"{s['key_env']}) is not touched.",
                                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Yes:
            services.remove(s["name"])
            self.refresh_online()

    def _delete_online(self, e):
        """Gone as if it never was: its file, its row, and the Model nodes' choice of it (they take another)."""
        if QMessageBox.question(self, "Remove online model", f"Remove {e['label']} from the online models?",
                                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Yes:
            os.remove(e["path"])
            online.forget_cache()
            cache = os.path.join(online.CACHE_DIR, e["name"] + ".json")
            if os.path.exists(cache):
                os.remove(cache)
            self.refresh_online()
            self.online_removed.emit(e["name"])

    # ---- arrangement (visual only) --------------------------------------
    def _save_order(self):
        entries = []
        for i in range(self.tree.topLevelItemCount()):
            kind, value, _ = self.tree.topLevelItem(i).data(0, Qt.UserRole)
            entries.append({"label": value} if kind == LABEL else
                           {"local": value.folder} if value.kind == "local" else {"repo": value.repo_id})
        set_library_order(entries)

    # ---- your own models (by path) ---------------------------------------
    def add_local(self, path):
        path = os.path.normpath(path)
        paths = local_models()
        if path not in paths:
            set_local_models(paths + [path])
            top = self._top(self.tree.currentItem())
            self.refresh()
            # place it after the selection (or at the end), then keep the arrangement
            new = next((self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())
                        if _kind(self.tree.topLevelItem(i)) == REPO
                        and _key(self.tree.topLevelItem(i).data(0, Qt.UserRole)[1]) == ("local", os.path.normcase(path))),
                       None)
            if new is not None and top is not None:
                self.tree.move_top_level(self.tree.indexOfTopLevelItem(new), self.tree.indexOfTopLevelItem(top) + 1)
            self._save_order()

    def add_local_folder(self):
        from PySide6.QtWidgets import QFileDialog
        path = QFileDialog.getExistingDirectory(self, "Add a model folder (listed where it is)")
        if path:
            self.add_local(path)

    def add_local_file(self):
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(self, "Add a model file (listed where it is)", "",
                                              "Model files (*.safetensors *.gguf *.onnx *.pth *.pt *.ckpt *.bin);;"
                                              "All files (*)")
        if path:
            self.add_local(path)

    def remove_local(self, repo):
        set_local_models([p for p in local_models() if os.path.normcase(p) != os.path.normcase(repo.folder)])
        self.refresh()
        self._save_order()

    def add_label(self):
        text, ok = QInputDialog.getText(self, "Add label", "Label:")
        if not ok or not text.strip():
            return
        top = self._top(self.tree.currentItem())
        index = self.tree.indexOfTopLevelItem(top) if top else 0
        item = self._label_item(text.strip())
        self.tree.insertTopLevelItem(index, item)
        item.setFirstColumnSpanned(True)
        self.tree.setCurrentItem(item)
        self._save_order()

    def rename_label(self, item):
        text, ok = QInputDialog.getText(self, "Rename label", "Label:", text=item.text(0))
        if ok and text.strip():
            item.setText(0, text.strip())
            item.setData(0, Qt.UserRole, (LABEL, text.strip(), None))
            self._save_order()

    def remove_label(self, item):
        self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        self._save_order()

    def move(self, step):
        top = self._top(self.tree.currentItem())
        if top is None:
            return
        source = self.tree.indexOfTopLevelItem(top)
        index = source + step
        if 0 <= index < self.tree.topLevelItemCount():
            self.tree.move_top_level(source, index)

    # ---- actions ---------------------------------------------------------
    def _double_clicked(self, item, _column=0):
        kind, _, path = item.data(0, Qt.UserRole)
        if kind == LABEL:
            self.rename_label(item)
        elif kind == PATH and os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item:
            self.tree.setCurrentItem(item)
        kind = _kind(item)
        top = self._top(item)
        menu = QMenu(self)
        menu.addAction("Add a model folder of your own…", self.add_local_folder)
        menu.addAction("Add a model file of your own…", self.add_local_file)
        menu.addSeparator()
        menu.addAction("Add label above…", self.add_label)
        if kind == LABEL:
            menu.addAction("Rename label…", lambda: self.rename_label(item))
            menu.addAction("Remove label", lambda: self.remove_label(item))
        menu.addSeparator()
        up = menu.addAction("Move up", lambda: self.move(-1))
        down = menu.addAction("Move down", lambda: self.move(+1))
        index = self.tree.indexOfTopLevelItem(top) if top else -1
        up.setEnabled(index > 0)
        down.setEnabled(0 <= index < self.tree.topLevelItemCount() - 1)
        menu.addSeparator()
        if kind in (REPO, PATH):
            path = item.data(0, Qt.UserRole)[2]
            menu.addAction("Show in Explorer", lambda: subprocess.Popen(
                ["explorer", "/select,", os.path.normpath(path)]))
        menu.addAction("Open the store folder", self._open_store)
        if kind in (REPO, PATH):
            menu.addSeparator()
            own = top.data(0, Qt.UserRole)[1].kind == "local"
            menu.addAction("Remove from the Library (files stay)" if own else "Delete model…", self._delete)
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _open_store(self):
        path = model_store.hub_dir(hf_store())
        os.makedirs(path, exist_ok=True)
        os.startfile(path)

    def _delete(self):
        online_item = self.online_tree.currentItem()
        e, sv = online_entry_of(online_item), service_of(online_item)
        if e is not None:  # the Delete button with an online model chosen
            self._delete_online(e)
            return
        if sv is not None and sv["path"]:
            self._remove_service(sv)
            return
        item = self._top(self.tree.currentItem())
        if not item:
            QMessageBox.information(self, "Delete", "Select a model or a label first.")
            return
        if _kind(item) == LABEL:
            self.remove_label(item)
            return
        repo = item.data(0, Qt.UserRole)[1]
        if repo.kind == "local":  # your own model: only taken off the list - its files are never touched
            if QMessageBox.question(self, "Remove from the Library",
                                    f"Remove {repo.repo_id} from the Library?\n\n{repo.folder}\n\nThe files stay where "
                                    f"they are.", QMessageBox.Yes | QMessageBox.Cancel,
                                    QMessageBox.Cancel) == QMessageBox.Yes:
                self.remove_local(repo)
            return
        kept = repo.total - repo.exclusive
        command = model_store.delete_command(repo)
        text = (f"Delete {repo.repo_id} from the store?\n\n"
                f"Frees {fmt_size(repo.exclusive)}."
                + (f"\n{fmt_size(kept)} stays - those files are shared with other models." if kept else "")
                + f"\n\nRuns in the Console:\n{command}")
        if QMessageBox.question(self, "Delete model", text, QMessageBox.Yes | QMessageBox.Cancel,
                                QMessageBox.Cancel) != QMessageBox.Yes:
            return
        self.run_command(command)  # the Library refreshes when the Console is idle again


def describe(repo):
    """[(title, [(label, value)])] for the parameter pane - only what the repo's files say."""
    cls = repo.class_label
    model = [("Yours", repo.folder) if repo.kind == "local" else
             ("Repo", ("link", repo.repo_id, f"https://huggingface.co/{repo.repo_id}")),
             ("Task", f"{repo.task} (model card)" if repo.task else "not declared on the model card"),
             ("Class", f"{cls[0]} ({cls[1]})" if cls else "not recorded in its files")]
    for key, label in (("library_name", "Library (card)"), ("license", "License")):
        if repo.card.get(key):
            model.append((label, repo.card[key]))
    if repo.index.get("_diffusers_version"):
        model.append(("Made with", "diffusers " + repo.index["_diffusers_version"]))
    sections = [("Model", model)]

    parts = [(name, cls_ + ("" if present else "  — not downloaded"))
             for name, cls_, present in repo.components() if cls_]
    if parts:
        sections.append(("Parts (model_index.json)", parts))
    needs = repo.requirements()
    sections.append(("Needs", needs if needs else
                     [("", "nothing else recorded in its files" if parts or repo.adapter
                       else "not recorded in its files")]))

    if repo.kind == "local":  # your own model, outside the store
        if not repo.snapshot:
            return sections + [("On disk", [("Missing", f"{repo.folder} isn't there any more")])]
        ext = os.path.splitext(repo.folder)[1].lower()
        disk = [("Size", fmt_size(repo.total)), ("Path for a Model node", repo.snapshot),
                ("Kept", "where it is - listed by path; removing it from the Library leaves the files")]
        if ext == ".onnx":
            disk.append(("Format", "ONNX - the engine has no ONNX runner yet (it can't render with it)"))
        sections.append(("On disk", disk))
        return sections
    disk = [("Size", fmt_size(repo.total)), ("Frees on delete", fmt_size(repo.exclusive))]
    if repo.total != repo.exclusive:
        disk.append(("Shared", fmt_size(repo.total - repo.exclusive) + " (used by other models too)"))
    if repo.incomplete:
        disk.append(("Unfinished", fmt_size(repo.incomplete) + " downloaded so far"))
    disk.append(("Revision", repo.revision[:12] + (f" (+{repo.snapshots - 1} older)" if repo.snapshots > 1 else "")))
    if repo.snapshot:
        disk.append(("Path for a Model node", repo.snapshot))
    disk.append(("Folder", repo.folder))
    sections.append(("On disk", disk))
    return sections


def describe_online(e):
    """[(title, [(label, value)])] for the parameter pane: an online model (as its file and its service say) or a
    service ({"service": entry})."""
    if "service" in e:
        sv = e["service"]
        st = services.status(sv["name"])
        rows = [("Service", sv["label"]), ("Gives", services.PROTOCOLS.get(sv["protocol"], ("", "", ""))[2]),
                ("Settings file", sv["path"] or "built in")]
        if sv["key_env"]:
            rows.append(("Key", f"Windows user variable {sv['key_env']}"
                                + (" (set)" if services.key(sv) else " - not set")))
        rows.append(("Status", {"ok": "ready" + (f" - account {st.get('account')}" if st.get("account") else ""),
                                "checking": "checking the key…"}.get(st.get("state"), st.get("error", ""))))
        if sv["settings"]:
            rows += [(k, str(v)) for k, v in sv["settings"].items()]
        spent = costs.spent(service=sv["name"])
        if spent:
            rows.append(("Spent so far", f"${spent:.2f} (logs/online_spend.jsonl)"))
        mine = [m["label"] for m in online.entries() if m["provider"] == sv["name"]]
        return [("Service", rows), ("Its online models", [("", m) for m in mine] or [("", "none yet")])]
    known = online.SETTINGS_HELP.get(e["provider"], {})
    settings = [(k, str(v)) for k, v in e["settings"].items()] or [("", "none set - the provider's defaults")]
    sections = [("Online model", [("Service", online.providers().get(e["provider"], e["provider"])),
                                  ("Model id", e["model"] or "the provider's own default"),
                                  ("Settings file", e["path"])] + ([("Problem", e["error"])] if e["error"] else [])),
                ("Settings", settings)]
    d = online.info(e)
    if d is not None:
        sections[0][1].insert(1, ("Page", ("link", d.get("url", ""), d.get("url", ""))))
        if d.get("description"):
            sections[0][1].insert(1, ("About", d["description"]))
        ins = []
        for f in d.get("fields") or []:
            kind = f"{f['media']} file{'s' if f['multi'] else ''}" if f["type"] == "uri" else f["type"]
            extra = f" = {f['default']}" if f["default"] not in (None, "") else ""
            opts = f" [{', '.join(map(str, f['options']))}]" if f["options"] else ""
            ins.append((f["name"] + (" *" if f["required"] else ""), kind + extra + opts))
        sections.append(("Inputs (* required)", ins))
        price = costs.table_rows(d.get("price"))
        sections.append(("Price (from its page, " + d.get("fetched", "") + ")",
                         [(m, c) for c, m in price] or [("", "not shown - billed by GPU time")]))
    if known:
        sections.append(("Settings it reads", [(k, v) for k, v in known.items()]))
    return sections
