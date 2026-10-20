"""Audio playbar: a live equalizer (spectrum analyser) above a timeline that works like the video playbar's.

Equalizer: the sound around the playhead, as frequency bars - bass left, treble right: 64 bands spaced
logarithmically from 50 Hz to 15 kHz (each octave the same width, as we hear it; on a linear scale everything
below 1 kHz - bass and most of a voice - would fit in 4 of the 64 bars), with a frequency scale along the top
and a peak cap on each bar that falls slowly. ffmpeg decodes the file once to 32 kHz mono (15 kHz needs a
rate above 30 kHz; cached); a 4096-point FFT (7.8 Hz per bin) in plain Python - the app's own environment has
no numpy - takes ~5.6 ms, redrawn ~30x a second while playing. Bass bands narrower than a bin are read
between the two nearest bins, so neighbouring bars don't come out identical. It only shows; the timeline below moves: click / drag, wheel = 0.1 s (Ctrl: 1 s), and
with the player focused Left / Right = 1 s, Space = play / pause. Without ffmpeg the timeline still works.
"""

import cmath
import math
import os
import shutil
import subprocess
from array import array

from PySide6.QtCore import QPointF, QRectF, QRegularExpression, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPolygonF, QRegularExpressionValidator
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QToolButton, QVBoxLayout, QWidget

from .playbar import PLAYHEAD, TRIM, TrimHandles, _icon, paint_trim

RATE = 32000          # decode rate: the spectrum goes up to 16 kHz
N = 4096              # FFT size: 128 ms at 32 kHz, 7.8 Hz per bin
BANDS = 64
LOW_HZ, HIGH_HZ = 50.0, 15000.0
SCALE_HZ = (50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000)  # labelled ticks along the top
FLOOR_DB = -70.0      # quieter than this (relative to the file's loudest moment) draws nothing
_decoded = {}         # (path, mtime) -> array('h') of 32 kHz mono samples (about 3.8 MB per minute), 2 files


