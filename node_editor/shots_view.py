"""A data result shown the way it reads best: a video's shots (a .shots file = JSON, engine/shots.py) or chunks (a
.chunks file, chunks.py) as a timeline and a list.

Shots view:
  - the source video (the file the .shots names) with the shot playing in a loop,
  - a strip under it: the whole video, each shot tinted (alternating shades, the chosen one bright), transition
    frames hatched, the playhead; click = play the shot (or transition) there,
  - prev / play / next shot + what is playing ("#3 · 132–164 · 1.38 s · hard cut"),
  - the list of shots with their first frames (made by ffmpeg in the background, kept in the instances folder).
Chunks look the same: each chunk is a span ("#12 · shot 10 (2/5) · 401–474 · 3.09 s"), shot boundaries drawn
brighter than chunk boundaries. Any other JSON is shown as text.
"""

import hashlib
import json
import math
import os
import shutil

from PySide6.QtCore import QProcess, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

from . import media_cache

SHOT_A, SHOT_B = QColor(70, 110, 150), QColor(55, 85, 115)
CHOSEN = QColor(120, 185, 250)
TRANSITION = QColor(215, 165, 70)
PLAYHEAD = QColor(255, 255, 255)
THUMB_H = 54
_chosen_before = {}  # file path -> ("span" | "transition", index): the pane comes back where it was (it is rebuilt
                     # every time its node is selected)


def load(path):
    """The JSON in `path` (None if it isn't readable JSON)."""
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def is_shots(data):
    """A shots JSON ("format": "shots"; files from before the format line are known by their content)."""
    return (isinstance(data, dict) and data.get("format", "shots") == "shots" and isinstance(data.get("shots"), list)
            and "fps_value" in data)


def is_chunks(data):
    return (isinstance(data, dict) and data.get("format", "chunks") == "chunks" and isinstance(data.get("chunks"), list)
            and "fps_value" in data)


def spans_of(data):
    """The spans a .shots / .chunks file lists (shots or chunks), or None for other data."""
    return data["chunks"] if is_chunks(data) else data["shots"] if is_shots(data) else None


def describe(span):
    """'#3 · 132–164 · 1.38 s · hard cut' for a shot; 'wipes · 237–245 · 0.38 s' for a transition."""
    secs = f"{span['frames'] / span['_fps']:.2f} s"
    if "chunk" in span:
        part = f" ({span['part']}/{span['of']})" if span.get("of", 1) > 1 else ""
        return f"#{span['chunk']} · shot {span['shot']}{part} · {span['first']}–{span['last']} · {secs}"
    if "shot" in span:
        begins = {"video_start": "start", "after_transition": f"after {span.get('transition') or 'a transition'}"}
        how = begins.get(span.get("begins"), (span.get("begins") or "").replace("_", " "))
        return f"#{span['shot']} · {span['first']}–{span['last']} · {secs} · {how}"
    return f"{span['type']} · {span['first']}–{span['last']} · {secs}"


class ShotStrip(QWidget):
    """The whole video as one bar: shots tinted, transitions hatched, the chosen span bright, the playhead."""

    def __init__(self, view):
        super().__init__()
        self.view = view
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setMinimumHeight(22)

    def _x(self, frame):  # left edge of a 1-based frame
        r = self.rect().adjusted(1, 1, -1, -1)
        return r.left() + (frame - 1) / max(1, self.view.frames) * r.width()

    def _frame_at(self, x):
        r = self.rect().adjusted(1, 1, -1, -1)
        return max(1, min(self.view.frames, math.floor((x - r.left()) / max(1, r.width()) * self.view.frames) + 1))

    def mousePressEvent(self, e):
        if self.view.frames and e.button() == Qt.LeftButton:
            span = self.view.span_at(self._frame_at(e.position().x()))
            if span:
                self.view.play_span(span)

    def mouseMoveEvent(self, e):
        if self.view.frames:
            span = self.view.span_at(self._frame_at(e.position().x()))
            self.setToolTip(describe(span) if span else "")

    def paintEvent(self, _):
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(68, 68, 68)))
        p.setBrush(QColor(30, 30, 30))
        p.drawRect(r)
        v = self.view
        if not v.frames:
            return
        top, h = r.top() + 1, r.height() - 2
        for i, s in enumerate(v.shots):
            box = QRectF(self._x(s["first"]), top, self._x(s["last"] + 1) - self._x(s["first"]), h)
            p.fillRect(box, CHOSEN if s is v.chosen else (SHOT_A if i % 2 == 0 else SHOT_B))
        for t in v.transitions:
            box = QRectF(self._x(t["first"]), top, max(2.0, self._x(t["last"] + 1) - self._x(t["first"])), h)
            p.fillRect(box, QColor(40, 34, 20))
            p.fillRect(box, QBrush(CHOSEN if t is v.chosen else TRANSITION, Qt.BDiagPattern))
        for s in v.shots[1:]:  # thin cut lines, so neighbouring short spans stay apart; a new shot brighter
            new_shot = s.get("part", 1) == 1
            p.setPen(QPen(QColor(230, 230, 230) if new_shot and "chunk" in s else QColor(20, 20, 20), 1))
            x = self._x(s["first"])
            p.drawLine(x, top, x, top + h)
        if v.frame:
            x = self._x(v.frame) + 0.5 * (self._x(2) - self._x(1))
            p.setPen(QPen(PLAYHEAD, 1.5))
            p.drawLine(x, r.top(), x, r.bottom())


