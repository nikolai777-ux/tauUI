"""Isolate node: one thing out of a list. A list of items (a Data node, a Merge, a Delete): the chosen item goes on
as it is - shown here, to be looked at or edited (e.g. into an Edit Media). Shots / chunks (a JSON from a shot
detector or a Chunk node): one shot out of a video - or out of a sound - cut as below.

One input. With shots / chunks it cuts the video the shots were found in (what feeds that Modality now - e.g. an
Edit Media result - else the file the JSON names). A video is cut to the shot's exact frames (first..last, 1-based,
as on the Edit Media timeline);
a sound (e.g. the film's soundtrack as a WAV) to the shot's seconds (start..end). The cut is saved in the Output folder
(the project's output/ by default) as <node>_shot<NNN>.mp4 / .wav and goes downstream from there.
"""

import os
import shutil
import subprocess

from PySide6.QtCore import QProcess, QTimer
from PySide6.QtGui import QColor

from . import spans
from .node import ANY, AUDIO, DATA, GEOMETRY, IMAGE, JSON, TEXT, VIDEO, Node, Param, set_hints
from .modality import DEFAULT_OUTPUT
from .registry import register_node

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".ogg", ".m4a", ".aac"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
IN_SHOTS = "list"  # shots / chunks JSON, or a list of items


def _is(kind):
    return lambda n: n.kind == kind