def decode(path):
    """The file as 32 kHz mono 16-bit samples (ffmpeg), cached per path + modification time; empty if not."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not path:
        return array("h")
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return array("h")
    if key not in _decoded:
        p = subprocess.run([ffmpeg, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                           capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        samples = array("h")
        samples.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
        while len(_decoded) >= 2:  # keep the two latest files (e.g. an Edit Media node's source and result)
            del _decoded[next(iter(_decoded))]
        _decoded[key] = samples
    return _decoded[key]


# ---- the FFT (radix-2, in place) --------------------------------------------------------------------------
_REV = [int(f"{i:0{N.bit_length() - 1}b}"[::-1], 2) for i in range(N)]
_TW = [cmath.exp(-2j * math.pi * k / N) for k in range(N // 2)]
_WIN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (N - 1)) for i in range(N)]
# log-spaced band edges; per band either the FFT bins inside it, or - narrower than a bin - the fractional
# bin at its (geometric) centre, read between the two nearest bins
_EDGES = [LOW_HZ * (HIGH_HZ / LOW_HZ) ** (b / BANDS) for b in range(BANDS + 1)]
_BAND_BINS = []
for _lo, _hi in zip(_EDGES, _EDGES[1:]):
    first, last = math.ceil(_lo * N / RATE), math.floor(_hi * N / RATE)
    _BAND_BINS.append(range(first, last + 1) if last >= first else math.sqrt(_lo * _hi) * N / RATE)


def freq_x(hz):
    """0..1 position of a frequency on the log scale."""
    return math.log(hz / LOW_HZ) / math.log(HIGH_HZ / LOW_HZ)


def _spectrum(chunk):
    a = [chunk[_REV[i]] * _WIN[_REV[i]] for i in range(N)]
    size = 2
    while size <= N:
        half, step = size // 2, N // size
        for start in range(0, N, size):
            for k in range(half):
                t = _TW[k * step] * a[start + k + half]
                u = a[start + k]
                a[start + k] = u + t
                a[start + k + half] = u - t
        size *= 2
    return a


def band_levels(samples, seconds):
    """[0..1] * BANDS for the 128 ms of sound starting at `seconds` (dB, FLOOR_DB..0 of full scale)."""
    start = int(seconds * RATE)
    chunk = samples[start:start + N]
    if len(chunk) < N:
        return [0.0] * BANDS
    spec = _spectrum(chunk)
    levels = []
    for bins in _BAND_BINS:
        if isinstance(bins, range):
            mag = max(abs(spec[k]) for k in bins)
        else:  # between two bins
            k = int(bins)
            mag = abs(spec[k]) + (bins - k) * (abs(spec[k + 1]) - abs(spec[k]))
        power = mag / (N / 4 * 32768)  # a Hann-windowed full-scale sine comes out ~1.0
        db = 20 * math.log10(power) if power > 0 else FLOOR_DB
        levels.append(max(0.0, min(1.0, (db - FLOOR_DB) / -FLOOR_DB)))
    return levels


def clock(seconds, hundredths=True):
    m, s = divmod(max(0.0, seconds), 60)
    return f"{int(m)}:{s:05.2f}" if hundredths else f"{int(m)}:{int(s):02d}"


class Equalizer(QWidget):
    """Frequency bars for the sound at the playhead, with slowly falling peak caps. Display only."""

    def __init__(self):
        super().__init__()
        self.samples = array("h")
        self.levels = [0.0] * BANDS
        self.caps = [0.0] * BANDS
        self.setFixedHeight(86)  # 72 for the bars + the frequency scale

    def set_samples(self, samples):
        self.samples = samples
        self.levels, self.caps = [0.0] * BANDS, [0.0] * BANDS
        self.update()

    def show_at(self, seconds, playing):
        new = band_levels(self.samples, seconds) if self.samples else [0.0] * BANDS
        if playing:  # bars jump up at once and fall back gently; caps fall slower
            self.levels = [max(n, old - 0.08) for n, old in zip(new, self.levels)]
            self.caps = [max(lv, c - 0.02) for lv, c in zip(self.levels, self.caps)]
        else:        # paused / scrubbed: the spectrum at that exact spot
            self.levels = new
            self.caps = list(new)
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(68, 68, 68)))
        p.setBrush(QColor(16, 18, 24))
        p.drawRoundedRect(r, 3, 3)
        font = p.font()
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.66))
        p.setFont(font)
        scale_h = p.fontMetrics().height() + 4
        area = r.adjusted(6, scale_h + 3, -6, -4)
        # frequency scale along the top: labelled ticks on the same log scale as the bars
        for hz in SCALE_HZ:
            x = area.left() + freq_x(hz) * area.width()
            label = f"{hz // 1000}k" if hz >= 1000 else str(hz)
            half = p.fontMetrics().horizontalAdvance(label) / 2
            p.setPen(QPen(QColor(85, 85, 85), 1))
            p.drawLine(QPointF(x, r.top() + scale_h - 2), QPointF(x, r.top() + scale_h + 2))
            p.setPen(QColor(135, 135, 135))
            box = QRectF(min(max(x - half, r.left() + 3), r.right() - 3 - 2 * half), r.top() + 1, 2 * half, scale_h - 3)
            p.drawText(box, Qt.AlignCenter, label)
        if not self.samples:
            p.setPen(QColor(130, 130, 130))
            p.drawText(area, Qt.AlignCenter, "no equalizer (ffmpeg not found or the file can't be read)")
            return
        slot = area.width() / BANDS
        gap = max(1.0, slot * 0.25)
        g = QLinearGradient(0, area.bottom(), 0, area.top())
        g.setColorAt(0, QColor(30, 80, 190))
        g.setColorAt(0.6, QColor(70, 150, 245))
        g.setColorAt(1, QColor(175, 220, 255))
        for i, (level, cap) in enumerate(zip(self.levels, self.caps)):
            x = area.left() + i * slot + gap / 2
            w = slot - gap
            h = level * area.height()
            if h > 0.5:
                p.setPen(Qt.NoPen)
                p.setBrush(g)
                p.drawRoundedRect(QRectF(x, area.bottom() - h, w, h), 1.5, 1.5)
            if cap > 0.02:
                y = area.bottom() - cap * area.height()
                p.setPen(QPen(QColor(190, 225, 255), 2))
                p.drawLine(QPointF(x, y - 1), QPointF(x + w, y - 1))


class TimeBar(TrimHandles, QWidget):
    """The audio timeline - the video playbar's timeline in seconds: ticks, time labels, the blue playhead tag.
    Click / drag to go there; wheel = 0.1 s (Ctrl: 1 s). With a trim (the Edit Media node), what it keeps is bracketed;
    editable brackets can be dragged. With fades (seconds of this timeline), a ramp runs from each bracket's foot
    to a handle on the top edge: drag the handle along the top to lengthen / shorten the fade (double-click: none)."""

    seek_requested = Signal(float)
    trim_started = Signal(str)
    trim_dragged = Signal(str, object)
    trim_released = Signal()
    fade_started = Signal(str)
    fade_dragged = Signal(str, float)
    fade_released = Signal()

    def __init__(self):
        super().__init__()
        self.duration, self.position = 0.0, 0.0
        self._init_trim()  # (start, end) seconds kept
        self.fades = None       # (fade in, fade out) in seconds of this timeline, drawn inside the trim when set
        self._fade_grab = None  # (which, offset) while a fade handle is dragged
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Click or drag to go there · wheel: 0.1 s (Ctrl: 1 s)")

    def set_time(self, duration, position):
        if (duration, position) != (self.duration, self.position):
            self.duration, self.position = duration, position
            self.update()

    def _area(self):
        return QRectF(self.rect()).adjusted(8, 0, -8, 0)

    def _time_at(self, x):
        a = self._area()
        return max(0.0, min(self.duration, (x - a.left()) / max(1.0, a.width()) * self.duration))

    def _trim_x(self):
        a = self._area()
        return (a.left() + self.trim[0] / self.duration * a.width(), a.left() + self.trim[1] / self.duration * a.width())

    def _trim_value(self, which, x):
        t = round(self._time_at(x), 3)
        return min(t, self.trim[1]) if which == "in" else max(t, self.trim[0])

    def _trim_end(self, which):
        return 0.0 if which == "in" else round(self.duration, 3)

    # ---- fade handles: on the top edge, at the end of each fade (on the bracket when there is none) ------------
    def _fade_x(self):
        x0, x1 = self._trim_x()
        per_s = self._area().width() / self.duration
        return x0 + self.fades[0] * per_s, x1 - self.fades[1] * per_s

    def fade_at(self, pos):
        """'in' / 'out' when pos is on a fade handle (the upper part of the timeline, near the handle)."""
        if not (self.trim and self.fades is not None and self.trim_editable) or pos.y() > self.height() * 0.45:
            return None
        xi, xo = self._fade_x()
        di, do = abs(pos.x() - xi), abs(pos.x() - xo)
        if min(di, do) > self.GRAB:
            return None
        return "in" if di <= do else "out"

    def _fade_length(self, which, x):
        t = self._time_at(x)
        span = self.trim[1] - self.trim[0]
        other = self.fades[1] if which == "in" else self.fades[0]
        length = t - self.trim[0] if which == "in" else self.trim[1] - t
        return round(max(0.0, min(span - other, length)), 3)

    def mousePressEvent(self, e):
        which = self.fade_at(e.position()) if self.duration and e.button() == Qt.LeftButton else None
        if which:
            xi, xo = self._fade_x()
            self._fade_grab = (which, (xi if which == "in" else xo) - e.position().x())
            self.fade_started.emit(which)
            return
        if self.duration and self._trim_press(e):
            return
        if self.duration and e.button() == Qt.LeftButton:
            self.seek_requested.emit(self._time_at(e.position().x()))

    def mouseMoveEvent(self, e):
        if self._fade_grab:
            which, offset = self._fade_grab
            self.fade_dragged.emit(which, self._fade_length(which, e.position().x() + offset))
            return
        if self.duration and not e.buttons() and self.fade_at(e.position()):
            self.setCursor(Qt.SizeHorCursor)
            return
        if self.duration and self._trim_move(e):
            return
        if self.duration and e.buttons() & Qt.LeftButton:
            self.seek_requested.emit(self._time_at(e.position().x()))

    def mouseReleaseEvent(self, e):
        if self._fade_grab:
            self._fade_grab = None
            self.fade_released.emit()
            return
        self._trim_release(e)

    def mouseDoubleClickEvent(self, e):
        which = self.fade_at(e.position()) if self.duration else None
        if which:  # no fade
            self.fade_started.emit(which)
            self.fade_dragged.emit(which, 0.0)
            self.fade_released.emit()
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def _paint_fades(self, p, r):
        """Fade ramps: from each bracket's foot up to its handle on the top edge; the faded-out corner darkened."""
        x0, x1 = self._trim_x()
        xi, xo = self._fade_x()
        top, bottom = r.top() + 1.5, r.bottom() - 1
        p.save()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 90))
        if xi > x0:
            p.drawPolygon(QPolygonF([QPointF(x0, top), QPointF(xi, top), QPointF(x0, bottom)]))
        if xo < x1:
            p.drawPolygon(QPolygonF([QPointF(xo, top), QPointF(x1, top), QPointF(x1, bottom)]))
        ramp = QColor(TRIM)
        ramp.setAlpha(200)
        p.setPen(QPen(ramp, 1.2))
        if xi > x0:
            p.drawLine(QPointF(x0, bottom), QPointF(xi, top))
        if xo < x1:
            p.drawLine(QPointF(xo, top), QPointF(x1, bottom))
        p.setPen(QPen(QColor(30, 30, 30), 1))
        p.setBrush(TRIM)
        for x in (xi, xo):  # the handles
            p.drawRect(QRectF(x - 3, top - 1, 6, 6))
        p.restore()

    def wheelEvent(self, e):
        if self.duration:
            step = 1.0 if e.modifiers() & Qt.ControlModifier else 0.1
            direction = 1 if e.angleDelta().y() > 0 else -1
            self.seek_requested.emit(max(0.0, min(self.duration, self.position + direction * step)))
        e.accept()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(68, 68, 68)))
        p.setBrush(QColor(30, 30, 30))
        p.drawRoundedRect(r, 2, 2)
        if not self.duration:
            return
        a = self._area()
        if self.trim:
            paint_trim(p, r, *self._trim_x())
            if self.fades is not None:
                self._paint_fades(p, r)
        font = p.font()
        font.setPointSizeF(max(6.5, font.pointSizeF() * 0.72))
        p.setFont(font)
        mid = r.top() + r.height() * 0.5
        per_s = a.width() / self.duration
        x_play = a.left() + self.position * per_s
        text = clock(self.position)
        w = p.fontMetrics().horizontalAdvance(text) + 8
        tag = QRectF(min(max(x_play - w / 2, r.left() + 1), r.right() - w - 1), mid, w, r.bottom() - mid - 1)
        label_every = next((s for s in (0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600) if s * per_s >= 40), 900)
        tick_every = label_every / 5
        k = 0
        while k * tick_every <= self.duration + 1e-9:
            t = k * tick_every
            x = a.left() + t * per_s
            labelled = k % 5 == 0
            p.setPen(QPen(QColor(150, 150, 150) if labelled else QColor(95, 95, 95), 1))
            p.drawLine(QPointF(x, r.top() + 2), QPointF(x, mid if labelled else r.top() + (mid - r.top()) * 0.55))
            if labelled:
                label = clock(t, label_every < 1)
                half = p.fontMetrics().horizontalAdvance(label) / 2 + 2
                box = QRectF(x - half, mid, 2 * half, r.bottom() - mid)
                if not box.intersects(tag) and box.left() >= r.left() and box.right() <= r.right():
                    p.setPen(QColor(150, 150, 150))
                    p.drawText(box, Qt.AlignCenter, label)
            k += 1
        p.setPen(QPen(PLAYHEAD, 2))
        p.drawLine(QPointF(x_play, r.top() + 1), QPointF(x_play, mid))
        p.setPen(Qt.NoPen)
        p.setBrush(PLAYHEAD)
        p.drawRoundedRect(tag, 2, 2)
        p.setPen(QColor(255, 255, 255))
        p.drawText(tag, Qt.AlignCenter, text)


