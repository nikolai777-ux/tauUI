"""Edit Media node: changes an image, a video or a sound; it shows the settings for whatever is plugged in.

Images: crop (a box drawn on the picture), rotate / flip (icons), resize (Width / Height start at the size coming
in - after crop and rotation - and follow it until changed; the lock keeps the proportions), exposure, contrast,
saturation, gamma, sharpen (a negative amount blurs) - in that order - and drawing on top.
Videos: the same picture edits (the picture shows the edited frame at the playhead, live; drawing is burned into
every frame), trimmed with brackets on the timeline, speed and frame rate (linked: every frame is kept, so 30 fps
at speed 0.5 goes out as 15 fps), and the sound kept (with the audio edits below) or muted. A video is only
rendered on demand (Render / Play, or when something downstream needs it).
Sounds: trim (brackets on the timeline), noise reduction, speed (pitch kept), volume, loudness normalization,
fade in / out (handles on the timeline), channels and sample rate - in that order. Written as 16-bit WAV (what
speech models read).
ffmpeg does all of it. The result is an instance (media_cache.py): images and sounds are re-made in the background
shortly after each change (a quick burst of changes makes one render); if something downstream needs the result
while it's stale, it is made on the spot, so nobody gets an outdated one. Several Edit Media nodes can hang off one
source - each keeps its own instance.
"""

import math
import os
import re
import shutil
import subprocess

from PySide6.QtCore import QProcess, QTimer
from PySide6.QtGui import QColor

from . import media_cache, spans
from .node import ANY, AUDIO, IMAGE, JSON, VIDEO, Node, Param, set_hints
from .registry import register_node

IN = "in"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".tga"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".ogg", ".m4a"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _is(*kinds):
    return lambda n: n.kind in kinds


ROTATIONS = [("0", "none"), ("90", "90° clockwise"), ("180", "180°"), ("270", "90° counter-clockwise")]
FLIPS = [("none", "none"), ("h", "horizontal (mirror)"), ("v", "vertical"), ("hv", "both")]
NORMALIZE = [("", "off"), ("-14", "−14 LUFS · streaming"), ("-16", "−16 LUFS · speech"), ("-23", "−23 LUFS · broadcast")]
DENOISE = [("", "off"), ("12", "light"), ("24", "strong")]  # afftdn noise reduction in dB
CHANNELS = [("", "keep"), ("1", "mono"), ("2", "stereo")]
RATES = [("", "keep")] + [(str(r), f"{r / 1000:g} kHz") for r in (16000, 22050, 24000, 32000, 44100, 48000)]
SOUND = [("keep", "keep"), ("mute", "mute")]

PICTURE_KEYS = ("crop_left", "crop_top", "crop_right", "crop_bottom", "rotate", "flip", "width", "height",
                "exposure", "contrast", "saturation", "gamma", "sharpen", "strokes")
IMAGE_KEYS = PICTURE_KEYS
AUDIO_KEYS = ("a_in", "a_out", "denoise", "speed", "volume", "normalize", "fade_in", "fade_out", "channels", "rate")
VIDEO_KEYS = PICTURE_KEYS + ("v_in", "v_out", "speed", "fps", "sound", "denoise", "volume", "normalize")
ALL_KEYS = tuple(dict.fromkeys(IMAGE_KEYS + AUDIO_KEYS + VIDEO_KEYS))
EXT = {IMAGE: ".png", VIDEO: ".mp4", AUDIO: ".wav"}


def _drawing(n):
    return n.kind in (IMAGE, VIDEO) and n.params["tools"] == "draw"


def _atempo(speed):
    """atempo takes 0.5..2 per filter: chain several for other speeds (the pitch stays)."""
    out = []
    while speed > 2.0:
        out.append("atempo=2")
        speed /= 2.0
    while speed < 0.5:
        out.append("atempo=0.5")
        speed /= 0.5
    if abs(speed - 1.0) > 1e-6:
        out.append(f"atempo={speed:.6g}")
    return out


