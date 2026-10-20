"""Houdini-style playbar for a QMediaPlayer: a frame timeline with a playhead, transport buttons and a frame field.

Frames count from 1 (as in Houdini). Frame f starts at (f - 1) / fps seconds; the player seeks 1 ms past that
start: at exactly a frame's timestamp it shows the frame BEFORE it, and past the last frame a looping player
wraps to the start. The frame count is the video stream's own (ffprobe reads it from the file header): the
file's duration can be longer than the video (e.g. audio running on - a 30-frame 1.000 s clip in a 1.021 s
file). Without ffprobe, or a count in the header, it is the whole frames that fit in the duration. fps comes from the file (the player's
metadata); a file that doesn't record it is treated as 24 fps and the playbar says so.
Click / drag the timeline to scrub; with the player focused: Left / Right = one frame, Space = play / pause.
"""

import json
import math
import shutil
import subprocess

from PySide6.QtCore import QPointF, QRectF, QRegularExpression, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QRegularExpressionValidator
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QToolButton, QVBoxLayout, QWidget

PLAYHEAD = QColor(80, 150, 240)  # Houdini's blue current-frame marker
ICON = QColor(215, 215, 215)
TRIM = QColor(255, 200, 60)      # the part kept by a trim: tinted, with brackets at its ends


def paint_trim(p, r, x0, x1):
    """Shade what a trim cuts away and bracket what it keeps (x0..x1) on a timeline rect r."""
    p.save()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0, 120))
    p.drawRect(QRectF(r.left(), r.top(), max(0.0, x0 - r.left()), r.height()))
    p.drawRect(QRectF(x1, r.top(), max(0.0, r.right() - x1), r.height()))
    kept = QColor(TRIM)
    kept.setAlpha(28)
    p.setBrush(kept)
    p.drawRect(QRectF(x0, r.top(), x1 - x0, r.height()))
    p.setPen(QPen(TRIM, 1.2))
    arm = 3
    for x, d in ((x0, 1), (x1, -1)):  # [ at the start, ] at the end
        p.drawLine(QPointF(x, r.top() + 1), QPointF(x, r.bottom() - 1))
        p.drawLine(QPointF(x, r.top() + 1), QPointF(x + d * arm, r.top() + 1))
        p.drawLine(QPointF(x, r.bottom() - 1), QPointF(x + d * arm, r.bottom() - 1))
    p.restore()


class TrimHandles:
    """For a timeline (QWidget) with draggable trim brackets: grab the [ or ] and slide it; double-click one =
    back to the very start / end. The timeline supplies _trim_x() -> (x0, x1), _trim_value(which, x) and
    _trim_end(which), and the signals trim_started(str), trim_dragged(str, value), trim_released()."""

    GRAB = 6  # pixels either side of a bracket that grab it

    def _init_trim(self):
        self.trim = None            # (start, end) kept, drawn when set
        self.trim_editable = False  # brackets can be dragged
        self._grab = None           # (which, offset from the mouse to the bracket) while dragging
        self.setMouseTracking(True)

    def bracket_at(self, x):
        if not (self.trim and self.trim_editable):
            return None
        x0, x1 = self._trim_x()
        d0, d1 = abs(x - x0), abs(x - x1)
        if min(d0, d1) > self.GRAB:
            return None
        if abs(d0 - d1) < 1e-6:  # both at one place: the side the mouse is on
            return "in" if x <= x0 else "out"
        return "in" if d0 < d1 else "out"

    def _trim_press(self, e):
        x = e.position().x()
        which = self.bracket_at(x) if e.button() == Qt.LeftButton else None
        if which is None:
            return False
        self._grab = (which, self._trim_x()[0 if which == "in" else 1] - x)
        self.trim_started.emit(which)
        return True

    def _trim_move(self, e):
        x = e.position().x()
        if self._grab:
            which, offset = self._grab
            self.trim_dragged.emit(which, self._trim_value(which, x + offset))
            return True
        if not e.buttons():
            self.setCursor(Qt.SizeHorCursor if self.bracket_at(x) else Qt.PointingHandCursor)
        return False

    def _trim_release(self, e):
        if not self._grab:
            return False
        self._grab = None
        self.trim_released.emit()
        return True

    def mouseDoubleClickEvent(self, e):
        which = self.bracket_at(e.position().x())
        if which:
            self.trim_started.emit(which)
            self.trim_dragged.emit(which, self._trim_end(which))
            self.trim_released.emit()
            e.accept()


