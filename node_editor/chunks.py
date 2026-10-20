"""Chunk node: a .shots file in, a .chunks file out - every shot cut into pieces of at most N frames, as equal as
possible (369 frames at most 73 -> 62, 62, 62, 61, 61, 61; never a 4-frame leftover). No model: plain arithmetic, done
by the app so nobody (person or language model) has to do it by hand.

.chunks = JSON, one line per chunk, frame numbers as in the .shots (1-based, first and last included):
  {"about", "video", "fps", "fps_value", "frames", "size", "shots_file", "shot_count", "max_frames",
   "chunks": [{"chunk", "shot", "part", "of", "first", "last", "frames", "start", "end", "seconds"}],
   "transitions": [...as in the .shots]}
Transition frames belong to no shot, so to no chunk.
"""

import math
import os

from PySide6.QtGui import QColor

from .engine.shots import dumps
from .modality import DEFAULT_OUTPUT
from .node import ANY, JSON, Node, Param, set_hints
from .registry import register_node

IN_SHOTS = "shots"


def split(first, last, max_frames):
    """[(first, last)] pieces of the span first..last, each at most max_frames long, lengths differing by <= 1."""
    n = last - first + 1
    k = max(1, math.ceil(n / max(1, max_frames)))
    base, extra = divmod(n, k)
    out, start = [], first
    for i in range(k):
        size = base + (1 if i < extra else 0)
        out.append((start, start + size - 1))
        start += size
    return out


def make_chunks(shots_data, max_frames, shots_file=""):
    fps = float(shots_data["fps_value"])
    chunks = []
    for s in shots_data["shots"]:
        pieces = split(s["first"], s["last"], max_frames)
        for part, (a, b) in enumerate(pieces, 1):
            chunks.append({"chunk": len(chunks) + 1, "shot": s["shot"], "part": part, "of": len(pieces),
                           "first": a, "last": b, "frames": b - a + 1, "start": round((a - 1) / fps, 3),
                           "end": round(b / fps, 3), "seconds": round((b - a + 1) / fps, 3)})
    keep = ("video", "fps", "fps_value", "frames", "seconds", "size")
    return {"about": f"Every shot cut into chunks of at most {max_frames} frames, as equal as possible. Frame "
                     "numbers start at 1 and include first and last (as in the .shots and on the Edit Media "
                     "timeline). 'part' of 'of' = which piece of its shot.",
            **{k: shots_data[k] for k in keep if k in shots_data},
            "shots_file": shots_file, "shot_count": len(shots_data["shots"]), "max_frames": max_frames,
            "chunks": chunks, "transitions": shots_data.get("transitions", [])}


@register_node
class ChunkNode(Node):
    type_name = "chunk"
    label = "Chunk"
    shape_name = "tabbed_right"
    color = QColor(60, 80, 90)
    info_title = "Chunk"
    inputs = [(IN_SHOTS, JSON, False)]
    outputs = [("out", JSON)]
    parameters = [
        Param("output_dir", "Output folder", "folder", DEFAULT_OUTPUT),
        Param("max_frames", "Max frames", "int", 73, step=1, minimum=1, maximum=1000),
        Param("result", "Result", "data_view", ""),
    ]

    def __init__(self, name):
        super().__init__(name)
        self._key = None
        self._data = None

    def accepts(self, socket, source):
        return not source.node.output_is_list and source.type in (JSON, ANY)

    def shots_file(self):
        values = [v for v in self.input_values(IN_SHOTS) if isinstance(v, str) and v]
        return values[0] if values else ""

    def analysed_video(self):
        """The video the shots were found in, as it is fed now (see Isolate): what feeds the shots' maker."""
        for maker in self.sources(IN_SHOTS):
            if hasattr(maker, "analysed_video"):
                return maker.analysed_video()
            videos = [v for v in (maker.videos() if hasattr(maker, "videos") else []) if isinstance(v, str) and v]
            if videos and os.path.isfile(videos[0]):
                return videos[0]
        return ""

    def _settings(self):
        path = self.shots_file()
        if not path or not os.path.isfile(path):
            return None
        return (path, os.path.getmtime(path), int(self.params["max_frames"] or 1),
                self.params["output_dir"] or DEFAULT_OUTPUT, self.name)

    def _make(self):
        """Write the .chunks file for the current settings (instant: arithmetic only)."""
        from .shots_view import is_shots, load
        key = self._settings()
        if key is None or key == self._key:
            return
        path, _mtime, max_frames, folder, name = key
        data = load(path)
        if not is_shots(data):
            self._key, self._data = None, None
            self.params["result"] = ""
            return
        out_data = {"format": "chunks", **make_chunks(data, max_frames, path)}
        os.makedirs(folder, exist_ok=True)
        out = os.path.join(folder, f"{name}_max{max_frames}.json")
        with open(out, "w", encoding="utf-8") as f:
            f.write(dumps(out_data))
        self._key, self._data = key, out_data
        self.set_param("result", out)

    def upstream_changed(self):
        self._make()

    def param_changed(self, name):
        if name in ("max_frames", "output_dir"):
            self._make()

    def params_restored(self, names):
        self._make()
        self.notify()

    def value(self):
        self._make()
        return self.params["result"] if self._key is not None else None

    def problem(self):
        if not self.shots_file():
            return "connect shots"
        if self._settings() is not None and self._key is None:
            return "not a .shots file"
        return ""

    def subtitle(self):
        if self._data is None or self.problem():
            return ""
        return f"{len(self._data['chunks'])} chunks ≤ {self._data['max_frames']} frames"

    def info_rows(self):
        rows = []
        d = self._data
        if d:
            sizes = [c["frames"] for c in d["chunks"]]
            split_shots = sum(1 for c in d["chunks"] if c["part"] == 1 and c["of"] > 1)
            rows.append(("Shots", f"{d['shot_count']} in {os.path.basename(d['shots_file'])}"))
            rows.append(("Chunks", f"{len(sizes)} · {min(sizes)}–{max(sizes)} frames · {split_shots} shots split"))
            rows.append(("Saved", self.params["result"]))
        return rows


set_hints(ChunkNode, {"output_dir": "Where the .chunks file goes", "max_frames": "Longest chunk, in frames",
                      "result": "The chunks, on the video"})
