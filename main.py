import os
import sys

from PySide6.QtCore import QPointF, QProcess, Qt, QTimer
from PySide6.QtGui import QAction, QCursor, QKeySequence, QUndoGroup
from PySide6.QtWidgets import QApplication, QFileDialog, QMainWindow, QMessageBox, QProxyStyle, QStyle
from shiboken6 import isValid

from node_editor import clipboard, document
from node_editor.console import ConsolePane
from node_editor.graph_tabs import GraphDoc, GraphTabs
from node_editor.library import LibraryPane, describe, describe_online
from node_editor.logo import APP_NAME
from node_editor.pane_splitter import PaneSplitter
from node_editor.param_panel import ParamPane

EXT = ".tau"  # a graph file (JSON inside). Only .tau is opened: any other JSON could be shaped differently and
FILE_FILTER = "tauUI graphs (*.tau)"  # load into something unpredictable


def _not_a_graph(path):
    """Why a file isn't opened as a graph ('' = it is a .tau file)."""
    if os.path.splitext(path)[1].lower() != EXT:
        return f"{os.path.basename(path)} isn't a {APP_NAME} graph: only {EXT} files are opened."
    return ""
GRAPHS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "graphs")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1400, 850)
        self.path = None     # file the tabs were last saved to / opened from
        self.tabs_dirty = False  # tabs added / closed / renamed / moved since the last save (not in any undo stack)
        self.undo_group = QUndoGroup(self)  # follows the current tab's undo stack
        self.tabs = GraphTabs()  # one graph per tab; all tabs are saved together in one file
        self.params = ParamPane(None)
        self.console = ConsolePane()
        self.library = LibraryPane(self.console.run)

        # Library | (graph tabs over console) | Parameters - the side panes run the full height.
        middle = PaneSplitter(self.tabs, self.console, right_width=200, orientation=Qt.Vertical)
        self.right_split = PaneSplitter(middle, self.params, right_width=340)
        self.left_split = PaneSplitter(self.library, self.right_split, left_width=280)
        self.setCentralWidget(self.left_split)

        self.console.idle.connect(self.library.refresh)  # a download or delete may have changed the store
        self.library.model_selected.connect(self._show_model)
        self.library.online_selected.connect(self._show_online)
        self.library.online_removed.connect(self._forget_online)
        self.tabs.current_changed.connect(self._tab_changed)
        self.tabs.add_requested.connect(self.new_tab)
        self.tabs.close_requested.connect(self.close_tab)
        self.tabs.changed.connect(self._mark_dirty)
        self._build_menu()
        self._add_doc()
        self._autosave_needed = False  # something changed since the last autosave copy
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self.autosave_now)
        self._restart_autosave()

    # the graph in the current tab
    @property
    def doc(self):
        return self.tabs.current()

    @property
    def scene(self):
        return self.doc.scene

    @property
    def view(self):
        return self.doc.view

    def _add_doc(self, name=None):
        doc = GraphDoc(name or self.tabs.unique_name())
        doc.scene.selectionChanged.connect(self._node_selected)
        self.undo_group.addStack(doc.scene.undo_stack)
        doc.scene.undo_stack.cleanChanged.connect(lambda _clean: isValid(self) and self._update_title())
        doc.scene.modified.connect(self._changed_since_autosave)  # any edit, a render result, a build
        return self.tabs.add(doc)

    def _tab_changed(self, doc):
        self.params.set_scene(doc.scene if doc else None)
        self.undo_group.setActiveStack(doc.scene.undo_stack if doc else None)
        self._update_title()

    # ---- menu ------------------------------------------------------------
    def _build_menu(self):
        menu = self.menuBar().addMenu("&File")
        for text, shortcut, slot in (("&New", QKeySequence.New, self.new_graph),
                                     ("&Open…", QKeySequence.Open, self.open_graph),
                                     (None, None, None),
                                     ("&Save", QKeySequence.Save, self.save_graph),
                                     ("Save &As…", QKeySequence("Ctrl+Shift+S"), self.save_graph_as),
                                     (None, None, None),
                                     ("New &Tab", QKeySequence("Ctrl+T"), self.new_tab),
                                     ("&Close Tab", QKeySequence("Ctrl+W"), lambda: self.close_tab(self.doc)),
                                     ("&Import File as Tabs…", None, self.import_tabs),
                                     (None, None, None),
                                     ("&stable-diffusion.cpp (sd-cli.exe)…", None, self.choose_sdcpp),
                                     ("&Preferences", None, None),
                                     (None, None, None),
                                     ("E&xit", QKeySequence.Quit, self.close)):
            if text is None:
                menu.addSeparator()
                continue
            if slot is None:  # submenu
                prefs = menu.addMenu(text)
                prefs.addAction("&UI…", self.choose_ui_scale)
                prefs.addAction("&3D…", self.choose_graphics)
                prefs.addAction("&Blender…", self.choose_blender)
                prefs.addAction("&Autosave…", self.choose_autosave)
                prefs.addAction("Default &Python env…", self.choose_default_env)
                prefs.addAction("External &tools folder…", self.choose_tools_folder)
                continue
            action = QAction(text, self)
            if shortcut:
                action.setShortcut(shortcut)
            action.triggered.connect(slot)
            menu.addAction(action)
        self._add_edit_shortcuts()
        # a thin line under the menu bar separates it from the panes below
        self.menuBar().setStyleSheet(
            "QMenuBar { background: #2b2b2b; color: #ddd; border-bottom: 1px solid #111; }"
            "QMenuBar::item:selected { background: #4a6a8a; }"
            "QMenu { background: #2d2d2d; color: #ddd; border: 1px solid #555; }"
            "QMenu::item:selected { background: #4a6a8a; }")

    def _add_edit_shortcuts(self):
        """Undo / Redo / Cut / Copy / Paste / Duplicate as window shortcuts (no Edit menu - the user's choice).
        Undo and Redo act on the current tab's history. A focused text field takes Ctrl+Z / X / C / V first (Qt's
        shortcut override); anywhere else (the graph, the pane's rulers / icons / crop box) they act on the graph."""
        undo_action = self.undo_group.createUndoAction(self, "Undo")
        undo_action.setShortcut(QKeySequence.Undo)
        redo_action = self.undo_group.createRedoAction(self, "Redo")
        redo_action.setShortcuts([QKeySequence("Ctrl+Y"), QKeySequence("Ctrl+Shift+Z")])
        self.addActions([undo_action, redo_action])
        for text, key, slot in (("Cut", QKeySequence.Cut, self.cut_nodes),
                                ("Copy", QKeySequence.Copy, self.copy_nodes),
                                ("Paste", QKeySequence.Paste, self.paste_nodes),
                                ("Duplicate", QKeySequence("Ctrl+D"), self.duplicate_nodes)):
            action = QAction(text, self)
            action.setShortcut(key)
            action.triggered.connect(slot)
            self.addAction(action)  # works without being in a menu
        delete = QAction("Delete", self)  # Del: the selected nodes / wires, wherever the focus is but a text field
        delete.setShortcut(QKeySequence.Delete)
        delete.triggered.connect(lambda: self.scene.delete_selected())
        self.addAction(delete)

    # ---- copy / paste nodes (clipboard.py) -------------------------------
    def _selected_nodes(self):
        from node_editor.node import Node
        return [i for i in self.scene.selectedItems() if isinstance(i, Node)]

    def copy_nodes(self):
        if self._copy_text():
            return False  # (Cut then leaves the nodes alone)
        return clipboard.copy(self.scene, self._selected_nodes())

    @staticmethod
    def _copy_text():
        """Ctrl+C in a read-only text (Conversation, Log, a result, the Console's output, an info row): editable fields
        take Ctrl+C themselves, read-only ones don't claim it - so it reaches here. Copies their selected text."""
        from PySide6.QtWidgets import QLabel, QPlainTextEdit, QTextEdit
        w = QApplication.focusWidget()
        if isinstance(w, (QPlainTextEdit, QTextEdit)) and w.textCursor().hasSelection():
            w.copy()
            return True
        if isinstance(w, QLabel) and w.hasSelectedText():
            QApplication.clipboard().setText(w.selectedText())
            return True
        return False

    def cut_nodes(self):
        if self.copy_nodes():
            self.scene.delete_selected("Cut")

    def paste_nodes(self):
        data = clipboard.clipboard_data()
        if data is None:
            return
        view = self.view
        at = view.mapFromGlobal(QCursor.pos())
        if not view.viewport().rect().contains(at):
            at = view.viewport().rect().center()  # the mouse isn't over the graph: its middle
        clipboard.paste(self.scene, data, at=view.mapToScene(at))
        view.setFocus()

    def duplicate_nodes(self):
        """Copies of the selected nodes next to them (the system clipboard is left alone)."""
        nodes = self._selected_nodes()
        if nodes:
            clipboard.paste(self.scene, clipboard.to_data(self.scene, nodes), offset=QPointF(40, 40),
                            verb="Duplicate")

    # ---- document state --------------------------------------------------
    @property
    def dirty(self):
        """Unsaved: tabs changed, or any tab's history isn't where it was when saved (undoing back to it is clean)."""
        return self.tabs_dirty or any(isValid(d.scene) and not d.scene.undo_stack.isClean() for d in self.tabs.docs)

    def _mark_dirty(self):
        self.tabs_dirty = True
        self._changed_since_autosave()
        self._update_title()

    # ---- autosave (File > Preferences > Autosave) --------------------------
    def _changed_since_autosave(self):
        self._autosave_needed = True

    def _restart_autosave(self):
        from node_editor.settings import autosave
        cfg = autosave()
        self._autosave_timer.stop()
        if cfg["on"]:
            self._autosave_timer.start(max(1, int(cfg["minutes"])) * 60 * 1000)

    def autosave_now(self):
        """A copy of all tabs into the autosave folder, named after the graph; with all copies there, the oldest
        is replaced. The graph's own file and its saved / unsaved state are left alone."""
        from node_editor.settings import autosave
        cfg = autosave()
        if not cfg["on"] or not self._autosave_needed:
            return None
        base = os.path.splitext(os.path.basename(self.path))[0] if self.path else "untitled"
        slots = [os.path.join(cfg["folder"], f"{base}_autosave_{i}{EXT}") for i in range(1, int(cfg["count"]) + 1)]
        free = [p for p in slots if not os.path.exists(p)]
        target = free[0] if free else min(slots, key=os.path.getmtime)
        docs = self.tabs.docs
        try:
            os.makedirs(cfg["folder"], exist_ok=True)
            document.save(target, [(d.name, d.scene, d.view) for d in docs], docs.index(self.doc))
        except OSError as e:
            print(f"autosave failed: {e}")
            return None
        self._autosave_needed = False
        return target

    def choose_autosave(self):
        from node_editor.preferences import AutosaveDialog
        from node_editor.settings import set_autosave
        dialog = AutosaveDialog(self)
        if dialog.exec():
            set_autosave(dialog.values())
            self._restart_autosave()

    def _set_clean(self, path):
        self.path = path
        self.tabs_dirty = False
        for d in self.tabs.docs:
            d.scene.undo_stack.setClean()
        self._update_title()

    def _update_title(self):
        name = os.path.basename(self.path) if self.path else "untitled"
        tab = f" [{self.doc.name}]" if self.doc is not None else ""
        self.setWindowTitle(f"{name}{' *' if self.dirty else ''}{tab} — {APP_NAME}")

    def _confirm_discard(self):
        """True when it's OK to throw away the current tabs (saved, discarded, or unchanged)."""
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, APP_NAME, "Save changes to the current file?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if answer == QMessageBox.Save:
            return self.save_graph()
        return answer == QMessageBox.Discard

    def _clear_tabs(self):
        for doc in list(self.tabs.docs):
            self.tabs.remove(doc)

    # ---- actions ---------------------------------------------------------
    def new_graph(self):
        if not self._confirm_discard():
            return
        self._clear_tabs()
        self._add_doc()
        self._set_clean(None)

    def new_tab(self):
        self._add_doc()
        self._mark_dirty()

    def close_tab(self, doc):
        """Closing a tab deletes its graph from the file (asks first if it has nodes)."""
        if doc is None:
            return False
        count = len(doc.scene.nodes())
        if count:
            answer = QMessageBox.question(
                self, APP_NAME, f"Close tab “{doc.name}”? Its {count} node{'s' if count != 1 else ''} "
                "will be removed from this file.", QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                return False
        if len(self.tabs.docs) == 1:
            self._add_doc()  # there's always a graph to work in
        self.tabs.remove(doc)
        self._mark_dirty()
        return True

    def _load_tabs(self, path):
        """Add the tabs stored in `path`. Returns (added docs, index of the tab that was showing, warnings)."""
        tabs, current = document.read(path)
        added, warnings = [], []
        for name, data in tabs:
            if name is None:  # an old single-graph file: the tab is named after the file
                name = os.path.splitext(os.path.basename(path))[0]
            if name in {d.name for d in self.tabs.docs}:
                name = self.tabs.unique_name(name + "_")
            doc = self._add_doc(name)
            warnings += [f"{name}: {w}" for w in document.load_graph(data, doc.scene, doc.view)]
            added.append(doc)
        return added, current, warnings

    def open_graph(self, path=None):
        if not self._confirm_discard():
            return False
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "Open", self._dialog_dir(), FILE_FILTER)
            if not path:
                return False
        path = os.path.normpath(path)
        why = _not_a_graph(path)
        if why:
            QMessageBox.warning(self, APP_NAME, why)
            return False
        try:
            document.read(path)  # check before closing what's open
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't open {os.path.basename(path)}:\n{e}")
            return False
        self._clear_tabs()
        added, current, warnings = self._load_tabs(path)
        if not added:
            self._add_doc()
        else:
            self.tabs.select(added[min(max(current, 0), len(added) - 1)])
        self._set_clean(path)
        if any("became a Merge node" in w for w in warnings):
            self._mark_dirty()  # the graph was changed on the way in: saving keeps the new Merge nodes
        if warnings:
            QMessageBox.information(self, APP_NAME, "Opened with some notes:\n\n" + "\n".join(warnings))
        return True

    def import_tabs(self):
        """Add the tabs of another file (e.g. an older single-graph file) to this one."""
        path, _ = QFileDialog.getOpenFileName(self, "Import file as tabs", self._dialog_dir(), FILE_FILTER)
        if not path:
            return
        why = _not_a_graph(path)
        if why:
            QMessageBox.warning(self, APP_NAME, why)
            return
        blank = self.doc if len(self.tabs.docs) == 1 and not self.scene.nodes() else None
        try:
            added, _current, warnings = self._load_tabs(os.path.normpath(path))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't import {os.path.basename(path)}:\n{e}")
            return
        if added and blank is not None:
            self.tabs.remove(blank)  # the empty starting tab isn't needed
        self._mark_dirty()
        if warnings:
            QMessageBox.information(self, APP_NAME, "Imported with some problems:\n\n" + "\n".join(warnings))

    def save_graph(self):
        if not self.path:
            return self.save_graph_as()
        return self._write(os.path.splitext(self.path)[0] + EXT)  # always a .tau file

    def save_graph_as(self):
        start = os.path.splitext(self.path)[0] + EXT if self.path else os.path.join(self._dialog_dir(), "untitled" + EXT)
        path, _ = QFileDialog.getSaveFileName(self, "Save", start, FILE_FILTER)
        if not path:
            return False
        if os.path.splitext(path)[1].lower() != EXT:
            path = os.path.splitext(path)[0] + EXT
        return self._write(os.path.normpath(path))

    def _write(self, path):
        docs = self.tabs.docs
        try:
            document.save(path, [(d.name, d.scene, d.view) for d in docs], docs.index(self.doc))
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Couldn't save {os.path.basename(path)}:\n{e}")
            return False
        self._set_clean(path)
        return True

    def choose_default_env(self):
        """Pick the Python environment models use when their Model node doesn't name one."""
        from node_editor.engine.client import python_in
        from node_editor.settings import default_python_env, set_default_python_env
        current = default_python_env()
        path = QFileDialog.getExistingDirectory(self, f"Default Python env (now: {current})", current)
        if not path:
            return
        path = os.path.normpath(path)
        if not python_in(path):
            QMessageBox.warning(self, APP_NAME, f"No python.exe found in:\n{path}\n\nThe default wasn't changed.")
            return
        set_default_python_env(path)
        for doc in self.tabs.docs:  # re-check models and Modality nodes against the new default
            for node in doc.scene.nodes():
                node.upstream_changed()
                node.notify()
        self.params.set_node(self.params.node)  # refresh the pane (placeholder shows the new default)

    def choose_ui_scale(self):
        """File > Preferences > UI: whole-UI scale (Qt applies it at start-up, so offer a restart)."""
        from node_editor.preferences import UiDialog
        from node_editor.settings import set_ui_scale, ui_scale
        dialog = UiDialog(self)
        if not dialog.exec() or dialog.value() == ui_scale():
            return
        set_ui_scale(dialog.value())
        self._offer_restart("Restart now to apply the new UI scale?")

    def choose_graphics(self):
        """File > Preferences > 3D: the graphics API (Qt takes it at start-up, so offer a restart)."""
        from node_editor.preferences import GraphicsDialog
        from node_editor.settings import graphics_api, set_graphics_api
        dialog = GraphicsDialog(self)
        if not dialog.exec() or dialog.value() == graphics_api():
            return
        set_graphics_api(dialog.value())
        self._offer_restart("Restart now to draw with the new graphics API?")

    def _offer_restart(self, question):
        answer = QMessageBox.question(self, APP_NAME, question, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if answer == QMessageBox.Yes and self.close():  # close() asks to save first; False if cancelled
            args = ([] if getattr(sys, "frozen", False) else [os.path.abspath(__file__)]) + \
                ([self.path] if self.path else [])
            QProcess.startDetached(sys.executable, args, os.path.dirname(os.path.abspath(__file__)))

    def choose_blender(self):
        """Which Blender converts Alembic files for the 3D viewer (else the newest one installed is used)."""
        from node_editor.settings import blender_exe, set_blender_exe
        current = blender_exe()
        path, _ = QFileDialog.getOpenFileName(self, f"Blender (now: {current or 'none found'})",
                                              os.path.dirname(current) if current else "", "blender.exe (blender.exe)")
        if path:
            set_blender_exe(os.path.normpath(path))

    def choose_tools_folder(self):
        """A folder of command-line tools outside the app (its README describes them): the Executor's brain is told
        about it (it runs them with PowerShell)."""
        from node_editor.settings import set_tools_folder, tools_folder
        current = tools_folder()
        path = QFileDialog.getExistingDirectory(self, f"External tools folder (now: {current or 'none'})", current)
        if path:
            set_tools_folder(os.path.normpath(path))

    def choose_sdcpp(self):
        """Which stable-diffusion.cpp build runs GGUF models."""
        from node_editor.settings import sdcpp_exe, set_sdcpp_exe
        current = sdcpp_exe()
        path, _ = QFileDialog.getOpenFileName(self, f"stable-diffusion.cpp sd-cli.exe (now: {current})",
                                              os.path.dirname(current), "sd-cli.exe (sd-cli.exe)")
        if path:
            set_sdcpp_exe(os.path.normpath(path))

    def _show_model(self, repo):
        """A model picked in the Library: its details go to the parameter pane."""
        if repo is None:
            return
        self.scene.blockSignals(True)  # deselecting nodes mustn't take the pane back
        self.scene.clearSelection()
        self.scene.blockSignals(False)
        self.view.viewport().update()
        self.params.show_info("Model", repo.repo_id, describe(repo))

    def _forget_online(self, name):
        """An online model was removed: Model nodes that ran it take their provider's first one (or none)."""
        from node_editor import online
        for doc in self.tabs.docs:
            for node in doc.scene.nodes():
                if node.type_name == "model" and node.params.get("online_model") == name:
                    choices = online.model_options(node)
                    node.set_param("online_model", choices[0][0] if choices else "")  # its settings follow

    def _show_online(self, entry):
        """An online model picked in the Library: what its settings file says."""
        self.scene.blockSignals(True)
        self.scene.clearSelection()
        self.scene.blockSignals(False)
        self.view.viewport().update()
        if "service" in entry:  # a service row (Replicate, Google...)
            self.params.show_info("Online service", entry["service"]["label"], describe_online(entry))
        else:
            self.params.show_info("Online model", entry["label"], describe_online(entry))

    def _node_selected(self):
        scene = self.sender()
        if not isValid(scene):  # a scene clears its selection while being destroyed
            return
        if scene.selectedItems():
            self.library.clear_selection()

    def _dialog_dir(self):
        if self.path:
            return os.path.dirname(self.path)
        os.makedirs(GRAPHS_DIR, exist_ok=True)
        return GRAPHS_DIR

    def closeEvent(self, event):
        if not self._confirm_discard():
            event.ignore()
            return
        from node_editor.engine.client import shutdown_all
        shutdown_all()  # stop the AI worker processes with the app
        from node_editor import media_cache
        media_cache.clear()  # Edit Media nodes' instances live only while the app runs
        self.console.shutdown()
        super().closeEvent(event)


class HintDelayStyle(QProxyStyle):
    """The app's own style, with hints (tooltips) appearing after the mouse rests on something for 1 s."""

    def styleHint(self, hint, option=None, widget=None, data=None):
        if hint == QStyle.SH_ToolTip_WakeUpDelay:
            return 1000
        return super().styleHint(hint, option, widget, data)


def place_on_screen(window):
    """Centre the window on the screen under the mouse, no bigger than that screen. With a custom UI scale on
    several monitors Qt can otherwise open it off every screen (seen at x = -4811 on a 2 x 4K desktop)."""
    from PySide6.QtGui import QCursor, QGuiApplication
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    area = screen.availableGeometry()
    w = min(window.width(), int(area.width() * 0.95))
    h = min(window.height(), int(area.height() * 0.95))
    window.resize(w, h)
    window.move(area.x() + (area.width() - w) // 2, area.y() + (area.height() - h) // 2)


def main():
    # Qt's FFmpeg media backend prints every opened file's stream details; keep the console for real problems
    os.environ.setdefault("QT_LOGGING_RULES", "qt.multimedia.ffmpeg*=false")
    os.environ.setdefault("QT_FFMPEG_DEBUG", "0")
    # windows compose through Qt's RHI from the start: the first 3D viewer (a QQuickWidget) would otherwise switch
    # the main window over at that moment, rebuilding it (it looked like the whole app reloading)
    os.environ.setdefault("QT_WIDGETS_RHI", "1")
    # drawing (the 3D viewer, the composed windows): File > Preferences > 3D, Direct3D 11 by default - the GPU's
    # driver on AMD and NVIDIA alike; with no usable GPU Windows' own software adapter ("Microsoft Basic Render
    # Driver") draws instead. Set explicitly, so a Qt update doesn't switch it silently
    if sys.platform == "win32":
        from node_editor.settings import graphics_api
        os.environ.setdefault("QSG_RHI_BACKEND", graphics_api())  # Qt Quick (the 3D viewer)
        # the composed windows must use the same one, or a QQuickWidget draws black
        os.environ.setdefault("QT_WIDGETS_RHI_BACKEND", os.environ["QSG_RHI_BACKEND"])
    # a hard crash (e.g. inside Qt's 3D renderer) writes what Python was doing to logs/crash.log
    import faulthandler
    logs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(logs, exist_ok=True)
    crash_log = open(os.path.join(logs, "crash.log"), "a", encoding="utf-8")  # noqa: SIM115 - open for the whole run
    crash_log.write(f"\n===== started {__import__('time').strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    crash_log.flush()
    faulthandler.enable(crash_log, all_threads=True)
    from node_editor.settings import ui_scale
    if ui_scale() != 1.0:  # File > Preferences > UI; Qt scales everything, but only from the start
        os.environ["QT_SCALE_FACTOR"] = f"{ui_scale():g}"
    from node_editor import media_cache
    media_cache.clear()  # leftovers of a session that didn't close normally
    from node_editor.settings import apply_import
    imported = apply_import(os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings_import.json"))
    if imported:  # settings handed over in a file (applied once; the file is renamed *.applied.json)
        print("settings_import.json applied:\n  " + "\n  ".join(imported))
    if os.name == "nt":  # the taskbar shows tauUI's own icon, not Python's
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("tauUI.app")
    app = QApplication(sys.argv)
    app.setStyle(HintDelayStyle(app.style().name()))
    from node_editor import logo
    app.setWindowIcon(logo.icon())
    window = MainWindow()
    place_on_screen(window)
    window.show()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        window.open_graph(sys.argv[1])  # open a graph given on the command line
    sys.exit(app.exec())


if __name__ == "__main__":
    if sys.argv[1:2] == ["--mcp-server"]:  # the Executor's tool relay, when this runs as a built .exe
        from node_editor import mcp_server
        mcp_server.main()
    else:
        main()
