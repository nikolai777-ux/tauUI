"""The Edit Media node's timeline for a video or a sound: the playhead, the transport, and the trim - the same for both.

Trim: drag the yellow [ and ] brackets on the timeline (double-click one = back to the very start / end). What is
kept is tinted, what is cut away darkened; one drag = one undo step. While a bracket is dragged the playhead goes
with it (a video's picture shows the frame the cut is at).
Sound fades: a handle on the timeline's top edge at each bracket; drag it inwards and a ramp shows the fade
(double-click it: no fade). Fades are seconds of the sound going out, so they are drawn stretched by the speed.
Play renders the edit if it isn't up to date, then plays it (looping) - a video with its sound in place of the
picture, a sound through the equalizer - while the playhead follows along on the source's timeline. Pause, a click
on the timeline, or a changed setting stops it (a sound re-renders at once and plays on).
Video: the timeline drives the picture above it (the edited frame at the playhead, made by ffmpeg as you go);
first / prev / play / next / last, frame field. Sound: first / play / last, time field.
With the timeline focused: Space = play / pause; a video's Left / Right = one frame.
"""

import math

from PySide6.QtCore import QRegularExpression, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QRegularExpressionValidator
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QToolButton, QVBoxLayout, QWidget

from . import undo
from .audiobar import Equalizer, TimeBar, clock, decode
from .node import AUDIO, VIDEO
from .playbar import FrameBar, _icon

STYLE = ("QToolButton#Transport { background: #3a3a3a; border: 1px solid #222; border-radius: 2px; padding: 3px 5px; }"
         " QToolButton#Transport:hover { background: #4a6a8a; }")