def _icon(kind, size=16):
    """Transport icons drawn here: light shapes that read on the dark theme."""
    pm = QPixmap(size * 2, size * 2)  # drawn at 2x for sharp scaling
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(ICON)
    s = size * 2

    def tri(x0, x1, right=True):
        path = QPainterPath()
        if right:
            path.moveTo(x0, s * 0.2); path.lineTo(x1, s * 0.5); path.lineTo(x0, s * 0.8)
        else:
            path.moveTo(x1, s * 0.2); path.lineTo(x0, s * 0.5); path.lineTo(x1, s * 0.8)
        path.closeSubpath()
        p.drawPath(path)

    def bar(x):
        p.drawRect(QRectF(x, s * 0.2, s * 0.1, s * 0.6))

    if kind == "play":
        tri(s * 0.3, s * 0.78)
    elif kind == "pause":
        bar(s * 0.3); bar(s * 0.58)
    elif kind == "next":       # |> one frame forward
        tri(s * 0.25, s * 0.62); bar(s * 0.65)
    elif kind == "prev":       # <| one frame back
        bar(s * 0.25); tri(s * 0.38, s * 0.75, right=False)
    elif kind == "last":       # >>| to the end
        tri(s * 0.12, s * 0.45); tri(s * 0.42, s * 0.75); bar(s * 0.77)
    elif kind == "first":      # |<< to the start
        bar(s * 0.13); tri(s * 0.25, s * 0.58, right=False); tri(s * 0.55, s * 0.88, right=False)
    p.end()
    return QIcon(pm)


def stream_frames(path):
    """(frames, fps) of the file's video stream from its header via ffprobe, or (0, 0) when not recorded."""
    probe = shutil.which("ffprobe")
    if not probe or not path:
        return 0, 0.0
    p = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=nb_frames,duration,r_frame_rate:stream_tags=DURATION", "-of", "json", path],
                       capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        st = json.loads(p.stdout)["streams"][0]
        num, den = (int(x) for x in st["r_frame_rate"].split("/"))
        fps = num / den
        if str(st.get("nb_frames", "")).isdigit() and int(st["nb_frames"]) > 0:
            return int(st["nb_frames"]), fps
        seconds = float(st["duration"]) if st.get("duration") else 0.0
        tag = (st.get("tags") or {}).get("DURATION", "")  # Matroska / WebM keep the stream's length only here
        if not seconds and tag.count(":") == 2:
            h, m, s = tag.split(":")
            seconds = int(h) * 3600 + int(m) * 60 + float(s)
        if seconds:  # durations are rounded (to the ms in Matroska): allow a small fraction of a frame
            return math.floor(seconds * fps + 0.05), fps
        return 0, fps
    except (ValueError, KeyError, IndexError, ZeroDivisionError):
        return 0, 0.0