@register_node
class EditMediaNode(Node):
    type_name = "edit_media"
    label = "Edit Media"
    shape_name = "pencil"
    HEIGHT = 54  # the box is the Prompt node's size (100 x 42); the pencil sticks out above it
    color = QColor(80, 80, 60)
    inputs = [(IN, ANY, False)]
    outputs = [("out", ANY)]
    info_title = "Edit Media"
    parameters = [
        # shots / chunks plugged in (Chunk node, a shot detector's Modality): which one to edit, and how many frames
        # of the film around it the clip keeps (so the trim can also reach past the span, not only into it)
        Param("shot", "Shot", "int", 1, step=1, minimum=1, maximum=999, visible=lambda n: n.span_unit() == "shot"),
        Param("chunk", "Chunk", "int", 1, step=1, minimum=1, maximum=9999,
              visible=lambda n: n.span_unit() == "chunk"),
        Param("handles", "Handles", "int", 12, step=1, minimum=0, maximum=240,
              visible=lambda n: n.span_unit() is not None),
        Param("span_preset", "Span preset", "data", None),  # {"id": span the trim was set for, "a": clip's 1st frame}
        # image / video: one row of icons (crop / draw mode, rotate, flip, reset); the picture
        Param("tools", "Tools", "toolbar", "", visible=_is(IMAGE, VIDEO), options=[
            ("mode:crop", "crop", "Crop: drag a box on the picture, then Enter (Esc cancels; double-click: no crop)"),
            ("mode:draw", "pen", "Draw on the picture (it goes downstream with it; on a video, on every frame)"),
            ("rotate_left", "rotate_left", "Rotate 90° left"), ("rotate_right", "rotate_right", "Rotate 90° right"),
            ("flip_h", "flip_h", "Mirror (flip left-right)"), ("flip_v", "flip_v", "Flip upside down"),
            ("reset", "reset", "Reset all edits")]),
        Param("audio_tools", "Tools", "toolbar", "", visible=_is(AUDIO), options=[
            ("reset", "reset", "Reset all edits")]),
        # draw mode: the pen, and undo / clear
        Param("pen_color", "Pen colour", "color", "#ff3b30", visible=_drawing),
        Param("pen_width", "Pen width", "int", 8, visible=_drawing, step=1, minimum=1, maximum=100),
        Param("draw_tools", "Drawing", "toolbar", "", visible=_drawing, options=[
            ("undo_stroke", "undo", "Undo the last stroke"), ("clear_strokes", "clear", "Clear the drawing")]),
        Param("strokes", "Strokes", "data", None),  # [{c: colour, w: width, p: [(x, y)...]}], relative to the picture
        # the picture: an image's result, or a video's edited frame at the playhead
        Param("result", "Result", "media_picture", "", visible=_is(IMAGE, VIDEO)),
        # the timeline: playhead, transport, and the trim - brackets dragged on it
        Param("trim", "Trim", "media_timeline", None, visible=_is(VIDEO, AUDIO)),
        Param("render", "Render", "button", None, visible=_is(VIDEO)),
        Param("export", "Export…", "button", None,
              visible=lambda n: n.kind in (IMAGE, VIDEO, AUDIO) and bool(n.params["result"])),
        # set by the crop box and the icons (kept as numbers, not shown as fields)
        Param("crop_left", "Crop left", "int", 0, visible=lambda n: False),
        Param("crop_right", "Crop right", "int", 0, visible=lambda n: False),
        Param("crop_top", "Crop top", "int", 0, visible=lambda n: False),
        Param("crop_bottom", "Crop bottom", "int", 0, visible=lambda n: False),
        Param("rotate", "Rotate", "choice", "0", options=ROTATIONS, visible=lambda n: False),
        Param("flip", "Flip", "choice", "none", options=FLIPS, visible=lambda n: False),
        # trim (the brackets on the timeline): frames of a video (from 1, as on the timeline), seconds of a sound
        Param("v_in", "Start frame", "int", 1, visible=lambda n: False),
        Param("v_out", "End frame", "int", 0, visible=lambda n: False),
        Param("a_in", "Start (s)", "float", 0.0, visible=lambda n: False),
        Param("a_out", "End (s)", "float", 0.0, visible=lambda n: False),
        # the size going out: starts at the size coming in (after crop / rotate) and follows it until changed
        Param("keep_aspect", "Keep proportions", "lock", True, visible=_is(IMAGE, VIDEO)),
        Param("width", "Width", "int", 0, visible=_is(IMAGE, VIDEO), step=1, minimum=1, maximum=16384),
        Param("height", "Height", "int", 0, visible=_is(IMAGE, VIDEO), step=1, minimum=1, maximum=16384),
        # a video's speed and frame rate are linked: every frame is kept (30 frames at 10 fps = 3x slower)
        # a video's speed IS its frame rate (every frame kept: speed = frame rate / the source's) - only Frame rate is
        # shown for video; a sound (no frames) has its Speed
        Param("speed", "Speed", "float", 1.0, visible=_is(AUDIO), step=0.05, minimum=0.1, maximum=4.0),
        Param("fps", "Frame rate", "float", 0.0, visible=_is(VIDEO), step=1.0, minimum=1.0, maximum=120.0),
        Param("exposure", "Exposure", "float", 0.0, visible=_is(IMAGE, VIDEO), step=0.05, minimum=-4.0, maximum=4.0),
        Param("contrast", "Contrast", "float", 1.0, visible=_is(IMAGE, VIDEO), step=0.05, minimum=0.0, maximum=3.0),
        Param("saturation", "Saturation", "float", 1.0, visible=_is(IMAGE, VIDEO), step=0.05, minimum=0.0, maximum=3.0),
        Param("gamma", "Gamma", "float", 1.0, visible=_is(IMAGE, VIDEO), step=0.05, minimum=0.1, maximum=3.0),
        Param("sharpen", "Sharpen", "float", 0.0, visible=_is(IMAGE, VIDEO), step=0.1, minimum=-2.0, maximum=3.0),
        # sound (a video's own sound, or an audio file)
        Param("sound", "Sound", "choice", "keep", options=SOUND, visible=_is(VIDEO)),
        Param("denoise", "Noise reduction", "choice", "", options=DENOISE,
              visible=lambda n: n.kind == AUDIO or (n.kind == VIDEO and n.params["sound"] == "keep")),
        Param("volume", "Volume (dB)", "float", 0.0, step=0.5, minimum=-30.0, maximum=30.0,
              visible=lambda n: n.kind == AUDIO or (n.kind == VIDEO and n.params["sound"] == "keep")),
        Param("normalize", "Normalize", "choice", "", options=NORMALIZE,
              visible=lambda n: n.kind == AUDIO or (n.kind == VIDEO and n.params["sound"] == "keep")),
        # fades: seconds of the sound going out, set by the handles on the timeline
        Param("fade_in", "Fade in (s)", "float", 0.0, visible=lambda n: False),
        Param("fade_out", "Fade out (s)", "float", 0.0, visible=lambda n: False),
        Param("channels", "Channels", "choice", "", options=CHANNELS, visible=_is(AUDIO)),
        Param("rate", "Sample rate", "choice", "", options=RATES, visible=_is(AUDIO)),
    ]

    def __init__(self, name):
        super().__init__(name)
        self.kind = ""            # image / video / audio - from what is plugged in
        self.error = ""
        self.frame = 1            # a video's playhead (the frame the picture shows)
        self.playing = False      # a video's result is playing in place of the picture (edit_media_timeline.py)
        self._key = None          # settings the current instance was made with
        self._wanted = None       # settings of the latest request
        self._jobs = {}           # slot ("result" / "frame" / "span") -> running QProcess
        self._span_clip = ("", None)  # a shot / chunk plugged in: (clip cut around it, its plan key)
        self._span_wanted = None
        self._natural = None      # the size coming in (after crop / rotate), which Width / Height follow
        self._quiet = False       # setting values ourselves (no follow-up reactions)
        self._sizes = {}          # source path -> (w, h)
        self._media = {}          # (source path, mtime) -> facts about a video / sound (media_info)
        self._frame_png, self._frame_key, self._frame_wanted = "", None, None
        self._timer = QTimer(singleShot=True, interval=120)
        self._timer.timeout.connect(self._render_async)
        self._frame_timer = QTimer(singleShot=True, interval=60)
        self._frame_timer.timeout.connect(self._render_frame)

    # ---- what is plugged in -------------------------------------------------------------------------------
    def source(self):
        if self.span_data() is not None:  # a shot / chunk: the clip cut around it (once it's made)
            path, key = self._span_clip
            return path if key is not None and key == self._span_plan_key() else ""
        values = [v for v in self.input_values(IN) if isinstance(v, str) and v]
        return values[0] if values else ""

    # ---- a shot / chunk as the source (spans.py) ------------------------------------------------------------
    def span_data(self):
        """The .shots / .chunks data plugged in, or None (an ordinary image / video / sound, or nothing)."""
        sock = self.input_sockets[0]
        if not sock.edges:
            return None
        values = [v for v in self.input_values(IN) if isinstance(v, str) and v]
        if not values:
            return None
        return spans.load(values[0])  # shots / chunks by what the JSON says (None for a picture, video, sound)

    def span_unit(self):
        data = self.span_data()
        return spans.unit(data) if data is not None else None

    def span(self):
        data = self.span_data()
        return spans.pick(data, self.params[spans.unit(data)]) if data is not None else None

    def _span_plan(self):
        """(video, clip's first frame, clip's last frame, fps, span) - the span plus its handles, within the film."""
        data, s = self.span_data(), self.span()
        video = spans.analysed_video(self, IN)
        if not s or not video or not os.path.isfile(video):
            return None
        h = max(0, int(self.params["handles"] or 0))
        total = int(data.get("frames") or s["last"])
        return video, max(1, s["first"] - h), min(total, s["last"] + h), float(data["fps_value"]), s

    def _span_plan_key(self):
        plan = self._span_plan()
        if plan is None:
            return None
        video, a, b, fps, _s = plan
        return video, os.path.getmtime(video), a, b, fps

    def _update_span(self, wait=False):
        """Cut the clip for the chosen span (in the background, or now when something needs it at once)."""
        key = self._span_plan_key()
        if key is None or key == self._span_clip[1] or (not wait and key == self._span_wanted and "span" in self._jobs):
            return
        video, _mtime, a, b, fps = key
        out = media_cache.path_for(self, ("span",) + key, ".mp4")
        args = spans.cut_command(video, VIDEO, a, b, fps, out)
        self._span_wanted = key
        if wait:
            self._stop("span")
            ffmpeg = shutil.which("ffmpeg")
            p = subprocess.run([ffmpeg] + args, capture_output=True, text=True, creationflags=NO_WINDOW)
            self._span_ready(key, out, p.returncode == 0 and os.path.isfile(out), p.stderr.strip())
        else:
            self._start("span", args, lambda ok, err, k=key, o=out: self._span_ready(k, o, ok and os.path.isfile(o), err))
            self.notify()

    def _span_ready(self, key, out, ok, err=""):
        if not ok:
            self.error = "ffmpeg: " + (err.splitlines()[-1] if err else "couldn't cut the span")
            self.notify()
            return
        old = self._span_clip[0]
        if old and old != out:
            media_cache.release(old)
        self._span_clip = (out, key)
        s, a, b = self.span(), key[2], key[3]
        span_id = [spans.unit(self.span_data()), s[spans.unit(self.span_data())], s["first"], s["last"]]
        preset = self.params["span_preset"] or {}
        self._quiet = True
        if preset.get("id") != span_id:  # another span: the trim brackets start on its own frames
            self.params["v_in"], self.params["v_out"] = s["first"] - a + 1, s["last"] - a + 1
        elif preset.get("a") != a:  # same span, other handles: the trim stays on the same frames of the film
            shift = preset["a"] - a
            n = b - a + 1
            self.params["v_in"] = max(1, min(n, int(self.params["v_in"] or 1) + shift))
            self.params["v_out"] = max(self.params["v_in"], min(n, int(self.params["v_out"] or n) + shift))
        self.params["span_preset"] = {"id": span_id, "a": a}
        self._followed = {**getattr(self, "_followed", {}), "v_out": b - a + 1}  # the trim above isn't "following"
        self._quiet = False
        self.error = ""
        self.frame = int(self.params["v_in"] or 1)
        self.upstream_changed()
        self.notify()

    def span_source_frames(self):
        """(first, last) frames of the film going out - the trim mapped back through the clip - or None."""
        preset = self.params["span_preset"] or {}
        if self.span_data() is None or "a" not in preset or not self.source():
            return None
        first, last = self.trim_frames()
        return preset["a"] + first - 1, preset["a"] + last - 1

    def _detect_kind(self):
        if self.span_data() is not None:
            return VIDEO
        sock = self.input_sockets[0]
        data_type = sock.edges[0].source.type if sock.edges else ANY
        if data_type in (IMAGE, VIDEO, AUDIO):
            return data_type
        ext = os.path.splitext(self.source())[1].lower()  # e.g. through a Merge: go by the file itself
        return IMAGE if ext in IMAGE_EXTS else VIDEO if ext in VIDEO_EXTS else AUDIO if ext in AUDIO_EXTS else ""

    def accepts(self, socket, source):
        """One image, sound or video: a source of that type - not a Merge's list, not text or a model. A Modality
        that hasn't rendered yet is taken too (a chain can be built before it runs); its result is edited once it
        exists - a text result is then not used."""
        from .modality import ModalityNode
        if source.node.output_is_list:
            return False
        # another Edit Media always gives a picture, video or sound - even before it knows which (nothing plugged in);
        # shots / chunks (JSON): one of them is edited
        return (source.type in (IMAGE, VIDEO, AUDIO, JSON) or isinstance(source.node, EditMediaNode)
                or (source.type == ANY and isinstance(source.node, ModalityNode)))

    def upstream_changed(self):
        if self.span_data() is not None:
            self._update_span()
        kind = self._detect_kind()
        if kind != self.kind:
            self.kind = kind
            self.output_sockets[0].set_type({"image": IMAGE, "video": VIDEO, "audio": AUDIO}.get(kind, ANY))
            self.notify()
        self._follow_size()
        self._follow_media()
        self._schedule()

    def _follow_media(self):
        """End frame / frame rate (video) and End (sound) show the source's own until changed, like the size:
        a value still equal to the previous source's (or 0, older graphs) follows a new source."""
        if self.kind not in (VIDEO, AUDIO) or not self.source():
            return
        info = self.media_info()
        if self.kind == VIDEO:
            now = {"v_out": info.get("frames") or 0, "fps": round(info.get("fps") or 0, 3)}
        else:
            now = {"a_out": round(info.get("duration") or 0, 3)}
        old = getattr(self, "_followed", {})
        changed = False
        self._quiet = True
        for name, value in now.items():
            if value and name not in self.links and (not self.params[name] or self.params[name] == old.get(name)):
                if self.params[name] != value:
                    self.params[name] = value
                    changed = True
        if self.kind == VIDEO and self.params["fps"] and now["fps"]:
            self.params["speed"] = round(self.params["fps"] / now["fps"], 4)  # linked (see param_changed)
        self._quiet = False
        self._followed = {**old, **now}
        if changed:
            self.notify()

    def media_info(self):
        """A video's / sound's facts (ffprobe): frames, fps, duration, whether a video has sound, sample rate."""
        src = self.source()
        try:
            key = (src, os.path.getmtime(src), self.kind)
        except OSError:
            return {}
        if key not in self._media:
            from .nodes import probe
            from .playbar import stream_frames
            data = probe(src)
            audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
            try:
                duration = float(data.get("format", {}).get("duration") or 0)
            except ValueError:
                duration = 0.0
            frames, fps = stream_frames(src) if self.kind == VIDEO else (0, 0.0)
            self._media[key] = {"duration": duration, "frames": frames, "fps": fps or 24.0, "fps_known": bool(fps),
                                "has_audio": audio is not None,
                                "sample_rate": int(audio.get("sample_rate") or 0) if audio else 0}
        return self._media[key]

    def frames(self):
        return self.media_info().get("frames", 0) if self.kind == VIDEO else 0

    def param_changed(self, name):
        if name == "tools" and not isinstance(self.params["tools"], str):
            self.params["tools"] = ""  # older graphs stored crop mode as True / False
        if self._quiet:
            return
        if name in ("shot", "chunk", "handles"):
            self._update_span()
            return
        if name in ("width", "height") and self.params["keep_aspect"] and self.natural():
            nw, nh = self.natural()  # the lock: the other side keeps the proportions coming in
            self._quiet = True
            if name == "width" and self.params["width"] and "height" not in self.links:
                self.params["height"] = max(1, round(self.params["width"] * nh / nw))
            elif name == "height" and self.params["height"] and "width" not in self.links:
                self.params["width"] = max(1, round(self.params["height"] * nw / nh))
            self._quiet = False
        if name in ("speed", "fps") and self.kind == VIDEO:
            src = self.media_info().get("fps")  # linked: every frame kept, so speed = frame rate / source's
            if src:
                self._quiet = True
                if name == "speed":
                    self.params["fps"] = round(src * self.params["speed"], 3)
                else:
                    self.params["speed"] = round(self.params["fps"] / src, 4)
                self._quiet = False
        if name in ("crop_left", "crop_top", "crop_right", "crop_bottom", "rotate"):
            self._follow_size()
        if name in ALL_KEYS or name == "tools":
            self._schedule()
        elif name == "result" and self.params[name] and not os.path.isfile(self.params[name]):
            self.params[name] = ""  # an instance from an earlier session (the cache is cleared on exit)

    # ---- size -------------------------------------------------------------------------------------------------
    def source_size(self):
        """The picture's size coming in: an image's, or a video's frames (as ffmpeg shows them - turned upright
        if the file says it was filmed on its side)."""
        src = self.source()
        if src and src not in self._sizes:
            from PySide6.QtGui import QImageReader
            path = src
            if self.kind == VIDEO:
                path = self._grab_frame(1, filters=False)
            size = QImageReader(path).size() if path else None
            self._sizes[src] = (size.width(), size.height()) if size is not None and size.isValid() else None
        return self._sizes.get(src)

    def natural(self):
        """The size coming in to the resize step: the source after crop and rotation."""
        if self.kind not in (IMAGE, VIDEO):
            return None
        size = self.source_size()
        if not size:
            return None
        p = self.params
        w = max(1, size[0] - p["crop_left"] - p["crop_right"])
        h = max(1, size[1] - p["crop_top"] - p["crop_bottom"])
        return (h, w) if p["rotate"] in ("90", "270") else (w, h)

    def _follow_size(self):
        """Width / Height follow the size coming in until the user changes them (0 = follow, older graphs)."""
        nat = self.natural()
        if not nat or nat == self._natural:
            return
        old, self._natural = self._natural, nat
        w, h = self.params["width"], self.params["height"]
        following = (not w and not h) or (w, h) == old or bool(old and (w, h) == (old[1], old[0]))
        swapped = bool(old and nat == (old[1], old[0]))  # turned 90°: a chosen size turns with the picture
        if "width" in self.links or "height" in self.links:
            return  # a linked size follows its source, not the picture
        if not following and not (w and h):  # one side chosen before the picture came (e.g. by the Executor)
            self._quiet = True
            if w:
                self.params["height"] = max(1, round(w * nat[1] / nat[0]))
            else:
                self.params["width"] = max(1, round(h * nat[0] / nat[1]))
            self._quiet = False
            self.notify()
        elif following or swapped:
            self._quiet = True
            self.params["width"], self.params["height"] = nat if following else (h, w)
            self._quiet = False
            self.notify()
        elif self.params["keep_aspect"] and w:  # a chosen width keeps the new proportions (e.g. after a crop)
            self._quiet = True
            self.params["height"] = max(1, round(w * nat[1] / nat[0]))
            self._quiet = False
            self.notify()

    def set_crop(self, left, top, right, bottom):
        """From the crop box: all four margins at once (one edit)."""
        self._quiet = True
        for k, v in zip(("crop_left", "crop_top", "crop_right", "crop_bottom"), (left, top, right, bottom)):
            self.params[k] = max(0, int(v))
        self._quiet = False
        self._follow_size()
        self._schedule()
        self.notify()

    # ---- trim -------------------------------------------------------------------------------------------------
    def trim_frames(self):
        """A video's (first, last) frame going out, 1-based and inclusive."""
        n = self.frames() or 1
        first = max(1, min(n, int(self.params["v_in"] or 1)))
        last = int(self.params["v_out"] or 0)
        last = n if last <= 0 or last > n else last
        return first, max(first, last)

    def trim_seconds(self):
        """A sound's (start, end) in seconds."""
        d = self.media_info().get("duration", 0.0)
        start = max(0.0, min(d, float(self.params["a_in"] or 0)))
        end = float(self.params["a_out"] or 0)
        end = d if end <= 0 or end > d else end
        return start, max(start, end)

    def set_frame(self, frame):
        """Move a video's playhead: the picture shows that frame, edited."""
        frame = max(1, min(self.frames() or 1, int(frame)))
        if frame != self.frame:
            self.frame = frame
            self._schedule_frame()
            self.notify()

    # ---- rendering ----------------------------------------------------------------------------------------
    def _settings(self):
        src = self.source()
        try:
            mtime = os.path.getmtime(src)
        except OSError:
            mtime = None
        keys = {IMAGE: IMAGE_KEYS, VIDEO: VIDEO_KEYS, AUDIO: AUDIO_KEYS}.get(self.kind, ())
        return (src, mtime, self.kind) + tuple(repr(self.params[k]) for k in keys)

    def stale(self):
        return self.kind in (IMAGE, VIDEO, AUDIO) and bool(self.source()) and self._settings() != self._key

    def _schedule(self):
        if self.kind not in (IMAGE, VIDEO, AUDIO) or not self.source():
            return
        if self.kind == VIDEO:  # a video renders on demand; the picture follows at once
            self._schedule_frame()
            return
        key = self._settings()
        if key != self._key and not (self.rendering() and key == self._wanted):  # not made, nor being made
            self._wanted = key
            self._timer.setInterval(120 if self.kind == IMAGE else 250)
            self._timer.start()

    def _picture_filters(self):
        p = self.params
        chain = []
        l, t, r, b = (max(0, int(p[k])) for k in ("crop_left", "crop_top", "crop_right", "crop_bottom"))
        if l or t or r or b:
            chain.append(f"crop=iw-{l + r}:ih-{t + b}:{l}:{t}")
        chain += {"90": ["transpose=1"], "180": ["hflip", "vflip"], "270": ["transpose=2"]}.get(p["rotate"], [])
        chain += {"h": ["hflip"], "v": ["vflip"], "hv": ["hflip", "vflip"]}.get(p["flip"], [])
        w, h = int(p["width"]), int(p["height"])
        if w and h and (w, h) != self.natural():
            # square pixels: a new aspect ratio is the picture's own, not a display flag (services such as Kling
            # refuse videos with non-square pixels, and players differ in honouring them)
            chain += [f"scale={w}:{h}:flags=lanczos", "setsar=1"]
        if abs(p["exposure"]) > 1e-6:
            chain.append(f"exposure=exposure={p['exposure']:g}")
        eq = [f"{k}={p[k]:g}" for k in ("contrast", "saturation", "gamma") if abs(p[k] - 1.0) > 1e-6]
        if eq:
            chain.append("eq=" + ":".join(eq))
        if abs(p["sharpen"]) > 1e-6:
            chain.append(f"unsharp=5:5:{p['sharpen']:g}:5:5:0")
        return chain

    def _alpha(self):
        from .nodes import probe
        pix = next((s.get("pix_fmt", "") for s in probe(self.source()).get("streams", [])
                    if s.get("codec_type") == "video"), "")
        return "a" in pix.replace("gray", "g").replace("yuva", "a")  # rgba / ya8 / bgra / yuva...: keep transparency

    def _image_command(self, out):
        # full RGB first: sources stored as 4:2:0 (most JPEGs) could otherwise only be cut to even sizes
        rgb = f"format={'rgba' if self._alpha() else 'rgb24'}"
        chain = [rgb] + self._picture_filters() + [rgb]
        return ["-v", "error", "-y", "-i", self.source(), "-vf", ",".join(chain), "-frames:v", "1", "-update", "1", out]

    def speed(self):
        """How much faster it goes out: a video's from its frame rate (linked - every frame kept), a sound's own."""
        if self.kind == VIDEO:
            src = self.media_info().get("fps") or 24.0
            return float(self.params["fps"] or src) / src
        return float(self.params["speed"] or 1.0)

    def _sound_filters(self, duration):
        """The audio edits after trimming; `duration` = seconds going in (for the fade-out's start)."""
        p = self.params
        chain = []
        if p["denoise"]:
            chain.append(f"afftdn=nr={p['denoise']}")
        speed = self.speed()
        chain += _atempo(speed)
        if abs(p["volume"]) > 1e-6:
            chain.append(f"volume={p['volume']:g}dB")
        if p["normalize"]:
            chain.append(f"loudnorm=I={p['normalize']}:TP=-1.5:LRA=11")
        out_len = duration / speed
        if self.kind == AUDIO and p["fade_in"] > 0:
            chain.append(f"afade=t=in:st=0:d={min(p['fade_in'], out_len):g}")
        if self.kind == AUDIO and p["fade_out"] > 0:
            d = min(p["fade_out"], out_len)
            chain.append(f"afade=t=out:st={max(0.0, out_len - d):g}:d={d:g}")
        return chain

    def _audio_command(self, out):
        info = self.media_info()
        start, end = self.trim_seconds()
        p = self.params
        chain = self._sound_filters(end - start)
        rate = p["rate"] or str(info.get("sample_rate") or 48000)  # loudnorm works at 192 kHz: say what comes out
        cmd = ["-v", "error", "-y", "-ss", f"{start:.6f}", "-t", f"{end - start:.6f}", "-i", self.source(), "-vn"]
        if chain:
            cmd += ["-af", ",".join(chain)]
        if p["channels"]:
            cmd += ["-ac", p["channels"]]
        return cmd + ["-ar", rate, "-c:a", "pcm_s16le", out]

    def _overlay_png(self, key):
        """The drawing as a transparent picture the size of the video going out (burned into every frame)."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QImage
        size = (int(self.params["width"]), int(self.params["height"])) if self.params["width"] else self.natural()
        if not self.params["strokes"] or not size:
            return ""
        img = QImage(size[0], size[1], QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        path = media_cache.path_for(self, ("overlay", key), ".png")
        return path if img.save(path) and self._burn_strokes(path) else ""

    def _video_command(self, out, key, progress=False):
        info = self.media_info()
        fps = info["fps"]
        first, last = self.trim_frames()
        start, length = (first - 1) / fps, (last - first + 1) / fps
        p = self.params
        rate = float(p["fps"] or fps)
        speed = rate / fps  # linked: every frame kept, shown faster or slower
        # timestamps from 0, sped up, then set to the frame rate going out: every frame, exactly once
        chain = self._picture_filters() + [f"setpts=(PTS-STARTPTS)/{speed:.6g}", f"fps={rate:.6g}"]
        out_frames = last - first + 1
        cmd = ["-v", "error", "-y"] + (["-progress", "pipe:1", "-nostats"] if progress else [])
        cmd += ["-ss", f"{max(0.0, start - 0.25 / fps):.6f}", "-t", f"{length:.6f}", "-i", self.source()]
        overlay = self._overlay_png(key)
        even = "crop=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p"  # H.264 in 4:2:0 needs even sizes
        if overlay:
            cmd += ["-i", overlay]
            graph = f"[0:v]{','.join(chain + ['format=rgba']) }[v0];[v0][1:v]overlay=0:0,{even}[v]"
        else:
            graph = f"[0:v]{','.join(chain + [even])}[v]"
        cmd += ["-filter_complex", graph, "-map", "[v]", "-frames:v", str(out_frames)]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]
        if p["sound"] == "keep" and info.get("has_audio"):
            audio = self._sound_filters(length)
            cmd += ["-map", "0:a:0"] + (["-af", ",".join(audio)] if audio else []) + ["-c:a", "aac", "-b:a", "192k"]
            cmd += ["-t", f"{out_frames / rate:.6f}"]  # the sound as long as the picture
        else:
            cmd += ["-an"]
        return cmd + ["-movflags", "+faststart", out]

    def _command(self, out, key, progress=False):
        if self.kind == VIDEO:
            return self._video_command(out, key, progress)
        if self.kind == AUDIO:
            return self._audio_command(out)
        return self._image_command(out)

    def _start(self, slot, args, on_done, on_output=None):
        """Run ffmpeg in the background in a slot ("result" / "frame"); a newer job replaces one still running."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.error = "ffmpeg isn't on PATH"
            self.notify()
            return
        self._stop(slot)
        proc = QProcess()
        proc.finished.connect(lambda code, _status, pr=proc: self._done(slot, pr, code, on_done))
        if on_output:
            proc.readyReadStandardOutput.connect(lambda pr=proc: on_output(pr))
        self._jobs[slot] = proc
        proc.start(ffmpeg, args)

    def _stop(self, slot):
        """Cancel a background job: forgotten first, so its late 'finished' signal is ignored."""
        proc = self._jobs.pop(slot, None)
        if proc is not None:
            proc.kill()
            proc.waitForFinished(1000)

    def _stop_render(self):
        for slot in list(self._jobs):
            self._stop(slot)

    def _done(self, slot, proc, code, on_done):
        from shiboken6 import isValid
        if self._jobs.get(slot) is not proc or not isValid(proc):
            return  # replaced by a newer job, or cancelled
        err = bytes(proc.readAllStandardError()).decode("utf-8", "replace").strip()
        self._jobs.pop(slot, None)
        on_done(code == 0, err)

    def _render_async(self, progress=False):
        if self.kind not in EXT or not self.source():
            return  # disconnected while the render was waiting
        key = self._settings()
        if key == self._key or (self.rendering() and key == self._wanted):
            return  # made already, or being made: a second run would rewrite the file under whoever reads it
        self._wanted = key
        out = media_cache.path_for(self, key, EXT[self.kind])
        watch = None
        if progress:
            self.progress = 0.0
            total = self._out_seconds()

            def watch(proc, total=total):  # "-progress pipe:1": out_time_us=... lines
                text = bytes(proc.readAllStandardOutput()).decode("ascii", "replace")
                times = re.findall(r"out_time_us=(\d+)", text)
                if times and total > 0:
                    self.progress = min(1.0, int(times[-1]) / 1e6 / total)
                    self.update()
        self._start("result", self._command(out, key, progress),
                    lambda ok, err, k=key, o=out: self._finish(k, o, ok and os.path.isfile(o), err), watch)
        self.notify()

    def _out_seconds(self):
        info = self.media_info()
        first, last = self.trim_frames()
        return (last - first + 1) / info.get("fps", 24.0) / self.speed()

    def rendering(self):
        return "result" in self._jobs

    def _burn_strokes(self, path):
        """Draw the strokes into a picture (Qt, antialiased, round ends)."""
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
        strokes = self.params["strokes"] or []
        if not strokes:
            return True
        img = QImage(path)
        if img.isNull():
            return False
        w, h = img.width(), img.height()
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        for st in strokes:
            pts = st["p"]
            path_ = QPainterPath(QPointF(pts[0][0] * w, pts[0][1] * h))
            for x, y in pts[1:]:
                path_.lineTo(x * w, y * h)
            p.setPen(QPen(QColor(st["c"]), max(1.0, st["w"] * w), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawPath(path_)
        p.end()
        return img.save(path)

    def add_stroke(self, points, picture_width):
        """A stroke drawn on the picture: points relative (0..1), width relative to the picture's width."""
        stroke = {"c": self.params["pen_color"], "w": self.params["pen_width"] / max(1, picture_width),
                  "p": [(round(x, 5), round(y, 5)) for x, y in points]}
        self.set_param("strokes", list(self.params["strokes"] or []) + [stroke])

    def _finish(self, key, out, ok, err=""):
        self.progress = None
        if ok and self.kind == IMAGE and not self._burn_strokes(out):
            ok, err = False, "couldn't draw the strokes into the picture"
        if ok:
            old = self.params["result"]
            self._key, self.error = key, ""
            if old and old != out:
                media_cache.release(old)
            self.set_param("result", out)  # the preview / players follow
        else:
            self.error = "ffmpeg: " + (err.splitlines()[-1] if err else "failed")
            self.notify()

    def render_now(self):
        """Make the result immediately (something downstream needs it)."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or self.kind not in (IMAGE, VIDEO, AUDIO) or not self.source():
            return
        self._timer.stop()
        self._stop("result")
        key = self._settings()
        out = media_cache.path_for(self, key, EXT[self.kind])
        p = subprocess.run([ffmpeg] + self._command(out, key), capture_output=True, text=True, creationflags=NO_WINDOW)
        self._finish(key, out, p.returncode == 0 and os.path.isfile(out), p.stderr.strip())

    # ---- a video's picture: the frame at the playhead ------------------------------------------------------
    def _grab_frame(self, frame, filters=True):
        """One frame of the video as a PNG (edited, or as it comes in), made now; '' if it can't be."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg or not self.source():
            return ""
        key = ("frame", self._settings() if filters else self.source(), frame, filters)
        out = media_cache.path_for(self, key, ".png")
        p = subprocess.run([ffmpeg] + self._frame_command(frame, out, filters), capture_output=True,
                           creationflags=NO_WINDOW)
        return out if p.returncode == 0 and os.path.isfile(out) else ""

    def _frame_command(self, frame, out, filters):
        fps = self.media_info().get("fps", 24.0)
        t = max(0.0, (frame - 1 - 0.25) / fps)  # a moment before the frame starts: the first frame at or after it
        chain = ["format=rgb24"] + (self._picture_filters() if filters else []) + ["format=rgb24"]
        return ["-v", "error", "-y", "-ss", f"{t:.6f}", "-i", self.source(), "-vf", ",".join(chain),
                "-frames:v", "1", "-update", "1", out]

    def _schedule_frame(self):
        if self.kind != VIDEO or not self.source():
            return
        crop = self.params["tools"] == "crop"  # the crop box is drawn on the frame as it comes in
        key = (self._settings(), self.frame, crop)
        if key != self._frame_key:
            self._frame_wanted = key
            self._frame_timer.start()

    def _render_frame(self):
        key = self._frame_wanted
        _settings, frame, crop = key
        out = media_cache.path_for(self, ("frame",) + key, ".png")

        def done(ok, err, key=key, out=out, crop=crop):
            if ok and os.path.isfile(out) and (crop or self._burn_strokes(out)):
                old, self._frame_png, self._frame_key = self._frame_png, out, key
                if old and old != out:
                    media_cache.release(old)
                self.error = ""
            else:
                self.error = "ffmpeg: " + (err.splitlines()[-1] if err else "couldn't show this frame")
            self.notify()
        self._start("frame", self._frame_command(frame, out, not crop), done)

    def picture(self):
        """What the picture in the pane shows: in crop mode the source as it comes in, else the edited result
        (a video: its frame at the playhead)."""
        crop = self.params["tools"] == "crop"
        if self.kind == VIDEO:
            if self.playing:
                return ""  # the result plays there instead (edit_media_timeline.py)
            return self._frame_png if self._frame_key and self._frame_key[2] == crop else ""
        return self.source() if crop else self.params["result"]

    # ---- what goes downstream -----------------------------------------------------------------------------
    def value(self):
        if self.span_data() is not None and self._span_clip[1] != self._span_plan_key():
            self._update_span(wait=True)  # needed now: cut the clip on the spot
        if self.stale():
            self.render_now()
        return self.params["result"] or None

    def image_path(self):
        return self.params["result"] if self.kind == IMAGE else ""

    # ---- display ------------------------------------------------------------------------------------------
    def problem(self):
        return self.error

    def changes(self):
        p = self.params
        out = []
        if self.kind == VIDEO:
            first, last = self.trim_frames()
            s, film = self.span(), self.span_source_frames()
            if s is not None and film is not None:  # a shot / chunk: trimmed = moved off its own frames
                if film != (s["first"], s["last"]):
                    out.append(f"trim {film[0] - s['first']:+d}/{film[1] - s['last']:+d}")
            elif (first, last) != (1, self.frames() or last):
                out.append("trim")
        if self.kind == AUDIO:
            start, end = self.trim_seconds()
            if start > 0 or end < self.media_info().get("duration", end):
                out.append("trim")
        if self.kind in (IMAGE, VIDEO):
            if any(p[k] for k in ("crop_left", "crop_top", "crop_right", "crop_bottom")):
                out.append("crop")
            if p["rotate"] != "0":
                out.append("rotate")
            if p["flip"] != "none":
                out.append("flip")
            if p["width"] and p["height"] and (p["width"], p["height"]) != self.natural():
                out.append("resize")
            defaults = {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0, "sharpen": 0.0}
            out += [k for k, d in defaults.items() if abs(p[k] - d) > 1e-6]
            if p["strokes"]:
                out.append("drawing")
        if self.kind in (VIDEO, AUDIO):
            if abs(self.speed() - 1.0) > 1e-4:
                out.append(f"speed ×{self.speed():.3g}" + (f" ({p['fps']:g} fps)" if self.kind == VIDEO else ""))
            if self.kind == VIDEO and p["sound"] == "mute":
                out.append("muted")
            elif self.kind == AUDIO or p["sound"] == "keep":
                if p["denoise"]:
                    out.append("denoise")
                if abs(p["volume"]) > 1e-6:
                    out.append(f"{p['volume']:+g} dB")
                if p["normalize"]:
                    out.append(f"{p['normalize']} LUFS")
            if self.kind == AUDIO:
                if p["fade_in"] or p["fade_out"]:
                    out.append("fades")
                if p["channels"]:
                    out.append(dict(CHANNELS)[p["channels"]])
                if p["rate"]:
                    out.append(dict(RATES)[p["rate"]])
        return out

    def subtitle(self):
        if not self.input_sockets[0].edges:
            return "connect an image, video or sound"
        unit = self.span_unit()
        if unit and not self.source():
            return f"{unit} {self.params[unit]} · cutting…" if "span" in self._jobs else f"{unit} {self.params[unit]}"
        if not unit and not self.source():  # e.g. a Modality that hasn't rendered yet
            return f"waiting for {self.input_sockets[0].edges[0].source.node.name}'s result"
        changes = self.changes()
        text = ", ".join(changes) if changes else "no changes yet"
        return f"{unit} {self.params[unit]} · {text}" if unit else text

    def info_rows(self):
        from .nodes import image_rows
        from .audiobar import clock
        rows = []
        s = self.span()
        if s is not None:
            from .shots_view import describe
            data = self.span_data()
            rows.append((spans.unit(data).title(), describe(dict(s, _fps=float(data["fps_value"])))))
            plan = self._span_plan()
            if plan is None:
                rows.append(("Problem", "the video the shots were found in isn't there"))
            else:
                video, a, b, _fps, _s = plan
                rows.append(("Clip", f"frames {a}–{b} of {os.path.basename(video)} "
                                     f"({s['first'] - a} + {b - s['last']} frames of handles)"))
                if "span" in self._jobs:
                    rows.append(("Status", "cutting the clip…"))
            src = self.span_source_frames()
            if src:
                moved = (src[0] - s["first"], src[1] - s["last"])
                note = "" if moved == (0, 0) else f" (start {moved[0]:+d}, end {moved[1]:+d} vs the {spans.unit(data)})"
                rows.append(("Film frames", f"{src[0]}–{src[1]}{note}"))
        if self.kind == IMAGE and self.params["result"]:
            size = next((v for k, v in image_rows(self.params["result"]) if k == "Image"), "")
            src = next((v for k, v in image_rows(self.source()) if k == "Image"), "")
            rows += [("Result", size), ("Source", src), ("Kept in", "the app's instance cache (freed with the node)")]
        if self.kind == VIDEO and self.source():
            info = self.media_info()
            first, last = self.trim_frames()
            size = self.source_size()
            fps = f"{info['fps']:g} fps" if info["fps_known"] else "fps not recorded (24 assumed)"
            rows.append(("Source", (f"{size[0]}×{size[1]} · " if size else "") + f"{self.frames()} frames · {fps}"
                         + ("" if info["has_audio"] else " · no sound")))
            n = last - first + 1
            rows.append(("Going out", f"frames {first}–{last} ({n}) · {self._out_seconds():.2f} s"))
            if self.rendering():
                status = "rendering…"
            elif not self.params["result"]:
                status = "not rendered yet - Render (or it renders when something downstream needs it)"
            elif self.stale():
                status = "settings changed - Render again (or it renders when something downstream needs it)"
            else:
                status = "up to date · H.264 MP4" + (" + AAC" if info["has_audio"] and self.params["sound"] == "keep"
                                                    else "")
            rows.append(("Result", status))
            w, h = (self.params["width"], self.params["height"])
            if w % 2 or h % 2:
                rows.append(("Note", f"H.264 needs even sizes: {w}×{h} goes out as {w // 2 * 2}×{h // 2 * 2}"))
        if self.kind == AUDIO and self.source():
            info = self.media_info()
            start, end = self.trim_seconds()
            rows.append(("Source", f"{clock(info['duration'])} · {info['sample_rate'] / 1000:g} kHz"))
            fades = [f"{label} {self.params[k]:g} s" for k, label in (("fade_in", "fade in"), ("fade_out", "fade out"))
                     if self.params[k] > 0]
            rows.append(("Going out", f"{clock(start)} – {clock(end)} ({(end - start) / self.speed():.2f} s) · 16-bit WAV"
                         + "".join(" · " + f for f in fades)))
        if self.error:
            rows.append(("Problem", self.error))
        return rows

    def links_changed(self):
        """Width and Height both linked: the links decide the size, so the proportions lock goes off."""
        if "width" in self.links and "height" in self.links and self.params["keep_aspect"]:
            self.set_param("keep_aspect", False)

    def action(self, name):
        p = self.params
        if name == "export":
            self.export()
        elif name == "render":
            if self.kind == VIDEO and self.source():
                self._render_async(progress=True)
        elif name in ("rotate_left", "rotate_right"):
            turn = 90 if name == "rotate_right" else 270
            self.set_param("rotate", str((int(p["rotate"]) + turn) % 360))
        elif name in ("flip_h", "flip_v"):
            parts = set(p["flip"].replace("none", ""))
            parts ^= {name[-1]}
            self.set_param("flip", "".join(sorted(parts)) or "none")
        elif name == "undo_stroke" and p["strokes"]:
            self.set_param("strokes", list(p["strokes"][:-1]) or None)
        elif name == "clear_strokes" and p["strokes"]:
            self.set_param("strokes", None)
        elif name == "reset":  # everything back as it was - links included (they're broken)
            from . import links
            for linked in list(self.links):
                links.break_link(self, linked)
            self._quiet = True
            for q in self.parameters:
                if q.name in ALL_KEYS or q.name in ("keep_aspect", "tools"):
                    p[q.name] = q.default
            p["width"] = p["height"] = 0  # back to following the size coming in
            self._natural = None
            self._followed = {}  # end / frame rate: back to the source's
            self._follow_media()
            self._quiet = False
            if self.span_data() is not None and self._span_clip[1] is not None:  # the trim: back on the span
                p["span_preset"] = None
                self._span_ready(self._span_clip[1], self._span_clip[0], True)
            self._follow_size()
            self._schedule()
            self.notify()

    def export(self):
        from PySide6.QtWidgets import QFileDialog
        result = self.value()
        if not result:
            return
        ext = EXT.get(self.kind, ".png")
        base = os.path.splitext(os.path.basename(self.source()))[0] + "_edit" + ext
        start = os.path.join(os.path.dirname(self.source()), base)
        filters = {IMAGE: "PNG (*.png);;JPEG (*.jpg);;WebP (*.webp);;All files (*)",
                   VIDEO: "MP4 (*.mp4);;All files (*)",
                   AUDIO: "WAV (*.wav);;FLAC (*.flac);;MP3 (*.mp3);;All files (*)"}[self.kind]
        path, _ = QFileDialog.getSaveFileName(None, "Export the result", start, filters)
        if not path:
            return
        if os.path.splitext(path)[1].lower() == ext:
            shutil.copyfile(result, path)
        else:  # another format: let ffmpeg convert it
            subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-i", result, "-q:v", "2", path],
                           creationflags=NO_WINDOW)
        self.error = "" if os.path.isfile(path) else f"Couldn't export to {path}"
        self.notify()

    def remove(self):
        self._timer.stop()
        self._frame_timer.stop()
        self._stop_render()
        media_cache.release(self.params["result"])
        media_cache.release(self._frame_png)
        media_cache.release(self._span_clip[0])
        self._span_clip = ("", None)
        self.params["result"] = ""
        self._key, self._frame_key, self._frame_png = None, None, ""  # brought back by undo: renders anew
        super().remove()

    def params_restored(self, names):
        """Undo / redo: the restored values are the whole state (sizes included) - don't let them follow again."""
        self._natural = self.natural()
        self._schedule()


set_hints(EditMediaNode, {  # hover hints: what changing each parameter does
    "shot": "Which shot to edit", "chunk": "Which chunk to edit", "handles": "Extra film frames each side",
    "tools": "Crop, rotate, flip, draw, reset", "audio_tools": "Reset all edits", "pen_color": "Drawing colour",
    "pen_width": "Line thickness in pixels", "draw_tools": "Undo or clear the drawing",
    "render": "Make the edited file now", "export": "Save a copy elsewhere", "rotate": "Turn the picture",
    "flip": "Mirror the picture", "keep_aspect": "Keep width and height in proportion",
    "width": "Output width in pixels", "height": "Output height in pixels",
    "speed": "Playback speed (frame rate follows)", "fps": "Frames per second (speed follows)",
    "exposure": "Brighter or darker", "contrast": "More or less contrast", "saturation": "More or less colour",
    "gamma": "Lighten or darken mid-tones", "sharpen": "Sharper; negative blurs", "sound": "Keep or mute the sound",
    "denoise": "Remove background noise", "volume": "Louder or quieter, in dB", "normalize": "Even out the loudness",
    "channels": "Mono or stereo", "rate": "Samples per second"})