class AudioPlaybar(QWidget):
    """Equalizer + timeline + [play/pause] [time] / duration, driving a QMediaPlayer."""

    def __init__(self, player):
        super().__init__()
        self.player = player
        self.eq = Equalizer()
        self.timeline = TimeBar()
        self.play = QToolButton(objectName="Transport", toolTip="Play / pause (Space)")
        self.play.setIcon(_icon("play"))
        self.play.setIconSize(QSize(16, 16))
        self.play.setFocusPolicy(Qt.NoFocus)
        self.field = QLineEdit(objectName="FrameField", toolTip="Time - type seconds or m:ss and press Enter")
        self.field.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9:.,]{0,10}"), self.field))
        self.field.setFixedWidth(self.fontMetrics().horizontalAdvance("00:00.00") + 14)
        self.field.setAlignment(Qt.AlignCenter)
        self.total = QLabel(objectName="TextLabel")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(self.play)
        row.addSpacing(4)
        row.addWidget(self.field)
        row.addWidget(self.total, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        lay.addWidget(self.eq)
        lay.addWidget(self.timeline)
        lay.addLayout(row)
        self.setStyleSheet("QToolButton#Transport { background: #3a3a3a; border: 1px solid #222; border-radius: 2px;"
                           " padding: 3px 5px; } QToolButton#Transport:hover { background: #4a6a8a; }")
        self.timer = QTimer(self, interval=33)  # ~30 equalizer frames a second while playing
        self.timer.timeout.connect(lambda: self._sync(self.player.position()))
        self.play.clicked.connect(self.toggle)
        self.timeline.seek_requested.connect(self.seek)
        self.field.editingFinished.connect(self._typed)
        player.positionChanged.connect(lambda pos: None if self.timer.isActive() else self._sync(pos))
        player.durationChanged.connect(lambda _: self._sync(self.player.position()))
        player.playbackStateChanged.connect(self._state)

    def showEvent(self, event):
        # the timeline is exactly as high as the text fields (font + style), like the rulers - at any UI scale
        self.timeline.setFixedHeight(self.field.sizeHint().height())
        super().showEvent(event)

    def set_file(self, path):
        self.eq.set_samples(decode(path) if path else array("h"))
        self._sync(0)

    def seek(self, seconds):
        self.player.setPosition(int(seconds * 1000))
        self._sync(int(seconds * 1000))

    def step(self, seconds):
        self.seek(max(0.0, self.player.position() / 1000 + seconds))

    def toggle(self):
        from PySide6.QtMultimedia import QMediaPlayer
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _playing(self):
        from PySide6.QtMultimedia import QMediaPlayer
        return self.player.playbackState() == QMediaPlayer.PlayingState

    def _state(self, _state):
        playing = self._playing()
        self.play.setIcon(_icon("pause" if playing else "play"))
        self.timer.start() if playing else self.timer.stop()
        self._sync(self.player.position())

    def _sync(self, pos):
        duration = self.player.duration() / 1000
        self.timeline.set_time(duration, pos / 1000)
        self.eq.show_at(pos / 1000, self._playing())
        if not self.field.hasFocus():
            self.field.setText(clock(pos / 1000))
        self.total.setText(f"/ {clock(duration)}" if duration else "")

    def _typed(self):
        text = self.field.text().replace(",", ".").strip()
        minutes, _, seconds = text.rpartition(":")
        try:
            t = (float(minutes) * 60 if minutes else 0.0) + float(seconds or 0)
        except ValueError:
            return
        self.player.pause()
        self.seek(t)