class FrameBar(TrimHandles, QWidget):
    """The timeline: frames 1..N with ticks and numbers, and the playhead on the current frame. With a trim
    (the Edit Media node), what it keeps is bracketed; editable brackets can be dragged."""

    frame_requested = Signal(int)
    trim_started = Signal(str)
    trim_dragged = Signal(str, object)
    trim_released = Signal()

    def __init__(self):
        super().__init__()
        self.frames, self.frame = 0, 1
        self.marks = []  # frames to mark (the Edit Geometry node's keys of what is selected), red like Maya's
        self._init_trim()  # (first, last) frame kept
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click or drag to go to a frame · wheel: one frame")

    def set_frames(self, frames, frame):
        if (frames, frame) != (self.frames, self.frame):
            self.frames, self.frame = frames, frame
            self.update()

    def _area(self):
        return QRectF(self.rect()).adjusted(8, 0, -8, 0)  # room for the playhead at frame 1 / N

    def _x(self, f):
        a = self._area()
        return a.left() + (f - 1) / max(1, self.frames - 1) * a.width()

    def _frame_at(self, x):
        a = self._area()
        return max(1, min(self.frames, round((x - a.left()) / max(1.0, a.width()) * (self.frames - 1)) + 1))

    # trim brackets sit half a frame outside their frames (the kept span covers whole frames)
    def _half(self):
        return self._area().width() / max(1, self.frames - 1) / 2

    def _trim_x(self):
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        return (max(r.left() + 1, self._x(self.trim[0]) - self._half()),
                min(r.right() - 1, self._x(self.trim[1]) + self._half()))

    def _trim_value(self, which, x):
        if which == "in":
            return min(self._frame_at(x + self._half()), self.trim[1])
        return max(self._frame_at(x - self._half()), self.trim[0])

    def _trim_end(self, which):
        return 1 if which == "in" else self.frames

    def mousePressEvent(self, e):
        if self.frames and self._trim_press(e):
            return
        if self.frames and e.button() == Qt.LeftButton:
            self.frame_requested.emit(self._frame_at(e.position().x()))

    def mouseMoveEvent(self, e):
        if self.frames and self._trim_move(e):
            return
        if self.frames and e.buttons() & Qt.LeftButton:
            f = self._frame_at(e.position().x())
            if f != self.frame:
                self.frame_requested.emit(f)

    def mouseReleaseEvent(self, e):
        self._trim_release(e)

    def wheelEvent(self, e):
        if self.frames:
            f = max(1, min(self.frames, self.frame + (1 if e.angleDelta().y() > 0 else -1)))
            if f != self.frame:
                self.frame_requested.emit(f)
        e.accept()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(68, 68, 68)))
        p.setBrush(QColor(30, 30, 30))
        p.drawRoundedRect(r, 2, 2)
        if self.frames < 1:
            return
        if self.trim:
            paint_trim(p, r, *self._trim_x())
        font = p.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.72))
        p.setFont(font)
        a = self._area()
        per_frame = a.width() / max(1, self.frames - 1)
        # label every n frames, n from 1, 2, 5, 10, 20, 50... so numbers are at least ~34 px apart
        label_every = next(n for n in (m * 10 ** e for e in range(7) for m in (1, 2, 5)) if n * per_frame >= 34)
        tick_every = 1 if per_frame >= 4 else max(1, label_every // 5)
        mid = r.top() + r.height() * 0.5
        # the playhead's tag goes where the current frame's number would be; numbers it would cover are skipped
        x = self._x(self.frame)
        text = str(self.frame)
        w = p.fontMetrics().horizontalAdvance(text) + 8
        tag = QRectF(min(max(x - w / 2, r.left() + 1), r.right() - w - 1), mid, w, r.bottom() - mid - 1)
        for f in range(1, self.frames + 1):
            labelled = f == 1 or f % label_every == 0
            if not labelled and f % tick_every:
                continue
            x = self._x(f)
            p.setPen(QPen(QColor(150, 150, 150) if labelled else QColor(95, 95, 95), 1))
            p.drawLine(QPointF(x, r.top() + 2), QPointF(x, mid if labelled else r.top() + (mid - r.top()) * 0.55))
            if labelled:
                half = p.fontMetrics().horizontalAdvance(str(f)) / 2 + 2
                box = QRectF(x - half, mid, 2 * half, r.bottom() - mid)
                if not box.intersects(tag) and box.left() >= r.left() and box.right() <= r.right():
                    p.setPen(QColor(150, 150, 150))
                    p.drawText(box, Qt.AlignCenter, str(f))
        if self.marks:  # keys: short red bars at the top
            p.setPen(QPen(QColor(225, 70, 70), 2))
            for f in self.marks:
                if 1 <= f <= self.frames:
                    p.drawLine(QPointF(self._x(f), r.top() + 1), QPointF(self._x(f), r.top() + (mid - r.top()) * 0.8))
        # the playhead: a blue line with the frame number on a blue tag
        x = self._x(self.frame)
        p.setPen(QPen(PLAYHEAD, 2))
        p.drawLine(QPointF(x, r.top() + 1), QPointF(x, mid))
        p.setPen(Qt.NoPen)
        p.setBrush(PLAYHEAD)
        p.drawRoundedRect(tag, 2, 2)
        p.setPen(QColor(255, 255, 255))
        p.drawText(tag, Qt.AlignCenter, text)


class Playbar(QWidget):
    """Timeline + transport row, driving a QMediaPlayer."""

    def __init__(self, player):
        super().__init__()
        self.player = player
        self.fps, self.fps_known, self.frames = 24.0, False, 0
        self._stream = (0, 0.0)  # (frames, fps) from the file header, when readable
        self.timeline = FrameBar()
        self.buttons = {}
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        for kind, tip in (("first", "First frame"), ("prev", "Previous frame (Left)"), ("play", "Play / pause (Space)"),
                          ("next", "Next frame (Right)"), ("last", "Last frame")):
            b = QToolButton(objectName="Transport", toolTip=tip)
            b.setIcon(_icon(kind))
            b.setIconSize(QSize(16, 16))
            b.setFocusPolicy(Qt.NoFocus)
            row.addWidget(b)
            self.buttons[kind] = b
        self.field = QLineEdit(objectName="FrameField", toolTip="Frame - type a number and press Enter")
        self.field.setValidator(QRegularExpressionValidator(QRegularExpression(r"\d{0,6}"), self.field))
        self.field.setFixedWidth(self.fontMetrics().horizontalAdvance("00000") + 14)
        self.field.setAlignment(Qt.AlignCenter)
        self.total = QLabel(objectName="TextLabel")
        row.addSpacing(6)
        row.addWidget(self.field)
        row.addWidget(self.total)
        self._tail = QHBoxLayout()  # add_tail(): widgets right after the fps (e.g. auto-key / Set Key)
        self._tail.setSpacing(2)
        row.addSpacing(8)
        row.addLayout(self._tail)
        row.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        lay.addWidget(self.timeline)
        lay.addLayout(row)
        self.setStyleSheet("QToolButton#Transport { background: #3a3a3a; border: 1px solid #222; border-radius: 2px;"
                           " padding: 3px 5px; } QToolButton#Transport:hover { background: #4a6a8a; }"
                           " QToolButton#Transport:disabled { background: #2e2e2e; }")

        self.buttons["first"].clicked.connect(lambda: self.go(1))
        self.buttons["last"].clicked.connect(lambda: self.go(self.frames))
        self.buttons["prev"].clicked.connect(lambda: self.step(-1))
        self.buttons["next"].clicked.connect(lambda: self.step(1))
        self.buttons["play"].clicked.connect(self.toggle)
        self.timeline.frame_requested.connect(self.go)
        self.field.editingFinished.connect(self._typed)
        player.positionChanged.connect(self._sync)
        player.durationChanged.connect(lambda _: self._measure())
        player.metaDataChanged.connect(self._measure)
        player.playbackStateChanged.connect(self._state)
        self._measure()

    def add_tail(self, widget):
        self._tail.addWidget(widget)

    display_fps = None  # shown instead of the timing's rate (Edit Geometry: frames kept, played at its own rate)

    def showEvent(self, event):
        # the timeline is exactly as high as the text fields (font + style), like the rulers - at any UI scale
        self.timeline.setFixedHeight(self.field.sizeHint().height())
        super().showEvent(event)

    # ---- frames <-> time ---------------------------------------------------------------------------------
    def set_file(self, path):
        self._stream = stream_frames(path) if path else (0, 0.0)
        self._measure()

    def _measure(self):
        from PySide6.QtMultimedia import QMediaMetaData
        rate = self.player.metaData().value(QMediaMetaData.VideoFrameRate)
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            rate = 0.0
        frames, header_fps = self._stream
        rate = header_fps or rate
        self.fps_known = rate > 0
        self.fps = rate if self.fps_known else 24.0
        by_duration = math.floor(self.player.duration() / 1000 * self.fps + 1e-6)
        self.frames = max(0, frames or by_duration)
        for b in self.buttons.values():
            b.setEnabled(self.frames > 0)
        self.field.setEnabled(self.frames > 0)
        self._sync(self.player.position())

    def current(self):
        return max(1, min(self.frames, math.floor(self.player.position() / 1000 * self.fps + 1e-6) + 1))

    def go(self, frame):
        if not self.frames:
            return
        frame = max(1, min(self.frames, int(frame)))
        self.player.setPosition(math.floor((frame - 1) / self.fps * 1000) + 1)  # 1 ms into the frame (see the top)
        self._sync(self.player.position(), frame)

    def step(self, delta):
        self.player.pause()
        self.go(self.current() + delta)

    def toggle(self):
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    # ---- display -----------------------------------------------------------------------------------------
    def _sync(self, _pos, frame=None):
        frame = frame or (self.current() if self.frames else 1)
        self.timeline.set_frames(self.frames, frame)
        if not self.field.hasFocus():
            self.field.setText(str(frame) if self.frames else "")
        if self.frames:
            fps = f"{self.fps:g} fps" if self.fps_known else "fps not recorded (24 assumed)"
            if self.display_fps:
                fps = f"{self.display_fps:g} fps"
            self.total.setText(f"/ {self.frames}  ·  {fps}")
        else:
            self.total.setText("")

    def _typed(self):
        if self.field.text().strip():
            self.player.pause()
            self.go(int(self.field.text()))

    def _state(self, state):
        from PySide6.QtMultimedia import QMediaPlayer
        playing = state == QMediaPlayer.PlayingState
        self.buttons["play"].setIcon(_icon("pause" if playing else "play"))
