"""File > Preferences dialogs. Numbers are set on rulers, like parameters (type a value or drag the scale)."""

import os

from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout, QWidget)

from .ruler import RulerField
from .settings import AUTOSAVE_DEFAULTS, autosave, set_tick_sound, tick_sound, ui_scale


def _dialog(parent, title, form, note, width=460):
    d = QDialog(parent)
    d.setWindowTitle(title)
    d.setMinimumWidth(width)
    buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    buttons.accepted.connect(d.accept)
    buttons.rejected.connect(d.reject)
    layout = QVBoxLayout(d)
    layout.addLayout(form)
    if note:
        label = QLabel(note)
        label.setStyleSheet("color: #888;")
        label.setWordWrap(True)
        layout.addWidget(label)
    layout.addWidget(buttons)
    return d


class UiDialog:
    """Whole-UI scale: panels, graph, nodes and text together (on top of Windows' own display scaling)."""

    def __init__(self, parent=None):
        self.scale = RulerField(round(ui_scale() * 100), "int", 5, 50, 300, 100)
        form = QFormLayout()
        form.addRow("UI scale %", self.scale)
        sound = QCheckBox("Rulers click as they move", checked=tick_sound())
        sound.toggled.connect(set_tick_sound)  # takes effect at once
        form.addRow("Tick sound", sound)
        self.dialog = _dialog(parent, "Preferences — UI", form,
                              "UI scale covers the whole interface, not just fonts. It takes effect when the app "
                              "restarts.")

    def exec(self):
        return self.dialog.exec()

    def value(self):
        return self.scale.ruler.value / 100


class GraphicsDialog:
    """Which graphics API draws the app (the 3D viewer above all). Taken at start-up."""

    def __init__(self, parent=None):
        from PySide6.QtWidgets import QComboBox
        from .settings import DEFAULT_GRAPHICS_API, GRAPHICS_APIS, graphics_api
        self.combo = QComboBox()
        for key, label in GRAPHICS_APIS:
            self.combo.addItem(label + ("  (default)" if key == DEFAULT_GRAPHICS_API else ""), key)
        self.combo.setCurrentIndex(self.combo.findData(graphics_api()))
        form = QFormLayout()
        form.addRow("Graphics API", self.combo)
        self.dialog = _dialog(parent, "Preferences — 3D", form,
                              "All of them draw on the graphics card's own driver (AMD or NVIDIA alike); without "
                              "a usable card Direct3D falls back to Windows' software renderer. Try another if the 3D "
                              "viewer misbehaves. It takes effect "
                              "when the app restarts.")

    def exec(self):
        return self.dialog.exec()

    def value(self):
        return self.combo.currentData()


class AutosaveDialog:
    """Autosave: a copy of the open graph every N minutes into a folder; the oldest of the kept copies is replaced."""

    def __init__(self, parent=None):
        now = autosave()
        self.on = QCheckBox("Save a copy of the open graph regularly", checked=bool(now["on"]))
        self.folder = QLineEdit(now["folder"])
        browse = QPushButton("…")
        browse.setFixedWidth(32)
        browse.clicked.connect(self._pick)
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(self.folder, 1)
        h.addWidget(browse)
        self.minutes = RulerField(int(now["minutes"]), "int", 1, 1, 120, AUTOSAVE_DEFAULTS["minutes"])
        self.count = RulerField(int(now["count"]), "int", 1, 1, 20, AUTOSAVE_DEFAULTS["count"])
        form = QFormLayout()
        form.addRow("Autosave", self.on)
        form.addRow("Folder", row)
        form.addRow("Every (minutes)", self.minutes)
        form.addRow("Copies kept", self.count)
        self.dialog = _dialog(parent, "Preferences — Autosave", form,
                              "Copies are named after the graph (<name>_autosave_1.tau …). When all are there, the "
                              "oldest one is replaced. Only changed graphs are saved. Open a copy with File > Open.")

    def _pick(self):
        path = QFileDialog.getExistingDirectory(self.dialog, "Autosave folder", self.folder.text())
        if path:
            self.folder.setText(os.path.normpath(path))

    def exec(self):
        return self.dialog.exec()

    def values(self):
        return {"on": self.on.isChecked(), "folder": self.folder.text().strip() or AUTOSAVE_DEFAULTS["folder"],
                "minutes": max(1, int(self.minutes.ruler.value)), "count": max(1, int(self.count.ruler.value))}