class FrameScreen(QWidget):
    """Shows the frames it is given, scaled to fit. The shots view hands it only frames INSIDE the chosen span: Qt's
    own video widget shows every frame it decodes - the one before the span right after a seek, and one past its
    end before the loop jumps back - which looked like shots bleeding into each other."""

    def __init__(self):
        super().__init__()
        self.image = None
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(60)

    def set_image(self, image):
        self.image = image
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0))
        if self.image is None or self.image.isNull():
            return
        size = self.image.size().scaled(self.size(), Qt.KeepAspectRatio)
        x, y = (self.width() - size.width()) // 2, (self.height() - size.height()) // 2
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.drawImage(QRectF(x, y, size.width(), size.height()), self.image)


class ShotsView(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink
        self._path, self._mtime = "", None
        self.data, self.shots, self.transitions = None, [], []
        self.frames, self.fps, self.frame, self.chosen = 0, 24.0, 0, None
        self._thumbs, self._thumb_dir, self._proc = {}, "", None
        self._aspect = 9 / 16

        self.header = QLabel(objectName="TextLabel", wordWrap=True)
        self.screen = FrameScreen()
        self.player = QMediaPlayer(self)
        self.audio = QAudioOutput(self)
        self.player.setAudioOutput(self.audio)
        self.sink = QVideoSink(self)
        self.player.setVideoSink(self.sink)
        self.player.metaDataChanged.connect(self._fit)
        self.sink.videoFrameChanged.connect(self._on_frame)
        self.strip = ShotStrip(self)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        from .playbar import _icon
        self.buttons = {}
        for kind, icon, tip in (("prev", "first", "Previous shot (Up)"), ("play", "play", "Play / pause (Space)"),
                                ("next", "last", "Next shot (Down)")):
            b = QToolButton(objectName="Transport", toolTip=tip)
            b.setIcon(_icon(icon))
            b.setIconSize(QSize(16, 16))
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
            self.buttons[kind] = b
        self.now = QLabel(objectName="TextLabel")
        row.addSpacing(6)
        row.addWidget(self.now, 1)
        self.buttons["prev"].clicked.connect(lambda: self._step(-1))
        self.buttons["next"].clicked.connect(lambda: self._step(1))
        self.buttons["play"].clicked.connect(self.toggle)
        self.player.playbackStateChanged.connect(
            lambda s: self.buttons["play"].setIcon(_icon("pause" if s == QMediaPlayer.PlayingState else "play")))
        self.list = QListWidget(objectName="ShotList")
        self.list.setIconSize(QSize(int(THUMB_H * 16 / 9), THUMB_H))
        self.list.setUniformItemSizes(True)
        self.list.currentRowChanged.connect(lambda i: 0 <= i < len(self.shots) and self.shots[i] is not self.chosen
                                            and self.play_span(self.shots[i]))
        self.text = QPlainTextEdit(readOnly=True)  # any other JSON
        self.setStyleSheet("QToolButton#Transport { background: #3a3a3a; border: 1px solid #222; border-radius: 2px;"
                           " padding: 3px 5px; } QToolButton#Transport:hover { background: #4a6a8a; }"
                           " QListWidget#ShotList { background: #262626; color: #ccc; border: 1px solid #444;"
                           " outline: none; } QListWidget#ShotList::item { padding: 2px; }"
                           " QListWidget#ShotList::item:selected { background: #4a6a8a; color: #fff; }")
        self.setFocusPolicy(Qt.ClickFocus)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        for w in (self.header, self.screen, self.strip):
            v.addWidget(w)
        v.addLayout(row)
        v.addWidget(self.list)
        v.addWidget(self.text)
        self._poll = QTimer(self, interval=400, timeout=self._pick_up_thumbs)
        self.set_path("")

    # ---- the file ----------------------------------------------------------------------------------------------
    def set_path(self, path):
        path = path or ""
        mtime = os.path.getmtime(path) if path and os.path.isfile(path) else None
        if (path, mtime) == (self._path, self._mtime):
            return
        self._path, self._mtime = path, mtime
        self._stop_thumbs()
        self.player.stop()
        self.data = load(path) if mtime else None
        shots = spans_of(self.data) is not None
        for w in (self.screen, self.strip, self.list, self.now):
            w.setVisible(shots)
        for b in self.buttons.values():
            b.setVisible(shots)
        self.text.setVisible(bool(self.data) and not shots)
        self.list.clear()
        self.shots, self.transitions, self.chosen, self.frame = [], [], None, 0
        if not shots:
            self.frames = 0
            self.header.setText("" if self.data or not path else f"Can't read {os.path.basename(path)} as JSON")
            self.header.setVisible(bool(path) and not self.data)
            if self.data:
                self.text.setPlainText(json.dumps(self.data, indent=1, ensure_ascii=False))
            self.player.setSource(QUrl())
            return
        d = self.data
        self.fps, self.frames = float(d["fps_value"]) or 24.0, int(d.get("frames") or 0)
        self.shots = [dict(s, _fps=self.fps) for s in spans_of(d)]
        self.transitions = [dict(t, _fps=self.fps) for t in d.get("transitions", [])]
        det = d.get("detector") or {}
        video = d.get("video", "")
        found = f" · found by {det.get('model')} in {det.get('seconds')} s" if det.get("model") else ""
        missing = "" if os.path.isfile(video) else f"\nThe video isn't there any more: {video}"
        if is_chunks(d):
            self.header.setText(f"{len(self.shots)} chunks of at most {d.get('max_frames')} frames from "
                                f"{d.get('shot_count')} shots · {self.frames} frames at {self.fps:g} fps{missing}")
        else:
            self.header.setText(f"{len(self.shots)} shots · {len(self.transitions)} transition"
                                f"{'' if len(self.transitions) == 1 else 's'} · {self.frames} frames at "
                                f"{self.fps:g} fps{found}{missing}")
        self.header.setVisible(True)
        self.header.setToolTip(self._path)
        for s in self.shots:
            item = QListWidgetItem(describe(s))
            item.setToolTip(describe(s))
            self.list.addItem(item)
        self.list.setFixedHeight(min(8, max(1, len(self.shots))) * (THUMB_H + 6) + 6)
        self.player.setSource(QUrl.fromLocalFile(video) if os.path.isfile(video) else QUrl())
        self.strip.update()
        if os.path.isfile(video):
            self._make_thumbs(video)
        if self.shots:
            kind, i = _chosen_before.get(self._path, ("span", 0))
            spans = self.shots if kind == "span" else self.transitions
            span = spans[i] if 0 <= i < len(spans) else self.shots[0]
            self.choose(span)
            if self.list.currentItem():
                self.list.scrollToItem(self.list.currentItem())
            self._seek(span["first"])
            self.player.pause()  # shows the span's first frame

    # ---- playing a shot ------------------------------------------------------------------------------------------
    def span_at(self, frame):
        return next((s for s in self.shots + self.transitions if s["first"] <= frame <= s["last"]), None)

    def choose(self, span):
        self.chosen = span
        if span in self.shots:
            _chosen_before[self._path] = ("span", self.shots.index(span))
        elif span in self.transitions:
            _chosen_before[self._path] = ("transition", self.transitions.index(span))
        self.now.setText(describe(span))
        if span in self.shots:
            i = self.shots.index(span)
            if self.list.currentRow() != i:
                self.list.blockSignals(True)
                self.list.setCurrentRow(i)
                self.list.blockSignals(False)
        else:
            self.list.clearSelection()
        self.strip.update()

    def play_span(self, span):
        self.choose(span)
        self._seek(span["first"])
        self.player.play()

    def _seek(self, frame):
        self.frame = frame
        self.player.setPosition(math.floor((frame - 1) / self.fps * 1000) + 1)  # 1 ms into the frame (playbar.py)
        self.strip.update()

    def _on_frame(self, video_frame):
        """Each frame shown: move the playhead; past the chosen span's last frame, go back to its first (loop)."""
        if not self.frames or not video_frame.isValid() or video_frame.startTime() < 0:
            return
        frame = math.floor(video_frame.startTime() / 1e6 * self.fps + 1e-6) + 1
        span = self.chosen
        if span and frame < span["first"]:  # right after a seek Qt still decodes the frame before it: not shown
            return
        if span and frame > span["last"]:  # past the end: not shown; playing, go round again
            if self.player.isPlaying():
                self._seek(span["first"])
            return
        self.screen.set_image(video_frame.toImage())
        if frame != self.frame:
            self.frame = frame
            self.strip.update()

    def toggle(self):
        if self.player.isPlaying():
            self.player.pause()
        elif self.chosen:
            self.player.play()

    def _step(self, delta):
        if not self.shots:
            return
        i = self.shots.index(self.chosen) if self.chosen in self.shots else -1
        if self.chosen in self.transitions:  # between shots: the next is the one after it, the previous before
            i = (self.chosen.get("before_shot") or len(self.shots) + 1) - 1 - (1 if delta > 0 else 0)
        self.play_span(self.shots[max(0, min(len(self.shots) - 1, i + delta))])

    def keyPressEvent(self, e):
        keys = {Qt.Key_Space: self.toggle, Qt.Key_Up: lambda: self._step(-1), Qt.Key_Down: lambda: self._step(1),
                Qt.Key_Left: lambda: self._step(-1), Qt.Key_Right: lambda: self._step(1)}
        if self.shots and e.key() in keys:
            keys[e.key()]()
            e.accept()
        else:
            super().keyPressEvent(e)

    # ---- size ----------------------------------------------------------------------------------------------------
    def _fit(self):
        from PySide6.QtMultimedia import QMediaMetaData
        size = self.player.metaData().value(QMediaMetaData.Resolution)
        if size and size.width():
            self._aspect = size.height() / size.width()
        self._resize_screen()

    def _resize_screen(self):
        self.screen.setFixedHeight(max(60, int(self.width() * self._aspect)))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._resize_screen()

    def showEvent(self, e):
        super().showEvent(e)
        self.strip.setFixedHeight(int(self.fontMetrics().height() * 1.6))

    def hideEvent(self, e):
        self.player.pause()
        super().hideEvent(e)

    # ---- first-frame pictures (one ffmpeg pass picks every shot's first frame) -----------------------------------
    def _make_thumbs(self, video):
        key = (os.path.abspath(video), os.path.getmtime(video), tuple(s["first"] for s in self.shots))
        self._thumb_dir = os.path.join(media_cache.CACHE_DIR,
                                       "thumbs_" + hashlib.sha1(repr(key).encode("utf-8")).hexdigest()[:12])
        done = os.path.join(self._thumb_dir, "done")
        if os.path.isfile(done):
            self._pick_up_thumbs()
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return
        os.makedirs(self._thumb_dir, exist_ok=True)
        # frame n (0-based, as decoded - the detector counted the same way) of each shot's first frame
        # summed as a balanced tree: ffmpeg's expression parser refuses a flat a+b+c... of ~130 terms
        def tree(terms):
            half = len(terms) // 2
            return terms[0] if len(terms) == 1 else f"({tree(terms[:half])}+{tree(terms[half:])})"
        pick = tree([f"eq(n\\,{s['first'] - 1})" for s in self.shots])
        self._proc = QProcess(self)
        self._proc.finished.connect(lambda code, _st, p=self._proc: self._thumbs_done(p, code, done))
        self._proc.start(ffmpeg, ["-v", "error", "-y", "-i", video, "-an", "-vf",
                                  f"select='{pick}',scale=-2:{THUMB_H * 2}", "-fps_mode", "passthrough",
                                  "-q:v", "4", os.path.join(self._thumb_dir, "t%04d.jpg")])
        self._poll.start()

    def _thumbs_done(self, proc, code, done):
        if proc is not self._proc:
            return
        self._proc = None
        self._poll.stop()
        if code == 0:
            open(done, "w").close()
        self._pick_up_thumbs(finished=True)

    def _pick_up_thumbs(self, finished=False):
        """Put the pictures ffmpeg has written so far on the list (one is complete once the next one exists)."""
        finished = finished or self._proc is None
        for i in range(self.list.count()):
            if i in self._thumbs:
                continue
            f = os.path.join(self._thumb_dir, f"t{i + 1:04d}.jpg")
            if not os.path.isfile(f):
                break
            if not finished and not os.path.isfile(os.path.join(self._thumb_dir, f"t{i + 2:04d}.jpg")):
                break
            pm = QPixmap(f)
            if pm.isNull():
                continue
            self._thumbs[i] = pm
            self.list.item(i).setIcon(QIcon(pm))

    def _stop_thumbs(self):
        self._poll.stop()
        self._thumbs = {}
        if self._proc is not None:
            proc, self._proc = self._proc, None
            proc.kill()
            proc.waitForFinished(1000)