@register_node
class IsolateNode(Node):
    type_name = "isolate"
    label = "Isolate"
    shape_name = "target"
    color = QColor(60, 80, 90)
    info_title = "Isolate"
    inputs = [(IN_SHOTS, ANY, False)]
    outputs = [("out", ANY)]
    show_socket_labels = "hover"
    parameters = [
        Param("output_dir", "Output folder", "folder", DEFAULT_OUTPUT),
        # a list of items: which one (1 = the first, as the list shows them)
        Param("item", "Item", "int", 1, step=1, minimum=1, maximum=99, visible=lambda n: n.picking()),
        # which span: a shot of a shots JSON, or a chunk of a chunks JSON (Chunk node)
        Param("shot", "Shot", "int", 1, step=1, minimum=1, maximum=999,
              visible=lambda n: not n.picking() and n.unit() == "shot"),
        Param("chunk", "Chunk", "int", 1, step=1, minimum=1, maximum=9999,
              visible=lambda n: not n.picking() and n.unit() == "chunk"),
        Param("result_image", "Item", "image_view", "", visible=_is(IMAGE)),
        Param("result_video", "Result", "video_view", "", visible=_is(VIDEO)),
        Param("result_audio", "Result", "audio_view", "", visible=_is(AUDIO)),
        Param("result_data", "Item", "data_view", "", visible=_is(JSON)),
        Param("result_text", "Item", "text_view", "", visible=_is(TEXT)),
        Param("result_geometry", "Item", "geometry_view", "", visible=_is(GEOMETRY)),
    ]

    def __init__(self, name):
        super().__init__(name)
        self.kind = ""
        self.error = ""
        self._key, self._wanted, self._proc = None, None, None
        self._timer = QTimer(singleShot=True, interval=150)
        self._timer.timeout.connect(self._render_async)

    # ---- what is plugged in -----------------------------------------------------------------------------------
    def accepts(self, socket, source):
        return source.node.output_is_list or source.type in (JSON, DATA, ANY)  # shots / chunks, or a list

    # ---- one item of a list ---------------------------------------------------------------------------------------
    def picking(self):
        """A list of items is plugged in (not shots / chunks): the chosen item goes on."""
        sock = self.input_sockets[0]
        return bool(sock.edges) and (sock.edges[0].source.node.output_is_list or sock.edges[0].source.type == DATA)

    def list_in(self):
        return self.data_items(IN_SHOTS)

    def item(self):
        """The chosen (name, value, kind), or None."""
        items = self.list_in()
        return items[max(1, min(len(items), int(self.params["item"] or 1))) - 1] if items else None

    def _show_item(self):
        """The chosen item, shown in the pane by what it is; the output takes its type."""
        it = self.item()
        kind = it[2] if it else ""
        value = it[1] if it and isinstance(it[1], str) else ""
        for name, k in (("result_image", IMAGE), ("result_video", VIDEO), ("result_audio", AUDIO),
                        ("result_data", JSON), ("result_text", TEXT), ("result_geometry", GEOMETRY)):
            self.params[name] = value if kind == k else ""
        self.kind = kind
        self.output_sockets[0].set_type(kind if kind in (IMAGE, VIDEO, AUDIO, JSON, TEXT, GEOMETRY) else ANY)
        self.notify()

    def shots_file(self):
        values = [v for v in self.input_values(IN_SHOTS) if isinstance(v, str) and v]
        return values[0] if values else ""

    def shots_data(self):
        return spans.load(self.shots_file())

    def media(self):
        """The file to cut: the video the shots were found in (as it is fed now)."""
        return spans.analysed_video(self, IN_SHOTS)

    def _detect_kind(self):
        ext = os.path.splitext(self.media())[1].lower()
        return VIDEO if ext in VIDEO_EXTS else AUDIO if ext in AUDIO_EXTS else ""

    def unit(self):
        """"chunk" when a .chunks file is connected, else "shot"."""
        return spans.unit(self.shots_data())

    def spans(self):
        return spans.spans(self.shots_data())

    def span(self):
        """The chosen shot / chunk (a dict from the file), or None."""
        return spans.pick(self.shots_data(), self.params[self.unit()])

    def upstream_changed(self):
        if self.picking():
            self._show_item()
            return
        kind = self._detect_kind()
        if kind != self.kind:
            self.kind = kind
            self.output_sockets[0].set_type({VIDEO: VIDEO, AUDIO: AUDIO}.get(kind, ANY))
            self.notify()
        self._schedule()

    def param_changed(self, name):
        if name == "item" and self.picking():
            self._show_item()
        if name in ("shot", "chunk", "output_dir"):
            self._schedule()

    def params_restored(self, names):
        self._schedule()
        self.notify()

    # ---- cutting ------------------------------------------------------------------------------------------------
    def _settings(self):
        src, s = self.media(), self.span()
        if not src or not s or not self.kind or not os.path.isfile(src):
            return None
        fps = float(self.shots_data()["fps_value"])
        return (src, os.path.getmtime(src), self.kind, s["first"], s["last"], fps, (self.unit(), s[self.unit()]),
                self.params["output_dir"] or DEFAULT_OUTPUT, self.name)

    def stale(self):
        return self._settings() is not None and self._settings() != self._key

    def _command(self, key, out):
        src, _mtime, kind, first, last, fps = key[:6]
        return spans.cut_command(src, kind, first, last, fps, out)

    def _out_path(self, key):
        folder, name = key[7], key[8]
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{name}_{key[6][0]}{key[6][1]:03d}" + (".mp4" if key[2] == VIDEO else ".wav"))

    def _schedule(self):
        if self.stale():
            self._timer.start()

    def _render_async(self):
        key = self._settings()
        if key is None or key == self._key or (self._proc is not None and key == self._wanted):
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.error = "ffmpeg isn't on PATH"
            self.notify()
            return
        self._stop()
        self._wanted = key
        out = self._out_path(key)
        proc = QProcess()
        proc.finished.connect(lambda code, _st, p=proc, k=key, o=out: self._done(p, code, k, o))
        self._proc = proc
        self.progress = 0.0
        proc.start(ffmpeg, self._command(key, out))
        self.notify()

    def _done(self, proc, code, key, out):
        if proc is not self._proc:
            return
        err = bytes(proc.readAllStandardError()).decode("utf-8", "replace").strip()
        self._proc = None
        self._finish(key, out, code == 0 and os.path.isfile(out), err)

    def _stop(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.kill()
            proc.waitForFinished(1000)

    def _finish(self, key, out, ok, err=""):
        self.progress = None
        if ok:
            name = "result_video" if key[2] == VIDEO else "result_audio"
            self._key, self.error = key, ""
            self.params["result_video" if name == "result_audio" else "result_audio"] = ""
            self.set_param(name, out)
        else:
            self.error = "ffmpeg: " + (err.splitlines()[-1] if err else "failed")
            self.notify()

    def render_now(self):
        key = self._settings()
        ffmpeg = shutil.which("ffmpeg")
        if key is None or not ffmpeg:
            return
        self._timer.stop()
        self._stop()
        out = self._out_path(key)
        p = subprocess.run([ffmpeg] + self._command(key, out), capture_output=True, text=True, creationflags=NO_WINDOW)
        self._finish(key, out, p.returncode == 0 and os.path.isfile(out), p.stderr.strip())

    def named_values(self):
        if self.picking():
            it = self.item()
            return [(it[0], it[1])] if it else []
        return super().named_values()

    def value(self):
        if self.picking():
            it = self.item()
            return it[1] if it else None
        if self.stale():
            self.render_now()
        if self._key is None:
            return None
        return self.params["result_video" if self.kind == VIDEO else "result_audio"] or None

    # ---- display ------------------------------------------------------------------------------------------------
    def problem(self):
        if self.picking():
            return "" if self.list_in() else "the list is empty"
        if not self.shots_file():
            return "connect a list, or shots / chunks"
        if self.shots_data() is None:
            return "not a shots / chunks JSON"
        if not self.media() or not os.path.isfile(self.media()):
            return "no video or sound to cut"
        if not self.kind:
            return "the shots were found in something that is not a video or a sound"
        return self.error

    def subtitle(self):
        if self.picking():
            it, n = self.item(), len(self.list_in())
            return f"{min(int(self.params['item'] or 1), n)} of {n} · {it[0]} · {it[2]}" if it else ""
        s = self.span()
        if not s or self.problem():
            return ""
        from .shots_view import describe
        return describe(dict(s, _fps=float(self.shots_data()["fps_value"])))

    def info_rows(self):
        if self.picking():
            items = self.list_in()
            chosen = max(1, min(len(items), int(self.params["item"] or 1)))
            return [(str(i), ("▸ " if i == chosen else "") + f"{name} · {kind}"
                     + (f" · {os.path.basename(v)}" if isinstance(v, str) and kind != TEXT else ""))
                    for i, (name, v, kind) in enumerate(items, 1)]
        rows = []
        data, s = self.shots_data(), self.span()
        if data:
            n, unit = len(self.spans()), self.unit()
            rows.append((unit.title() + "s", f"{n} in {os.path.basename(self.shots_file())}"))
            if int(self.params[unit] or 1) > n:
                rows.append(("Note", f"there are {n} {unit}s: {unit} {n} is used"))
        if s:
            fps = float(data["fps_value"])
            rows.append(("Frames", f"{s['first']}–{s['last']} ({s['frames']})"))
            rows.append(("Time", f"{s['start']:.3f}–{s['end']:.3f} s ({s['frames'] / fps:.2f} s)"))
        if self.media():
            rows.append(("Source", self.media()))
            if self.kind == VIDEO and data:
                from .playbar import stream_frames
                frames, fps = stream_frames(self.media())
                if frames and (frames != data["frames"] or abs(fps - float(data["fps_value"])) > 0.01):
                    rows.append(("Note", f"this video has {frames} frames at {fps:.3f} fps; the shots were found in "
                                         f"one with {data['frames']} at {float(data['fps_value']):.3f} - frame numbers "
                                         f"may not match"))
        if self._proc is not None:
            rows.append(("Result", "cutting…"))
        elif self._key is not None:
            rows.append(("Saved", self.params["result_video" if self.kind == VIDEO else "result_audio"]))
        elif self.error:
            rows.append(("Result", self.error))
        return rows

    def remove(self):
        self._timer.stop()
        self._stop()
        self._key = None  # saved cuts stay in the output folder
        super().remove()

    def readded(self):
        self._key = None
        self._schedule()


set_hints(IsolateNode, {"output_dir": "Where the cuts are saved", "item": "Which item of the list",
                        "shot": "Which shot to cut out",
                        "chunk": "Which chunk to cut out"})