class MediaTimeline(QWidget):
    def __init__(self, node, parent):
        super().__init__(parent)  # parented up front: showing itself must never make a (briefly flashing) window
        self.node = node
        node.playing = False  # a new view: nothing of an older one is playing any more
        if not hasattr(node, "position"):
            node.position = 0.0  # a sound's playhead (source seconds); a video's is node.frame
        self._pending = None       # the undo step of a bracket / fade drag
        self._play_wanted = False  # Play pressed while the result was being made
        self._kind = None          # which timeline is built (video / audio)
        self._eq_samples = None    # path whose samples the equalizer shows
        self.player = None         # plays the result (made on the first Play)
        self.screen = None         # a video's picture while playing
        self.setStyleSheet(STYLE)
        self.setFocusPolicy(Qt.ClickFocus)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        # sound: the equalizer above its timeline
        self.eq = Equalizer()
        lay.addWidget(self.eq)
        # the timelines (one shown)
        self.frames_bar = FrameBar()
        self.frames_bar.setToolTip("Click or drag: show a frame · drag the yellow brackets to trim (double-click one:"
                                   " back to the start / end) · wheel: one frame")
        self.frames_bar.frame_requested.connect(self._go)
        self.time_bar = TimeBar()
        self.time_bar.setToolTip("Click or drag to go there · drag the yellow brackets to trim, the small handles on"
                                 " the top edge to fade in / out (double-click: back / no fade)")
        self.time_bar.seek_requested.connect(self._go)
        for bar, start, end in ((self.frames_bar, "v_in", "v_out"), (self.time_bar, "a_in", "a_out")):
            bar.trim_editable = True
            bar.trim_started.connect(lambda which: self._drag_start("Trim start" if which == "in" else "Trim end"))
            bar.trim_dragged.connect(lambda which, value, s=start, e=end: self._trim_drag(s if which == "in" else e,
                                                                                        value))
            bar.trim_released.connect(self._drag_end)
            lay.addWidget(bar)
        self.time_bar.fade_started.connect(lambda which: self._drag_start("Fade in" if which == "in" else "Fade out"))
        self.time_bar.fade_dragged.connect(self._fade_drag)
        self.time_bar.fade_released.connect(self._drag_end)

        # transport
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(2)
        self.buttons = {}
        for kind, tip in (("first", "Start of the trim"), ("prev", "Previous frame (Left)"),
                          ("play", "Play the edit (Space) - made first if needed"),
                          ("next", "Next frame (Right)"), ("last", "End of the trim")):
            b = QToolButton(objectName="Transport", toolTip=tip)
            b.setIcon(_icon(kind))
            b.setIconSize(QSize(16, 16))
            b.setFocusPolicy(Qt.NoFocus)
            b.clicked.connect(lambda _=False, k=kind: self.toggle() if k == "play" else self._step(k))
            row.addWidget(b)
            self.buttons[kind] = b
        self.field = QLineEdit(objectName="FrameField")
        self.field.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9:.,]{0,10}"), self.field))
        self.field.setFixedWidth(self.fontMetrics().horizontalAdvance("00:00.00") + 14)
        self.field.setAlignment(Qt.AlignCenter)
        self.field.editingFinished.connect(self._typed)
        self.total = QLabel(objectName="TextLabel")
        self.total.setMinimumWidth(1)  # a narrow pane cuts it rather than growing sideways
        row.addSpacing(6)
        row.addWidget(self.field)
        row.addWidget(self.total, 1)
        lay.addLayout(row)
        self.timer = QTimer(self, interval=33)  # the equalizer, ~30 times a second while a sound plays
        self.timer.timeout.connect(self._tick)
        self.refresh(node)

    def showEvent(self, event):
        h = self.field.sizeHint().height()  # timelines as high as the text fields, like the rulers
        self.frames_bar.setFixedHeight(h)
        self.time_bar.setFixedHeight(h)
        super().showEvent(event)

    # ---- which kind: video / sound -----------------------------------------------------------------------------
    def _build(self, kind):
        self._kind = kind
        video = kind == VIDEO
        self.frames_bar.setVisible(video)
        self.time_bar.setVisible(not video)
        self.eq.setVisible(not video)
        self.buttons["prev"].setVisible(video)
        self.buttons["next"].setVisible(video)
        self.field.setToolTip("Frame - type a number and press Enter" if video
                              else "Time - type seconds or m:ss and press Enter")

    # ---- state from the node -------------------------------------------------------------------------------
    def refresh(self, node):
        self.node = node
        if node.kind not in (VIDEO, AUDIO):
            return
        if node.kind != self._kind:
            self._stop_play()
            self._build(node.kind)
        if node.playing and node.stale():  # a setting changed under it: a sound re-renders at once and plays on
            self._stop_play()
            self._play_wanted = node.kind == AUDIO
        if self._play_wanted and not node.rendering() and not (node.kind == AUDIO and node._timer.isActive()):
            self._play_wanted = False
            if node.params["result"] and not node.stale():
                self._start_play()
        if node.kind == VIDEO:
            frames = node.frames()
            self.frames_bar.trim = node.trim_frames() if frames else None
            self.frames_bar.set_frames(frames, node.frame)
            self.frames_bar.update()
        else:
            info = node.media_info()
            duration = info.get("duration", 0.0)
            self.time_bar.trim = node.trim_seconds() if duration else None
            speed = node.speed()
            self.time_bar.fades = (node.params["fade_in"] * speed, node.params["fade_out"] * speed) if duration else None
            if not node.playing:
                self._show_sound(node.source(), node.position, False)
            self.time_bar.set_time(duration, node.position)
            self.time_bar.update()
        self._show_position()

    def _show_position(self):
        n = self.node
        if not self.field.hasFocus():
            if n.kind == VIDEO:
                self.field.setText(str(n.frame) if n.frames() else "")
            else:
                self.field.setText(clock(n.position))
        if self._play_wanted:
            self.total.setText("making the edit…")
        elif n.kind == VIDEO:
            frames = n.frames()
            info = n.media_info()
            fps = f"{info['fps']:g} fps" if info.get("fps_known") else "fps not recorded (24 assumed)"
            self.total.setText(f"/ {frames}  ·  {fps}" if frames else "no frames found")
        else:
            d = n.media_info().get("duration", 0.0)
            self.total.setText(f"/ {clock(d)}" if d else "")

    def _show_sound(self, path, seconds, playing):
        """The equalizer: the spectrum of `path` at `seconds`."""
        if path != self._eq_samples:
            self._eq_samples = path
            self.eq.set_samples(decode(path))
        self.eq.show_at(seconds, playing)

    # ---- trim brackets and fades ------------------------------------------------------------------------------
    def _drag_start(self, text):
        self._stop_play()
        self._pending = undo.Pending(self.node, text)

    def _trim_drag(self, name, value):
        n = self.node
        if n.params[name] != value:
            n.set_param(name, value)
        self._go(value)  # the playhead goes with the bracket (a video's picture: the frame the cut is at)

    def _fade_drag(self, which, seconds):
        n = self.node
        name = "fade_in" if which == "in" else "fade_out"
        value = round(seconds / n.speed(), 3)  # the timeline's seconds -> seconds going out
        if n.params[name] != value:
            n.set_param(name, value)

    def _drag_end(self):
        pending, self._pending = self._pending, None
        if pending is not None:
            pending.finish()

    # ---- moving the playhead ------------------------------------------------------------------------------
    def _go(self, where):
        """A frame (video) or source seconds (sound)."""
        self._stop_play()
        n = self.node
        if n.kind == VIDEO:
            n.set_frame(where)
        else:
            n.position = max(0.0, min(n.media_info().get("duration", 0.0), float(where)))
            self.refresh(n)

    def _step(self, step):
        n = self.node
        if n.kind == VIDEO:
            first, last = n.trim_frames()
            self._go(first if step == "first" else last if step == "last"
                     else n.frame + (-1 if step == "prev" else 1))
        else:
            start, end = n.trim_seconds()
            self._go(start if step == "first" else end)

    def _typed(self):
        text = self.field.text().replace(",", ".").strip()
        if not text:
            return
        if self.node.kind == VIDEO:
            if text.isdigit():
                self._go(int(text))
            return
        minutes, _, seconds = text.rpartition(":")
        try:
            self._go((float(minutes) * 60 if minutes else 0.0) + float(seconds or 0))
        except ValueError:
            pass

    def keyPressEvent(self, event):
        n = self.node
        if n.kind == VIDEO and event.key() in (Qt.Key_Left, Qt.Key_Right):
            self._step("prev" if event.key() == Qt.Key_Left else "next")
        elif n.kind in (VIDEO, AUDIO) and event.key() == Qt.Key_Space:
            self.toggle()
        else:
            return super().keyPressEvent(event)
        event.accept()

    # ---- playing the edit --------------------------------------------------------------------------------
    def toggle(self):
        n = self.node
        if n.playing:
            self._stop_play()
            return
        if not n.source() or n.kind not in (VIDEO, AUDIO):
            return
        if n.stale() or not n.params["result"]:
            self._play_wanted = True  # plays when the edit is made (refresh)
            if not n.rendering():
                if n.kind == VIDEO:
                    n.action("render")
                else:
                    n._render_async()
            self._show_position()
            return
        self._start_play()

    def _make_player(self):
        from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
        from PySide6.QtMultimediaWidgets import QVideoWidget
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(QAudioOutput(self))
        self.player.setLoops(QMediaPlayer.Infinite)
        self.player.positionChanged.connect(self._played_to)
        self.screen = QVideoWidget()
        from .param_panel import no_focus
        no_focus(self.screen, self)
        self.screen.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.screen.setVisible(False)
        self.player.setVideoOutput(self.screen)
        self.layout().insertWidget(0, self.screen)

    def _start_play(self):
        n = self.node
        if self.player is None:
            self._make_player()
        path = n.params["result"]
        if self.player.source() != QUrl.fromLocalFile(path):
            self.player.setSource(QUrl.fromLocalFile(path))
        if n.kind == VIDEO:
            first, last = n.trim_frames()
            frame = n.frame if first <= n.frame < last else first
            at = (frame - first) / n.media_info().get("fps", 24.0) / n.speed()
            w, h = n.params["width"] or 16, n.params["height"] or 9
            self.screen.setFixedHeight(max(60, int(self.width() * min(h / w, 1.25))))
            self.screen.setVisible(True)
        else:
            start, end = n.trim_seconds()
            at = (n.position - start) / n.speed() if start <= n.position < end else 0.0
            self.timer.start()
        self.player.setPosition(int(at * 1000))
        n.playing = True
        n.notify()  # a video's picture steps aside
        self.player.play()
        self.buttons["play"].setIcon(_icon("pause"))

    def _stop_play(self):
        n = self.node
        self._play_wanted = False
        self.timer.stop()
        if self.player is not None:
            self.player.pause()
            self.screen.setVisible(False)
        self.buttons["play"].setIcon(_icon("play"))
        if n.playing:
            n.playing = False
            if n.kind == VIDEO:
                n._schedule_frame()  # the frame view comes back where it stopped
            n.notify()

    def _played_to(self, ms):
        """The result's position -> the playhead on the source's timeline."""
        n = self.node
        if not n.playing:
            return
        if n.kind == VIDEO:
            first, last = n.trim_frames()
            fps = n.media_info().get("fps", 24.0)
            n.frame = min(last, first + math.floor(ms / 1000 * n.speed() * fps + 1e-6))  # no frame render meanwhile
            self.frames_bar.set_frames(n.frames(), n.frame)
        else:
            start, end = n.trim_seconds()
            n.position = min(end, start + ms / 1000 * n.speed())
            self.time_bar.set_time(n.media_info().get("duration", 0.0), n.position)
        self._show_position()

    def _tick(self):
        if self.node.playing and self.player is not None:
            self._show_sound(self.node.params["result"], self.player.position() / 1000, True)
