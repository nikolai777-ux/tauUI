"""Parameter pane: docked on the right (Houdini-style), shows the selected node's parameters."""

import os
import re

from PySide6.QtCore import QPoint, QRect, QSize, Qt, QUrl
from PySide6.QtGui import (
    QAction, QColor, QImageReader, QPainter, QPixmap, QSyntaxHighlighter, QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLayout,
    QLineEdit, QMenu, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QToolButton,
    QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from . import spellcheck, undo

STYLE = """
QWidget#ParamPane { background: #2d2d2d; }
QWidget#PaneTitle { background: #353535; border-bottom: 1px solid #1f1f1f; }
QLabel#PaneTitleText { color: #bbb; font-weight: bold; }
QWidget#Header { background: #333; border-bottom: 1px solid #262626; }
QScrollArea { border: none; background: #2d2d2d; }
QWidget#Body { background: #2d2d2d; }
QLabel { color: #ccc; }
QLabel#NodeType { color: #999; }
QLabel#Empty { color: #888; }
QLineEdit, QSpinBox, QDoubleSpinBox, QPlainTextEdit, QComboBox { background: #1e1e1e; color: #ddd; border: 1px solid #444; padding: 2px; }
QLineEdit#NameEdit { font-weight: bold; }
QComboBox QAbstractItemView { background: #1e1e1e; color: #ddd; selection-background-color: #4a6a8a; }
QLabel#TextLabel { color: #999; }
QLabel#InfoHeader { color: #888; border-top: 1px solid #444; padding-top: 6px; }
QLabel#InfoKey { color: #888; }
QLabel#InfoValue { color: #ddd; }
QCheckBox { color: #ddd; }
QToolButton { color: #ccc; border: none; padding: 0 4px; }
QToolButton:hover { color: #fff; background: #555; }
QMenu { background: #2d2d2d; color: #ddd; border: 1px solid #555; }
QMenu::item:selected { background: #4a6a8a; }
QMenu::item:disabled { color: #777; }
QMenu::separator { height: 1px; background: #444; margin: 3px 6px; }
QPushButton#Action { background: #3d5a78; color: #eee; border: 1px solid #5b7ea0; padding: 5px; font-weight: bold; }
QPushButton#Action:hover { background: #4a6a8a; }
QToolButton#Browse { border: 1px solid #444; background: #3a3a3a; padding: 1px 6px; }
QToolButton#Tool { border: 1px solid #222; background: #3a3a3a; border-radius: 2px; padding: 4px 6px; }
QToolButton#Tool:hover { background: #4a6a8a; }
QToolButton#Tool:checked { background: #3d5a78; border: 1px solid #5b7ea0; }
QToolButton#ViewToggle { border: 1px solid #222; background: #3a3a3a; border-radius: 2px; padding: 1px; }
QToolButton#ViewToggle:hover { background: #4a6a8a; }
QToolButton#ViewToggle:checked { background: #3d5a78; border: 1px solid #5b7ea0; }
QComboBox#ViewCamera { padding: 0 4px; }
QToolButton#AutoKey { border: 1px solid #222; background: #3a3a3a; border-radius: 2px; padding: 1px; }
QToolButton#AutoKey:hover { background: #4a6a8a; }
QToolButton#AutoKey:checked { background: #8a2c2c; border: 1px solid #c05050; }
QToolButton#Lock { border: none; background: transparent; padding: 2px; }
QLineEdit[linked="true"], QPlainTextEdit[linked="true"] { background: #3a3a3a; color: #9a9a9a; }
QLabel[linked="true"] { color: #7fb2e5; }
QToolButton#Lock:hover { background: #3a3a3a; }
QToolButton#Play { border: 1px solid #5b7ea0; background: #3d5a78; color: #eee; padding: 4px 10px; font-weight: bold; }
"""

# Spin box arrows laid out by the stylesheet itself: with only the box styled, the Windows 11 style drew the
# arrows in one place while the text field (placed by the stylesheet) covered one of them and took its clicks.
_ASSETS = os.path.join(os.path.dirname(__file__), "assets").replace("\\", "/")
STYLE += f"""
QSpinBox, QDoubleSpinBox {{ padding-right: 50px; }}
QSpinBox::up-button, QDoubleSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border; subcontrol-position: right; width: 22px; top: 0; bottom: 0;
    background: #3a3a3a; border: 1px solid #222; }}
QSpinBox::up-button, QDoubleSpinBox::up-button {{ position: relative; right: 24px; }}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{ background: #555; }}
QSpinBox::up-button:pressed, QDoubleSpinBox::up-button:pressed,
QSpinBox::down-button:pressed, QDoubleSpinBox::down-button:pressed {{ background: #4a6a8a; }}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({_ASSETS}/arrow_up.svg); width: 10px; height: 6px; }}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({_ASSETS}/arrow_down.svg); width: 10px; height: 6px; }}
"""

IMAGE_FILTER = "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp *.tga);;All files (*)"


def _wrappable(text):
    """Let long paths/names wrap: allow line breaks after \\ / _ (labels only break at spaces)."""
    for ch in ("\\", "/", "_"):
        text = text.replace(ch, ch + "\u200b")
    return re.sub(r"([^\s\u200b]{24})(?=[^\s\u200b])", "\\1\u200b", text)  # long hashes too


def _fill_info(grid, row, title, rows):
    """Add a header and label/value rows to a grid; returns the next free row."""
    grid.addWidget(QLabel(title, objectName="InfoHeader"), row, 0, 1, 2)
    row += 1
    for label, value in rows:
        k = QLabel(label, objectName="InfoKey")
        k.setAlignment(Qt.AlignRight | Qt.AlignTop)
        if isinstance(value, tuple) and value[0] == "link":  # ("link", text, url): opens in the browser
            _, text, url = value
            v = QLabel(f'<a href="{url}" style="color:#6fa8dc;">{_wrappable(text)}</a>', objectName="InfoValue")
            v.setTextFormat(Qt.RichText)
            v.setOpenExternalLinks(True)
            v.setToolTip(url)
            v.setTextInteractionFlags(Qt.TextBrowserInteraction)
        else:
            v = QLabel(_wrappable(str(value)), objectName="InfoValue")
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.setWordWrap(True)
        v.setMinimumWidth(1)  # long values wrap instead of widening the pane
        grid.addWidget(k, row, 0)
        grid.addWidget(v, row, 1)
        row += 1
    return row


def clean_path(text):
    """Accept paths pasted from Explorer's 'Copy as path' (wrapped in quotes) or copied from the info rows
    (which carry invisible line-break hints)."""
    return text.replace("​", "").strip().strip('"').strip()


def _filter_extensions(file_filter):
    """('.safetensors', '.ckpt', ...) from a dialog filter like 'Models (*.safetensors *.ckpt)'."""
    return tuple(e.lower() for e in re.findall(r"\*(\.\w+)", file_filter or ""))


def dropped_path(mime):
    """The single local path in a drag (from the Library or Explorer), or None."""
    urls = [u for u in mime.urls() if u.isLocalFile()]
    if len(urls) == 1:
        return os.path.normpath(urls[0].toLocalFile())
    return None


class PathEdit(QLineEdit):
    """A path field that takes a dropped file or folder (replacing its text) when accept(path) allows it."""

    def __init__(self, text, accept, **kw):
        super().__init__(text, **kw)
        self.accept = accept

    def _ok(self, event):
        path = dropped_path(event.mimeData())
        return path if path and self.accept(path) else None

    def dragEnterEvent(self, event):
        if self._ok(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dropEvent(self, event):
        path = self._ok(event)
        if path:
            self.setText(path)
            event.acceptProposedAction()
        else:
            event.ignore()


class ImagePreview(QWidget):
    """Shows an image at the pane's width, keeping its aspect ratio. Double-click opens it."""

    MAX_HEIGHT_RATIO = 1.5  # very tall images are capped (then scaled to fit)

    def __init__(self, parent):
        super().__init__(parent)  # parented up front so show() never makes a top-level window
        self.hide()
        sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMinimumSize(40, 20)
        self.setToolTip("Double-click to open")
        self._pixmap = None
        self._scaled = None
        self._message = ""
        self._path = None

    def set_path(self, path):
        path = clean_path(path or "")
        if path == self._path:
            return
        self._path = path
        self._pixmap, self._scaled, self._message = None, None, ""
        if path and os.path.isfile(path):
            reader = QImageReader(path)
            reader.setAutoTransform(True)  # apply EXIF orientation, so photos appear upright like in other viewers
            pix = QPixmap.fromImage(reader.read())
            if pix.isNull():
                self._message = "Can't read this file as an image"
            else:
                self._pixmap = pix
        elif path:
            self._message = "File not found"
        self.setVisible(bool(self._pixmap or self._message))
        self.updateGeometry()
        self.update()

    @property
    def has_image(self):
        return self._pixmap is not None

    def image_size(self):
        return self._pixmap.size() if self._pixmap else QSize()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        if not self._pixmap:
            return 20
        return min(round(w * self._pixmap.height() / self._pixmap.width()), round(w * self.MAX_HEIGHT_RATIO))

    def sizeHint(self):
        return QSize(200, self.heightForWidth(200))

    def mouseDoubleClickEvent(self, event):
        if self._pixmap and self._path:
            os.startfile(self._path)  # open in the system's image viewer

    def paintEvent(self, event):
        p = QPainter(self)
        if self._pixmap:
            target = self._pixmap.size().scaled(self.size(), Qt.KeepAspectRatio)
            if self._scaled is None or self._scaled.size() != target:
                self._scaled = self._pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            p.drawPixmap((self.width() - self._scaled.width()) // 2, (self.height() - self._scaled.height()) // 2,
                         self._scaled)
        elif self._message:
            p.setPen(QColor(140, 140, 140))
            p.drawText(self.rect(), Qt.AlignLeft | Qt.AlignVCenter, self._message)
        p.end()


class AudioPlayer(QWidget):
    """An audio file with a waveform playbar (audiobar.py): play / pause, time, click / drag / wheel to move.
    Click it, then Left / Right = 1 s, Space = play / pause."""

    def __init__(self):
        super().__init__()
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from .audiobar import AudioPlaybar
        self._path = ""
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.playbar = AudioPlaybar(self.player)
        self.label = QLabel("No audio yet", objectName="TextLabel")
        self.setFocusPolicy(Qt.ClickFocus)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(self.playbar)
        v.addWidget(self.label)
        self.set_path("")

    def set_path(self, path):
        path = path or ""
        ok = bool(path) and os.path.isfile(path)
        if path == self._path and ok:
            return
        self._path = path if ok else ""
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(self._path) if ok else QUrl())
        self.playbar.set_file(self._path)
        self.playbar.setVisible(ok)
        self.label.setVisible(not ok)

    def toggle(self):
        self.playbar.toggle()

    def keyPressEvent(self, event):
        if not self._path:
            return super().keyPressEvent(event)
        if event.key() == Qt.Key_Left:
            self.playbar.step(-1)
        elif event.key() == Qt.Key_Right:
            self.playbar.step(1)
        elif event.key() == Qt.Key_Space:
            self.playbar.toggle()
        else:
            return super().keyPressEvent(event)
        event.accept()


def no_focus(video_widget, owner=None):
    """Qt's video widget draws in a native child that takes the keyboard focus when it is shown - away from the
    graph (a selected Video node then ate Del / Tab). Whenever the focus lands on it, it goes back where it came
    from; after a click on the picture it goes to `owner` (the player: Space / arrows)."""
    from PySide6.QtWidgets import QApplication
    video_widget.setFocusPolicy(Qt.NoFocus)

    def moved(old, new):
        if not isValid(video_widget) or new is None or not (new is video_widget or video_widget.isAncestorOf(new)):
            return
        clicked = QApplication.mouseButtons() != Qt.NoButton
        target = owner if clicked and owner is not None and isValid(owner) else old
        if target is not None and isValid(target) and target is not new:
            target.setFocus()
    QApplication.instance().focusChanged.connect(moved)
    video_widget.destroyed.connect(lambda: QApplication.instance().focusChanged.disconnect(moved))


class VideoPlayer(QWidget):
    """A video (with its sound) at the pane's width with a Houdini-style playbar (playbar.py): timeline, frame
    stepping, frame field; loops. Click it, then Left / Right = one frame, Space = play / pause.
    Double-click the picture to open the file in the system player."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self._path = ""
        self.view = QVideoWidget()
        self.view.setMinimumHeight(60)
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.view.setToolTip("Double-click to open")
        self.view.mouseDoubleClickEvent = lambda e: self._path and os.startfile(self._path)
        no_focus(self.view, self)
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.view)
        self.player.setLoops(QMediaPlayer.Infinite)
        self.player.metaDataChanged.connect(self._fit)
        from .playbar import Playbar
        self.playbar = Playbar(self.player)
        self.label = QLabel("No video yet", objectName="TextLabel")
        self.setFocusPolicy(Qt.ClickFocus)
        self.view.setFocusProxy(self)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        v.addWidget(self.view)
        v.addWidget(self.playbar)
        v.addWidget(self.label)
        self._aspect = 9 / 16
        self.set_path("")

    def set_path(self, path):
        path = path or ""
        ok = bool(path) and os.path.isfile(path)
        if path == self._path and ok:
            return
        self._path = path if ok else ""
        self.player.stop()
        self.player.setSource(QUrl.fromLocalFile(self._path) if ok else QUrl())
        self.playbar.set_file(self._path)
        self.view.setVisible(ok)
        self.playbar.setVisible(ok)
        self.label.setVisible(not ok)  # the file name is in the field above; only say when there's nothing
        self.view.setToolTip(f"{os.path.basename(self._path)} - double-click to open" if ok else "")
        if ok:
            self.player.pause()  # shows the first frame

    def _fit(self):
        from PySide6.QtMultimedia import QMediaMetaData
        size = self.player.metaData().value(QMediaMetaData.Resolution)
        if size and size.width():
            self._aspect = size.height() / size.width()
        self._resize_view()

    def _resize_view(self):
        self.view.setFixedHeight(max(60, int(self.width() * self._aspect)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._resize_view()

    def toggle(self):
        self.playbar.toggle()

    def keyPressEvent(self, event):
        if not self._path:
            return super().keyPressEvent(event)
        if event.key() == Qt.Key_Left:
            self.playbar.step(-1)
        elif event.key() == Qt.Key_Right:
            self.playbar.step(1)
        elif event.key() == Qt.Key_Space:
            self.playbar.toggle()
        else:
            return super().keyPressEvent(event)
        event.accept()


class SpellHighlighter(QSyntaxHighlighter):
    """Red wavy underline under words Windows' spell checker flags, in the language language() returns."""

    def __init__(self, document, language):
        super().__init__(document)
        self.language = language
        self.fmt = QTextCharFormat()
        self.fmt.setUnderlineStyle(QTextCharFormat.SpellCheckUnderline)
        self.fmt.setUnderlineColor(QColor(230, 80, 80))

    def highlightBlock(self, text):
        language = self.language()
        if language:
            for start, length, _ in spellcheck.check(language, text):
                self.setFormat(start, length, self.fmt)


class TextField(QPlainTextEdit):
    """Multi-line text parameter (e.g. prompts). Tab moves focus instead of inserting a tab.
    With language (a callable returning a Windows language tag, or ''), mistakes are underlined and
    right-click offers corrections."""

    def __init__(self, text, read_only=False, language=None):
        super().__init__()
        self.setPlainText(text)
        self.setTabChangesFocus(True)
        self.setReadOnly(read_only)
        self.setMinimumHeight(50)
        self.language = language
        self._checked_language = language() if language else ""
        self.highlighter = SpellHighlighter(self.document(), language) if language else None

    def sizeHint(self):
        return QSize(super().sizeHint().width(), 110)

    on_submit = None  # set for a field whose Enter sends (e.g. a message); Shift+Enter starts a new line

    def keyPressEvent(self, event):
        if (self.on_submit is not None and event.key() in (Qt.Key_Return, Qt.Key_Enter)
                and not event.modifiers() & Qt.ShiftModifier):
            self.on_submit()
            return
        super().keyPressEvent(event)

    def language_changed(self):
        if self.highlighter and self.language() != self._checked_language:
            self._checked_language = self.language()
            self.highlighter.rehighlight()

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        language = self.language() if self.language else ""
        if language and not self.isReadOnly():
            cursor = self.cursorForPosition(event.pos())
            block, pos = cursor.block(), cursor.positionInBlock()
            for start, length, replacement in spellcheck.check(language, block.text()):
                if start <= pos <= start + length:
                    self._add_corrections(menu, language, block, start, length, replacement)
                    break
        if getattr(self, "extra_menu", None):  # e.g. Copy / Paste Parameter (placed by their own Copy / Paste)
            self.extra_menu(menu)
        menu.exec(event.globalPos())

    def _add_corrections(self, menu, language, block, start, length, replacement):
        span = QTextCursor(block)
        span.setPosition(block.position() + start)
        span.setPosition(block.position() + start + length, QTextCursor.KeepAnchor)
        word = span.selectedText()
        choices = [replacement] if replacement is not None else spellcheck.suggest(language, word)
        first = menu.actions()[0] if menu.actions() else None
        actions = []
        for choice in choices:
            act = QAction(choice or f"Delete repeated '{word}'", menu)
            act.triggered.connect(lambda _=False, c=choice: span.insertText(c))
            actions.append(act)
        if not choices:
            none = QAction("(no suggestions)", menu)
            none.setEnabled(False)
            actions.append(none)
        word_actions = () if replacement == "" else (  # a repeated word is spelled fine: only offer to delete it
            (f"Add '{word}' to dictionary", spellcheck.add), (f"Ignore '{word}'", spellcheck.ignore))
        for text, handler in word_actions:
            act = QAction(text, menu)
            act.triggered.connect(lambda _=False, h=handler: (h(language, word), self.highlighter.rehighlight()))
            actions.append(act)
        menu.insertActions(first, actions)
        menu.insertSeparator(first)


class FlowLayout(QLayout):
    """Items left to right, wrapping onto another line when the pane is too narrow (ticks / icons sharing a row)."""

    def __init__(self, spacing=10, line_spacing=6):
        super().__init__()
        self._items, self._spacing, self._line_spacing = [], spacing, line_spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations()

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._place(QRect(0, 0, width, 0), dry=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, dry=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _place(self, rect, dry):
        x, y, line_h = rect.x(), rect.y(), 0
        for item in self._items:
            if item.widget() is not None and not item.widget().isVisibleTo(self.parentWidget()):
                continue
            hint = item.sizeHint()
            if x > rect.x() and x + hint.width() > rect.right() + 1:  # no room left on this line
                x, y, line_h = rect.x(), y + line_h + self._line_spacing, 0
            if not dry:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y()


class BoxHeader(QLabel):
    """A text box's label with a fold arrow: click it to fold the box away or bring it back."""

    def __init__(self, title, folded, on_toggle):
        super().__init__(objectName="TextLabel")
        self.title, self.folded, self.on_toggle = title, folded, on_toggle
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click to fold / unfold")
        self._show()

    def _show(self):
        self.setText(("\u25b8  " if self.folded else "\u25be  ") + self.title)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.folded = not self.folded
            self._show()
            self.on_toggle(self.folded)


class BoxGrip(QWidget):
    """The bottom edge of a text box: drag it to make the box taller or shorter; double-click = automatic height."""

    def __init__(self, box, on_height):
        super().__init__()
        self.box, self.on_height, self._drag = box, on_height, None
        self.setFixedHeight(8)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip("Drag to resize \u00b7 double-click: default height")

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag = (event.globalPosition().y(), self.box.height())

    def mouseMoveEvent(self, event):
        if self._drag is not None:
            y0, h0 = self._drag
            self.box.setFixedHeight(max(40, round(h0 + event.globalPosition().y() - y0)))

    def mouseReleaseEvent(self, event):
        if self._drag is not None:
            self._drag = None
            self.on_height(self.box.height())

    def mouseDoubleClickEvent(self, event):
        self._drag = None
        self.on_height(None)

    def paintEvent(self, event):
        from PySide6.QtGui import QColor, QPainter, QPen
        p = QPainter(self)
        p.setPen(QPen(QColor(120, 120, 120) if self.underMouse() else QColor(72, 72, 72), 2))
        mid, y = self.width() / 2, self.height() / 2
        p.drawLine(round(mid - 14), round(y), round(mid + 14), round(y))

    def enterEvent(self, event):
        self.update()

    def leaveEvent(self, event):
        self.update()


class ParamPane(QWidget):
    """Permanent pane showing the selected node's parameters, read-only info and results."""

    def __init__(self, scene):
        super().__init__()
        self.setObjectName("ParamPane")
        self.setAttribute(Qt.WA_StyledBackground)
        self.setStyleSheet(STYLE)
        self.scene = None
        self.node = None
        self.body = None
        self.name_edit = None
        self.param_rows, self.param_sync, self.previews = [], {}, []
        self.spell_fields = []
        self.live_widgets = []
        self._links_shown = set()
        # ONE 3D viewer for the whole app, never destroyed nor moved: destroying one crashed Qt (its 3D renderer:
        # QSSGRenderRoot::markDirty; Qt Quick's item tree) and so did moving one to another parent (heap corruption).
        # It sits in a fixed area under the node's name (outside the scrolling parameters, which are rebuilt) and is
        # switched to whichever node is shown (GeometryView.set_node: that node's scene, camera, frame, selection);
        # with no viewer to show, its scene is unloaded. Its header line (plain widgets) is rebuilt per node.
        self._geo = None        # the GeometryView
        self._geo_param = None  # the parameter it shows now
        self._geo_used = False  # (during a rebuild) the node shown has a viewer
        self._info_rows = None

        title = QWidget(objectName="PaneTitle")
        t = QHBoxLayout(title)
        t.setContentsMargins(10, 5, 10, 5)
        t.addWidget(QLabel("Parameters", objectName="PaneTitleText"))

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setMinimumWidth(0)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.head_slot = QWidget()  # the node's type + name (rebuilt for each node)
        QVBoxLayout(self.head_slot).setContentsMargins(0, 0, 0, 0)
        self.viewer_area = QWidget(objectName="Body")  # the viewers' pages (see above)
        self._viewer_layout = QVBoxLayout(self.viewer_area)
        self._viewer_layout.setContentsMargins(10, 4, 10, 0)
        self._viewer_layout.setSpacing(0)
        self.viewer_area.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(title)
        layout.addWidget(self.head_slot)
        layout.addWidget(self.viewer_area)
        layout.addWidget(self.scroll, 1)
        self.setMinimumWidth(0)

        self.set_scene(scene)

    def set_scene(self, scene):
        """Follow another graph (a different tab)."""
        if self.scene is not None and isValid(self.scene):
            self.scene.selectionChanged.disconnect(self._on_selection_changed)
            self.scene.node_renamed.disconnect(self._on_node_renamed)
            self.scene.node_changed.disconnect(self._on_node_changed)
        self.scene = scene
        if scene is None:
            self.set_node(None)
            return
        scene.selectionChanged.connect(self._on_selection_changed)
        scene.node_renamed.connect(self._on_node_renamed)
        scene.node_changed.connect(self._on_node_changed)
        self.node = False  # force a rebuild for the new scene's selection
        self._on_selection_changed()

    def sizeHint(self):
        return QSize(340, 600)

    # ---- scene events ----------------------------------------------------
    def _on_selection_changed(self):
        from .node import Node
        if not isValid(self.scene):  # scene clears its selection while being destroyed on exit
            return
        nodes = [i for i in self.scene.selectedItems() if isinstance(i, Node)]
        node = nodes[0] if nodes else None
        if node is not self.node:
            self.set_node(node)

    def _on_node_renamed(self, node):
        if node is self.node and self.name_edit:
            self.name_edit.setText(node.name)
        elif self.node and self.body is not None:  # the shown node may list it (e.g. a Merge's inputs)
            rows = self.node.info_rows()
            if rows != self._info_rows:
                self._build_info(rows)

    def _on_node_changed(self, node):
        """A parameter changed: update values the node set itself, visibility and read-only info."""
        if node is not self.node or self.body is None:
            return
        if set(node.links) != self._links_shown:  # linked / unlinked (Reset, Break Connection, source deleted):
            self.set_node(node)                   # rebuild, so fields gain or lose the greyed, locked look now
            return
        if [p.name for p in node.parameters] != self._param_names:  # its fields changed (a service model's inputs
            self.set_node(node)                                      # arrived, another model was picked)
            return
        for name, sync in self.param_sync.items():
            sync(node.params[name])
        for field in self.spell_fields:
            field.language_changed()
        for w in self.live_widgets:  # widgets that follow several of the node's values (e.g. the crop preview)
            w.refresh(node)
        if self._geo is not None and self._geo_used and self._geo.node is node:
            self.viewer_area.setVisible(bool(self._geo_param.visible(node)))
        for param, widgets in self.param_rows:
            show = bool(param.visible(node))
            for w in widgets:
                want = show and not getattr(w, "folded_away", False)
                if w.isVisibleTo(self.body) != want:
                    w.setVisible(want)
        for param, widgets in getattr(self, "_enabled_rows", []):
            on = bool(param.enabled(node))
            for w in widgets:
                if w.isEnabled() != on:
                    w.setEnabled(on)
        self._stretch_text_rows()
        rows = node.info_rows()
        if rows != self._info_rows:
            self._build_info(rows)

    # ---- building --------------------------------------------------------
    def set_node(self, node):
        """Show a node's parameters. Building them mustn't take the keyboard from the graph: Qt's video widget
        grabs the focus when it is shown (a selected Video node then ate Del / Tab) - it goes back where it was."""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        before = QApplication.focusWidget()
        self._build_node(node)
        if self._geo is not None and not self._geo_used:  # nothing to view: the viewer's scene goes
            self._geo.set_node(None)
            self._geo_param = None
        if before is not None and not self.isAncestorOf(before):
            QTimer.singleShot(0, lambda: self._give_back_focus(before))

    def _give_back_focus(self, before):
        from PySide6.QtWidgets import QApplication
        now = QApplication.focusWidget()
        if isValid(before) and before.isVisible() and now is not None and self.isAncestorOf(now):
            before.setFocus()

    def _build_node(self, node):
        for w in self.live_widgets:  # the old pane's (the 3D viewer is kept: it only sleeps - see __init__)
            if isValid(w) and hasattr(w, "release") and not hasattr(w, "sleep"):
                w.release()
        if self._geo is not None:
            self._geo.sleep()
        self._geo_used = False
        self.viewer_area.hide()
        self.node = node
        self.name_edit = None
        self.param_rows, self.param_sync, self.previews = [], {}, []
        self.spell_fields = []  # text fields checked in the node's "spell" language
        self.live_widgets = []
        self._links_shown = set(getattr(node, "links", {}) or {})  # which fields are drawn as linked
        self._param_names = [p.name for p in node.parameters] if node is not None else []
        self._info_rows = None
        old = self.scroll.takeWidget()
        if old:
            old.deleteLater()
        self.body = QWidget(objectName="Body")
        outer = QVBoxLayout(self.body)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        if node is None:
            slot = self.head_slot.layout()
            while slot.count():
                item = slot.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            empty = QLabel("Select a node to see its parameters", objectName="Empty")
            empty.setAlignment(Qt.AlignCenter)
            empty.setWordWrap(True)
            outer.addWidget(empty)
            outer.addStretch()
            self.scroll.setWidget(self.body)
            return

        # Header: node type + editable name (like Houdini's parameter pane).
        header = QWidget(objectName="Header")
        h = QHBoxLayout(header)
        h.setContentsMargins(10, 6, 10, 6)
        h.addWidget(QLabel(node.label, objectName="NodeType"))
        self.name_edit = QLineEdit(node.name, objectName="NameEdit")
        # this field renames THIS node with ITS text: it can finish editing (lose focus) after the pane has moved on to
        # another node - reading self.name_edit then renamed the old node to the new one's name
        edit = self.name_edit
        edit.editingFinished.connect(lambda: isValid(edit) and edit.text() != node.name
                                     and self.scene.rename_node(node, edit.text()))
        h.addWidget(self.name_edit, 1)
        slot = self.head_slot.layout()
        while slot.count():
            item = slot.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        slot.addWidget(header)

        content = QWidget()
        grid = self._grid = QGridLayout(content)
        self._text_rows = []  # (param, grid row, box state) of the text boxes - automatic ones take the spare height
        grid.setContentsMargins(10, 8, 10, 12)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        row = 0

        def add(label, widget):
            nonlocal row
            lbl = QLabel(label)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(lbl, row, 0)
            grid.addWidget(widget, row, 1)
            row += 1
            return [lbl, widget]

        def add_wide(widget):
            nonlocal row
            grid.addWidget(widget, row, 0, 1, 2)
            row += 1
            return [widget]

        self._enabled_rows = []  # (param, widgets) greyed out while param.enabled(node) is False
        row_box = None           # (row name, layout) of the line ticks / icons are being added to

        for param in node.parameters:
            k = param.kind
            if param.row and k in ("bool", "icon"):  # ticks and icon buttons sharing one line
                if row_box is None or row_box[0] != param.row:
                    line = QWidget()
                    flow = FlowLayout()  # wraps onto a second line when the pane is narrow
                    line.setLayout(flow)
                    add_wide(line)
                    row_box = (param.row, flow)
                w = self._make_row_item(node, param)
                row_box[1].addWidget(w)
                widgets = [w]
            elif k in ("image", "file", "folder", "audio", "video"):
                field, preview = self._make_file_widgets(node, param, content, k == "image")
                widgets = add(param.label, field)
                if preview:
                    add_wide(preview)  # visibility follows whether there is an image
                if k == "video":
                    player = VideoPlayer(content)
                    player.set_path(node.params[param.name])
                    field_sync = self.param_sync[param.name]
                    self.param_sync[param.name] = lambda v, f=field_sync, p=player: (f(v), p.set_path(v))
                    widgets += add_wide(player)
                if k == "audio":
                    player = AudioPlayer()
                    player.set_path(node.params[param.name])
                    field_sync = self.param_sync[param.name]
                    self.param_sync[param.name] = lambda v, f=field_sync, p=player: (f(v), p.set_path(v))
                    widgets += add_wide(player)  # the waveform uses the full width
            elif k == "text":
                spell = (lambda: node.params.get("spell") or "") if "spell" in node.params else None
                edit = TextField(node.params[param.name], language=spell)
                if spell:
                    self.spell_fields.append(edit)
                # textChanged also fires when only the formatting changes (spell-check underlines): write back
                # real edits only; and follow values set from elsewhere
                edit.textChanged.connect(lambda e=edit, p=param: e.toPlainText() != node.params[p.name]
                                         and self._set(node, p, e.toPlainText(), typing=e))
                self.param_sync[param.name] = lambda v, e=edit: e.toPlainText() != (v or "") and e.setPlainText(v or "")
                if param.submit:
                    edit.on_submit = lambda p=param: self._act(node, p.submit, p.label)
                if param.placeholder:
                    edit.setPlaceholderText(param.placeholder() if callable(param.placeholder) else param.placeholder)
                widgets = self._box(node, param, edit, add_wide)
            elif k == "data":  # kept by the node (e.g. drawn strokes), not edited as a field
                widgets = []
            elif k == "color":  # a colour swatch; click to choose
                widgets = add(param.label, self._make_color(node, param))
            elif k == "toolbar":  # a row of icon buttons; options = [(action, icon, tooltip)], "mode:x" toggles
                widgets = add_wide(self._make_toolbar(node, param))
            elif k == "lock":  # a lock icon in the label column, beside the two rows that follow it
                button = QToolButton(objectName="Lock", checkable=True, toolTip=param.label)
                from .icons import icon
                button.setChecked(bool(node.params[param.name]))
                button.setIcon(icon("lock" if button.isChecked() else "unlock"))
                button.setAutoRaise(True)
                button.toggled.connect(lambda on, b=button, p=param: (b.setIcon(icon("lock" if on else "unlock")),
                                                                       self._set(node, p, on)))
                grid.addWidget(button, row, 0, 2, 1, Qt.AlignLeft | Qt.AlignVCenter)
                self.param_sync[param.name] = lambda v, b=button: (b.blockSignals(True), b.setChecked(bool(v)),
                                                                   b.setIcon(icon("lock" if v else "unlock")),
                                                                   b.blockSignals(False))
                widgets = [button]
            elif k == "media_picture":  # the Edit Media node's picture (with the crop box in crop mode)
                from .edit_media_picture import MediaPicture
                view = MediaPicture(node, content)
                self.live_widgets.append(view)
                widgets = add_wide(view)
            elif k == "geometry_view":  # a 3D file in a viewer (Geometry node; Isolate showing a 3D item)
                view = self._viewer_page(node, param)
                self.live_widgets.append(view)
                widgets = []  # (not in the scrolling rows: its page sits in the fixed viewer area)
            elif k == "curve_editor":  # the Edit Geometry node's keys (curve_editor.py), its height: the grip under it
                from .curve_editor import CurveEditor
                view = CurveEditor(node, lambda name, value: self._set(
                    node, next(p for p in node.parameters if p.name == name), value), content)
                self.live_widgets.append(view)
                widgets = self._box(node, param, view, add_wide)
            elif k == "media_timeline":  # the Edit Media node's timeline for a video / sound, with the trim buttons
                from .edit_media_timeline import MediaTimeline
                view = MediaTimeline(node, content)
                self.live_widgets.append(view)
                widgets = add_wide(view)
            elif k == "button":
                button = QPushButton(param.label, objectName="Action")
                button.clicked.connect(lambda _=False, p=param: self._act(node, p.name, p.label))
                widgets = add_wide(button)
            elif k == "image_view":
                preview = ImagePreview(content)
                preview.set_path(node.params[param.name])
                self.previews.append(preview)
                holder = QWidget()  # shown/hidden with the parameter; preview hides itself when empty
                v = QVBoxLayout(holder)
                v.setContentsMargins(0, 0, 0, 0)
                v.addWidget(preview)
                self.param_sync[param.name] = preview.set_path
                widgets = add_wide(holder)
            elif k == "text_view":
                view = TextField(node.params[param.name], read_only=True)
                view.setPlaceholderText("The output text appears here")
                self.param_sync[param.name] = lambda v, w=view: w.toPlainText() != (v or "") and w.setPlainText(v or "")
                widgets = self._box(node, param, view, add_wide)  # results get the spare height too
            elif k == "log_view":  # a running log: read-only, follows its newest line
                view = TextField(node.params[param.name], read_only=True)
                if param.placeholder:
                    view.setPlaceholderText(param.placeholder)

                def follow(v, w=view):
                    if w.toPlainText() != (v or ""):
                        w.setPlainText(v or "")
                        w.verticalScrollBar().setValue(w.verticalScrollBar().maximum())
                follow(node.params[param.name])
                self.param_sync[param.name] = follow
                widgets = self._box(node, param, view, add_wide)
            elif k == "chat_view":  # a conversation the node reads itself (e.g. the Executor's, from its transcript)
                view = TextField(node.chat_text(), read_only=True)
                if param.placeholder:
                    view.setPlaceholderText(param.placeholder)

                def follow_chat(_v, w=view, n=node):
                    text = n.chat_text()
                    if w.toPlainText() != text:
                        at_end = w.verticalScrollBar().value() >= w.verticalScrollBar().maximum() - 4
                        w.setPlainText(text)
                        if at_end:  # keep following the newest message unless the user scrolled up to read
                            w.verticalScrollBar().setValue(w.verticalScrollBar().maximum())
                view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())
                self.param_sync[param.name] = follow_chat
                widgets = self._box(node, param, view, add_wide)
            elif k == "audio_view":
                player = AudioPlayer()
                player.set_path(node.params[param.name])
                self.param_sync[param.name] = player.set_path
                widgets = add_wide(QLabel(param.label, objectName="TextLabel")) + add_wide(player)
            elif k == "video_view":
                player = VideoPlayer(content)
                player.set_path(node.params[param.name])
                self.param_sync[param.name] = player.set_path
                widgets = add_wide(player)
            elif k == "data_view":  # a JSON result: a video's shots as timeline + list, anything else as text
                from .shots_view import ShotsView
                view = ShotsView(content)
                view.set_path(node.params[param.name])
                self.param_sync[param.name] = view.set_path
                widgets = add_wide(view)
            else:
                widgets = add(param.label, self._make_widget(node, param))
            if not (param.row and k in ("bool", "icon")):
                row_box = None
            self.param_rows.append((param, widgets))
            self._wire_links(node, param, widgets)
            for w in widgets:
                w.setVisible(bool(param.visible(node)) and not getattr(w, "folded_away", False))
                if param.hint and not w.toolTip():
                    w.setToolTip(param.hint)
            if param.enabled is not None:
                self._enabled_rows.append((param, widgets))
                for w in widgets:
                    w.setEnabled(bool(param.enabled(node)))

        # Read-only information (what a model was detected as, a Modality's status...).
        self.info_box = QWidget()
        self.info_grid = QGridLayout(self.info_box)
        self.info_grid.setContentsMargins(0, 6, 0, 0)
        self.info_grid.setHorizontalSpacing(8)
        self.info_grid.setVerticalSpacing(3)
        self.info_grid.setColumnStretch(1, 1)
        add_wide(self.info_box)
        self._build_info(node.info_rows())

        outer.addWidget(content, 1)
        self._outer, self._content = outer, content
        outer.addStretch()
        self._stretch_text_rows()
        self.scroll.setWidget(self.body)

    def _viewer_page(self, node, param):
        """THE 3D viewer (see __init__), shown for this node: made once (header line, the view, a grip that sets its
        height - double-click = its default; fold / height remembered per node type, as for the other boxes), then
        switched from node to node; its header line ("▾ View" + the view's own toggles) rebuilt for each."""
        from .geometry_view import GeometryView
        from .settings import pane_box, set_pane_box
        if self._geo is None:
            page = QWidget()
            v = QVBoxLayout(page)
            v.setContentsMargins(0, 0, 0, 0)
            v.setSpacing(2)
            self._geo_line = QWidget()
            QHBoxLayout(self._geo_line).setContentsMargins(0, 0, 0, 0)
            self._geo_line.layout().setSpacing(12)
            view = GeometryView(node, param, page)
            self._geo_box = {"key": "", "folded": False, "h": None, "default": view.default_height}

            def height(h):
                self._geo_box["h"] = h
                set_pane_box(self._geo_box["key"], {"h": h})
                self._geo_apply()
            self._geo_grip = BoxGrip(view, height)
            v.addWidget(self._geo_line)
            v.addWidget(view)
            v.addWidget(self._geo_grip)
            self._viewer_layout.addWidget(page)
            self._geo = view
        else:
            self._geo.set_node(node, param)
        view = self._geo
        self._geo_param, self._geo_used = param, True
        key = f"{node.type_name}/{param.name}"
        state = pane_box(key)
        self._geo_box.update({"key": key, "folded": bool(state.get("folded")), "h": state.get("h")})

        def fold(on):
            self._geo_box["folded"] = on
            set_pane_box(self._geo_box["key"], {"folded": on})
            self._geo_apply()
        row = self._geo_line.layout()
        while row.count():
            item = row.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        header = BoxHeader(param.label, self._geo_box["folded"], fold)
        if param.hint:
            header.setToolTip(param.hint + " \u00b7 click to fold")
        row.addWidget(header)
        for w in view.header_widgets(lambda name, value, n=node: self._set(
                n, next(p for p in n.parameters if p.name == name), value)):
            row.addWidget(w)
        row.addStretch(1)
        self._geo_apply()
        self.viewer_area.setVisible(bool(param.visible(node)))
        return view

    def _geo_apply(self):
        box = self._geo_box
        self._geo.setVisible(not box["folded"])
        self._geo_grip.setVisible(not box["folded"])
        self._geo.setFixedHeight(int(box["h"] or box["default"]))

    def _box(self, node, param, view, add_wide):
        """A multi-line field as a box: a header that folds it, the text, and a grip under it that sets its height
        (double-click: its default again - param.lines, else automatic). Folded / height are remembered per node
        type and box."""
        from .settings import pane_box, set_pane_box
        key = f"{node.type_name}/{param.name}"
        state = pane_box(key)
        default = getattr(view, "default_height", None)  # e.g. the 3D viewer's
        if param.lines:  # a box that starts small (e.g. where you type to the brain), resizable from there
            margins = view.contentsMargins()
            default = round(view.fontMetrics().lineSpacing() * param.lines + 2 * view.document().documentMargin()
                            + margins.top() + margins.bottom())
        box = {"folded": bool(state.get("folded")), "h": state.get("h"), "default": default}
        grip = None

        def apply():
            view.folded_away = grip.folded_away = box["folded"]
            show = bool(param.visible(node)) and not box["folded"]
            view.setVisible(show)
            grip.setVisible(show)
            if box["h"] or box["default"]:
                view.setFixedHeight(int(box["h"] or box["default"]))
            else:
                view.setMinimumHeight(50)
                view.setMaximumHeight(16777215)

        def fold(on):
            box["folded"] = on
            set_pane_box(key, {"folded": on})
            apply()
            self._stretch_text_rows()

        def height(h):
            box["h"] = h
            set_pane_box(key, {"h": h})
            apply()
            self._stretch_text_rows()

        header = BoxHeader(param.label, box["folded"], fold)
        if param.hint:
            header.setToolTip(param.hint + " \u00b7 click to fold")
        grip = BoxGrip(view, height)
        extra = view.header_widgets(lambda name, value: self._set(
            node, next(p for p in node.parameters if p.name == name), value)) if hasattr(view, "header_widgets") else []
        if extra:  # the view's own controls on the header line (e.g. the 3D viewer's Light / Options)
            line = QWidget()
            row = QHBoxLayout(line)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(12)
            row.addWidget(header)
            for w in extra:
                row.addWidget(w)
            row.addStretch(1)
            header = line
        widgets = add_wide(header) + add_wide(view)
        view_row = self._grid.getItemPosition(self._grid.indexOf(view))[0]
        widgets += add_wide(grip)
        apply()
        self._text_rows.append((param, view_row, box))
        return widgets

    def _stretch_text_rows(self):
        """Visible text boxes of automatic height grow to fill the pane (what follows them sits at the bottom);
        folded boxes and boxes given a height keep theirs; with none growing, the parameters stay at the top."""
        shown = [row for param, row, box in self._text_rows
                 if param.visible(self.node) and not box["folded"] and not (box["h"] or box["default"])]
        for _param, row, _box in self._text_rows:
            self._grid.setRowStretch(row, 1 if row in shown else 0)
        # by position: [content, the stretch below it] (positions were off by one after the name moved out of this
        # layout - with no box growing, the spare height then went into the rows: the rulers grew huge)
        i = self._outer.indexOf(self._content)
        self._outer.setStretch(i, 1 if shown else 0)
        self._outer.setStretch(i + 1, 0 if shown else 1)

    def _build_info(self, rows):
        self._info_rows = rows
        while self.info_grid.count():
            item = self.info_grid.takeAt(0)
            if item.widget():
                item.widget().hide()  # gone at once, not only when the deletion comes round
                item.widget().deleteLater()
        self.info_box.setVisible(bool(rows))
        if not rows:
            return
        _fill_info(self.info_grid, 0, getattr(self.node, "info_title", "Info"), rows)

    def show_info(self, kind, name, sections):
        """Show read-only facts instead of a node (e.g. a model picked in the Library).
        sections: [(title, [(label, value)])]; a value may be ('link', text, url)."""
        self.set_node(None)
        body = self.body
        outer = body.layout()
        while outer.count():  # drop the "select a node" placeholder
            item = outer.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        header = QWidget(objectName="Header")
        h = QHBoxLayout(header)
        h.setContentsMargins(10, 6, 10, 6)
        h.addWidget(QLabel(kind, objectName="NodeType"))
        title = QLabel(_wrappable(name), objectName="InfoValue")
        title.setWordWrap(True)
        title.setMinimumWidth(1)
        title.setStyleSheet("font-weight: bold;")
        title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        h.addWidget(title, 1)
        outer.addWidget(header)
        content = QWidget()
        grid = QGridLayout(content)
        grid.setContentsMargins(10, 2, 10, 12)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(3)
        grid.setColumnStretch(1, 1)
        row = 0
        for section_title, rows in sections:
            row = _fill_info(grid, row, section_title, rows)
        outer.addWidget(content)
        outer.addStretch()

    def _make_file_widgets(self, node, param, parent, with_preview):
        preview = ImagePreview(parent) if with_preview else None
        if preview:
            self.previews.append(preview)
        folder = param.kind == "folder"
        exts = _filter_extensions(param.filter or ("" if folder else IMAGE_FILTER))

        def accept(path):  # what a drop may put here
            if os.path.isdir(path):
                return folder or (param.accept_folders and any(f.lower().endswith(exts) for f in os.listdir(path)))
            return not folder and (not exts or path.lower().endswith(exts))

        edit = PathEdit(node.params[param.name], accept,
                        placeholderText="Folder" if folder else "Path to an image file" if with_preview
                        else "Path to an audio file" if param.kind == "audio"
                        else "Path to a video file" if param.kind == "video"
                        else "Weight file or folder (drop from the Library)" if param.accept_folders
                        else "Path to a file")
        browse = QToolButton(text="…", objectName="Browse", toolTip="Browse")

        def changed(text):
            path = clean_path(text)
            if path != node.params[param.name]:
                self._set(node, param, path, typing=edit)
            if preview:
                preview.set_path(text)

        def pick():
            current = clean_path(edit.text())
            if folder:
                path = QFileDialog.getExistingDirectory(self, f"Choose {param.label.lower()}", current)
            else:
                path, _ = QFileDialog.getOpenFileName(self, f"Choose {param.label.lower()}",
                                                      os.path.dirname(current), param.filter or IMAGE_FILTER)
            if path:
                edit.setText(os.path.normpath(path))

        edit.textChanged.connect(changed)
        edit.editingFinished.connect(lambda: edit.setText(clean_path(edit.text())))
        browse.clicked.connect(pick)
        if preview:
            preview.set_path(edit.text())

        def sync(v):  # value set from elsewhere (code, a loaded scene...)
            if clean_path(edit.text()) != (v or ""):
                edit.setText(v or "")
        self.param_sync[param.name] = sync

        edit.setMinimumWidth(40)  # long paths scroll inside the field instead of widening the pane
        if param.placeholder:
            text = param.placeholder() if callable(param.placeholder) else param.placeholder
            edit.setPlaceholderText(text)
            edit.setToolTip(text)
        field = QWidget()
        h = QHBoxLayout(field)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        h.addWidget(edit, 1)
        h.addWidget(browse)
        return field, preview

    # ---- parameter links (Houdini's channel references: Copy Parameter / Paste Parameter) ----------------------
    def _wire_links(self, node, param, widgets):
        """Right-click menu on the parameter's label and field; a linked parameter is greyed and locked, and a
        click on its label shows what it follows."""
        from . import links
        if param.kind not in links.LINKABLE:
            return
        label = widgets[0] if len(widgets) > 1 and isinstance(widgets[0], QLabel) else None
        values = [w for w in widgets if w is not label]
        linked = param.name in node.links

        def add_actions(menu):
            """Copy Parameter next to the menu's own Copy, the two pastes next to its Paste (text fields have
            those); then Break Connection / Revert to Default."""
            from PySide6.QtGui import QAction
            ok, why = links.can_paste(node, param.name)
            src = links.clipboard
            what = f" ({src[0].name} › {links.param_of(src[0], src[1]).label})" if ok else ""

            def action(text, slot, enabled=True):
                act = QAction(text, menu)
                act.triggered.connect(slot)
                act.setEnabled(enabled)
                return act
            def recorded(text, fn):
                with undo.record(node, f"{text} ({param.label})"):
                    fn()
            copy_group = [action("Copy Parameter", lambda: links.copy_param(node, param.name))]
            paste_group = [action("Paste Parameter - linked" + what,
                                  lambda: (recorded("Link", lambda: links.paste_link(node, param.name)),
                                           self.set_node(node)), ok),
                           action("Paste Value" + what,
                                  lambda: recorded("Paste Value", lambda: links.paste_value(node, param.name)),
                                  ok and param.name not in node.links)]
            if not ok and src is not None:
                paste_group.append(action(f"(can't paste: {why})", lambda: None, False))
            existing = menu.actions()

            def place(group, anchor_name):
                anchor = next((a for a in existing if a.objectName() == anchor_name), None)
                if anchor is None:
                    menu.addActions(group)
                    return
                after = existing[existing.index(anchor) + 1] if existing.index(anchor) + 1 < len(existing) else None
                for act in group:
                    menu.insertAction(after, act) if after else menu.addAction(act)
            if not existing:
                menu.addActions(copy_group + paste_group)
            else:
                place(copy_group, "edit-copy")
                place(paste_group, "edit-paste")
            menu.addSeparator()
            if param.name in node.links:
                menu.addAction(f"Break Connection ({links.describe(node, param.name)})",
                               lambda: (recorded("Break Connection", lambda: links.break_link(node, param.name)),
                                        self.set_node(node)))
            else:
                r = menu.addAction("Revert to Default",
                                   lambda: recorded("Revert", lambda: node.set_param(param.name, param.default)))
                r.setEnabled(node.params[param.name] != param.default)

        def build_menu(widget):
            base = widget.createStandardContextMenu() if isinstance(widget, QLineEdit) else QMenu(self)
            add_actions(base)
            return base

        def menu_at(widget, pos):
            build_menu(widget).exec(widget.mapToGlobal(pos))

        targets = []
        for w in values:
            targets += [w] + w.findChildren(QWidget)
        for w in targets + ([label] if label else []):
            if isinstance(w, TextField):
                w.extra_menu = add_actions
            elif isinstance(w, (QLineEdit, QLabel, QComboBox, QCheckBox)) or hasattr(w, "set_locked") \
                    or type(w).__name__ == "Ruler":
                w.setContextMenuPolicy(Qt.CustomContextMenu)
                w.customContextMenuRequested.connect(lambda pos, ww=w: menu_at(ww, pos))
                w.param_menu = lambda ww=w: build_menu(ww)  # the same menu, built without showing it
        if not linked:
            return
        # linked: greyed and locked; the label shows what it follows when clicked
        for w in targets:
            if hasattr(w, "set_locked"):
                w.set_locked(True)
            elif isinstance(w, (QLineEdit, QPlainTextEdit)):
                w.setReadOnly(True)
                w.setProperty("linked", True)
                w.style().unpolish(w)
                w.style().polish(w)
            elif isinstance(w, (QComboBox, QCheckBox, QToolButton, QPushButton)):
                w.setEnabled(False)
        if label is not None:
            source = links.describe(node, param.name)
            label.setToolTip(f"Follows {source} - click to show; right-click > Break Connection to unlink")
            label.setProperty("linked", True)
            label.style().unpolish(label)
            label.style().polish(label)
            plain = label.text()

            def toggle(event, lbl=label, plain=plain, source=source):
                if event.button() == Qt.LeftButton:
                    lbl.setText(source if lbl.text() == plain else plain)
            label.mousePressEvent = toggle

    # ---- user edits: each one an undo step ---------------------------------------------------------------------
    def _set(self, node, param, value, typing=None, text=None):
        merge = undo.typing_key(typing, node, param.name) if typing is not None else None
        with undo.record(node, text or param.label, merge):
            node.set_param(param.name, value)

    def _act(self, node, name, text):
        with undo.record(node, text):
            node.action(name)

    def _make_row_item(self, node, param):
        """A tick with its label beside it, or an icon button - for parameters sharing one line."""
        if param.kind == "icon":
            from .icons import icon
            b = QToolButton(objectName="Tool", toolTip=param.hint or param.label)
            b.setIcon(icon(param.default))  # an icon parameter's value is its icon's name
            b.setIconSize(QSize(18, 18))
            b.clicked.connect(lambda _=False: self._act(node, param.name, param.label))
            return b
        w = QCheckBox(param.label, checked=bool(node.params[param.name]))
        w.toggled.connect(lambda on: self._set(node, param, on))

        def sync(v):
            w.blockSignals(True)
            w.setChecked(bool(v))
            w.blockSignals(False)
        self.param_sync[param.name] = sync
        return w

    def _make_color(self, node, param):
        from PySide6.QtWidgets import QColorDialog
        from PySide6.QtGui import QColor as _QColor
        b = QToolButton(objectName="Swatch", toolTip="Choose the colour")
        b.setMinimumWidth(60)

        def show(v):
            b.setStyleSheet(f"QToolButton#Swatch {{ background: {v}; border: 1px solid #111; border-radius: 2px;"
                            f" min-height: 18px; }}")

        def pick():
            c = QColorDialog.getColor(_QColor(node.params[param.name]), self, param.label)
            if c.isValid():
                self._set(node, param, c.name())
        b.clicked.connect(pick)
        show(node.params[param.name])
        self.param_sync[param.name] = show
        return b

    def _make_toolbar(self, node, param):
        from .icons import icon
        bar = QWidget()
        h = QHBoxLayout(bar)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(3)
        buttons = {}
        for action, kind, tip in param.options:
            b = QToolButton(objectName="Tool", toolTip=tip)
            b.setIcon(icon(kind))
            b.setIconSize(QSize(18, 18))
            if action.startswith("mode:"):  # the toolbar's value: one mode at a time (crop / draw), or none
                mode = action[5:]
                b.setCheckable(True)
                b.setChecked(node.params[param.name] == mode)
                b.toggled.connect(lambda on, m=mode, t=tip: self._set(node, param, m, text=t) if on else
                                  (node.params[param.name] == m and self._set(node, param, "", text=t)))
            else:
                b.clicked.connect(lambda _=False, a=action, t=tip: self._act(node, a, t))
            h.addWidget(b)
            buttons[action] = b
        h.addStretch(1)
        modes = {a[5:]: b for a, b in buttons.items() if a.startswith("mode:")}
        if modes:
            def sync(v, modes=modes):
                for m, b in modes.items():
                    b.blockSignals(True)
                    b.setChecked(v == m)
                    b.blockSignals(False)
            self.param_sync[param.name] = sync
        return bar

    def _make_widget(self, node, param):
        value = node.params[param.name]

        def setter(v, typing=False):  # typing (spin boxes, text fields): one undo step per visit to the field
            self._set(node, param, v, typing=w if typing else None)

        if param.kind in ("int", "float") and param.step:
            from .ruler import RulerField
            w = RulerField(value, param.kind, param.step, param.minimum, param.maximum, param.default)
            w.committed.connect(setter)
            self.param_sync[param.name] = w.set_value
            return w
        if param.kind == "float":
            w = QDoubleSpinBox(decimals=3, minimum=-1e9, maximum=1e9, value=value)
            w.valueChanged.connect(lambda v: setter(v, True))
        elif param.kind == "int":
            w = QSpinBox(minimum=-10**9, maximum=10**9, value=value)
            w.valueChanged.connect(lambda v: setter(v, True))
        elif param.kind == "bool":
            w = QCheckBox(checked=value)
            w.toggled.connect(setter)
        elif param.kind == "choice":
            w = QComboBox()
            w.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)  # long options don't widen the pane
            w.setMinimumContentsLength(6)

            def fill(v, w=w):
                """The options as the parameter lists them now (some arrive later, e.g. a service's model list);
                a saved value that isn't among them is kept, marked."""
                items = param.choices(node)
                if v not in [o for o, _l in items] and v not in (None, ""):
                    items.append((v, f"{v} (saved)"))
                if [(w.itemData(i), w.itemText(i)) for i in range(w.count())] != items:
                    w.blockSignals(True)
                    w.clear()
                    for opt_value, opt_label in items:
                        w.addItem(opt_label, opt_value)
                    w.blockSignals(False)
                w.blockSignals(True)
                w.setCurrentIndex(max(0, w.findData(v)))
                w.blockSignals(False)
            fill(value)
            w.fill = fill
            w.currentIndexChanged.connect(lambda i: setter(w.itemData(i)))
        else:
            w = QLineEdit(str(value))
            if param.placeholder:
                w.setPlaceholderText(param.placeholder() if callable(param.placeholder) else param.placeholder)
            w.textChanged.connect(lambda v: setter(v, True))

        def sync(v):  # the node changed this value itself (e.g. recommended steps)
            w.blockSignals(True)
            if isinstance(w, (QSpinBox, QDoubleSpinBox)):
                w.setValue(v)
            elif isinstance(w, QCheckBox):
                w.setChecked(v)
            elif isinstance(w, QComboBox):
                w.fill(v) if hasattr(w, "fill") else w.setCurrentIndex(max(0, w.findData(v)))
            elif w.text() != str(v):
                w.setText(str(v))
            w.blockSignals(False)
        self.param_sync[param.name] = sync
        return w
