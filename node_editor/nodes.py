"""Concrete node types. Add a new class + @register_node to put it in the Tab menu."""

import json
import math
import os

from PySide6.QtGui import QColor

from . import model_info as mi
from . import spellcheck
from .node import ANY, AUDIO, DATA, FILE, GEOMETRY, IMAGE, MODEL, TEXT, TYPE_LABELS, VIDEO, Node, Param, set_hints
from .registry import register_node
from .settings import default_python_env
from .modality import ModalityNode  # noqa: F401,E402  (registers itself)
from .edit_media import EditMediaNode  # noqa: F401,E402  (registers itself)
from .isolate import IsolateNode  # noqa: F401,E402  (registers itself)
from .chunks import ChunkNode  # noqa: F401,E402  (registers itself)
from .edit_geometry import EditGeometryNode  # noqa: F401,E402  (registers itself)


# Windows' spell checker, in any spell-checking language installed in Windows (for text fields)
SPELL_OPTIONS = spellcheck.OPTIONS


@register_node
class GenericNode(Node):
    type_name = "generic"
    label = "Generic"
    color = QColor(75, 90, 110)
    # Example parameters - replace with real ones as node types take shape.
    parameters = [
        Param("value", "Value", "float", 0.0),
        Param("count", "Count", "int", 1),
        Param("enabled", "Enabled", "bool", True),
        Param("note", "Note", "string", ""),
    ]


@register_node
class ImageNode(Node):
    type_name = "image"
    label = "Image"
    shape_name = "mountains"
    color = QColor(110, 80, 120)
    inputs = []
    outputs = [("image", IMAGE)]
    parameters = [
        Param("file", "File", "image", ""),  # path + preview in the parameter pane
    ]

    def value(self):
        return self.params["file"] or None

    def image_path(self):
        return self.params["file"]

    def info_rows(self):
        return image_rows(self.params["file"])


@register_node
class AudioNode(Node):
    type_name = "audio"
    label = "Audio"
    shape_name = "speaker"
    OUTPUT_X = (0.3,)  # under the speaker cone
    color = QColor(50, 100, 80)
    inputs = []
    outputs = [("audio", AUDIO)]
    parameters = [
        Param("file", "File", "audio", "", filter="Audio (*.wav *.flac *.mp3 *.ogg *.m4a);;All files (*)"),
    ]

    def value(self):
        return self.params["file"] or None

    def info_rows(self):
        return audio_rows(self.params["file"])

    def subtitle(self):
        return os.path.basename(self.params["file"]) if self.params["file"] else "no file"


@register_node
class VideoNode(Node):
    type_name = "video"
    label = "Video"
    shape_name = "camera"
    inputs = []
    outputs = [("video", VIDEO)]
    parameters = [
        Param("file", "File", "video", "", filter="Video (*.mp4 *.mov *.mkv *.webm *.avi)"),  # path + player
    ]

    def value(self):
        return self.params["file"] or None

    def subtitle(self):
        return os.path.basename(self.params["file"]) if self.params["file"] else "no file"

    def info_rows(self):
        return video_rows(self.params["file"])


@register_node
class FileNode(Node):
    """Any file a model may take - a PDF, a text, a document, an archive... What it is comes from the file itself
    (a picture through a File node is still a picture). Its own viewer per kind comes later; for now its facts."""

    type_name = "file"
    label = "File"
    shape_name = "document"
    inputs = []
    outputs = [("file", FILE)]
    parameters = [
        Param("file", "File", "file", "", filter="All files (*)"),
    ]

    def value(self):
        return self.params["file"] or None

    def subtitle(self):
        return os.path.basename(self.params["file"]) if self.params["file"] else "no file"

    def info_rows(self):
        return file_rows(self.params["file"])


def file_rows(path):
    """What a file is, as facts: its kind, type, size, date - and a PDF's page count (Qt's own PDF reader)."""
    import mimetypes
    import time
    from .node import item_kind
    if not path:
        return []
    if not os.path.isfile(path):
        return [("File", "not found")]
    st = os.stat(path)
    mime = mimetypes.guess_type(path)[0] or "not recorded"
    rows = [("Kind", item_kind(path)), ("Type", f"{os.path.splitext(path)[1].lower() or 'no extension'} · {mime}"),
            ("Size", _size(st.st_size)), ("Modified", time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)))]
    if path.lower().endswith(".pdf"):
        try:
            from PySide6.QtPdf import QPdfDocument
            doc = QPdfDocument()
            doc.load(path)
            rows.append(("Pages", str(doc.pageCount())))
            title = doc.metaData(QPdfDocument.MetaDataField.Title)
            if title:
                rows.append(("Title", str(title)))
            doc.close()
        except Exception:  # noqa: BLE001 - facts are optional
            pass
    return rows


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024


GEO_FILTER = "3D files (*.obj *.fbx *.glb *.gltf *.abc *.bvh *.ply *.stl *.dae *.3ds);;All files (*)"


@register_node
class GeometryNode(Node):
    """A 3D scene (OBJ, FBX, glTF / GLB, Alembic, PLY, STL...) - shown in the pane with a simple light rig, its
    animation on a frame timeline like a video, looked at with the viewer's orbit camera or through one of the
    cameras it (or a separate camera file) brings. Goes on as geometry (e.g. into a Data node for a 3D model)."""

    type_name = "geometry"
    label = "Geometry"
    shape_name = "cube"
    inputs = []
    outputs = [("geometry", GEOMETRY)]
    parameters = [
        Param("file", "Scene", "file", "", filter=GEO_FILTER, hint="The 3D file: meshes, cameras, animation"),
        Param("camera_file", "Camera file", "file", "", filter=GEO_FILTER,
              hint="More cameras from another file (optional)"),
        # which camera the viewer looks through: chosen on the View header (after the toggles) - Default = orbit
        Param("camera", "Camera", "data", "Default"),
        Param("view", "View", "geometry_view", None, visible=lambda n: bool(n.params["file"])),
        # the viewer's own settings, set from its header (Light / Options menus beside "View"):
        Param("headlight", "Headlight", "data", False),  # off = key + fill + rim with shadow; on = one camera light
        Param("textures", "Textures", "data", True),     # the file's materials + textures; off = grey Blinn
        Param("wire", "Wireframe", "data", False),       # the edges drawn over the shaded model
        Param("msaa", "Multisample", "data", True),      # multisample antialiasing
        Param("grid", "Grid", "data", True),             # the grid on the ground plane
        Param("at_origin", "At origin", "data", False),  # off = where the file puts it; on = standing at the origin
        Param("ground", "Ground", "data", False),        # a ground under the model, 2x its larger footprint
    ]

    def __init__(self, name):
        super().__init__(name)
        self.geometry_info = {}  # what the viewer loaded: {"state", "size"} (geometry_view.py)

    def value(self):
        return self.params["file"] or None

    def subtitle(self):
        return os.path.basename(self.params["file"]) if self.params["file"] else "no file"

    def problem(self):
        state = self.geometry_info.get("state", "") or self.geometry_info.get("error", "")
        return state if state.startswith(("error", "the 3D viewer")) else ""

    def info_rows(self):
        info = self.geometry_info
        rows = file_rows(self.params["file"])
        size = info.get("size")
        if info.get("converting"):
            rows.append(("Status", "converting…"))
        if info.get("note"):
            rows.append(("Note", info["note"]))
        if info.get("cameras"):
            rows.append(("Cameras", ", ".join(info["cameras"])))
        if (info.get("camera_note") or "").startswith("error"):
            rows.append(("Camera file", info["camera_note"]))
        if info.get("duration_ms"):
            fps = info.get("file_fps") or 0
            frames = round(info["duration_ms"] / 1000 * (fps or 24)) + 1  # the last key = the last frame (ms are rounded)
            rate = f"{fps:g} fps (the file's)" if fps else "24 fps assumed (the file records none)"
            rows.append(("Animation", f"{frames} frames · {rate}"))
        if info.get("state") == "ok" and size:
            rows.append(("Bounds", " × ".join(f"{v:g}" for v in size) + " (file units)"))
            centre = info.get("centre") or (0, 0, 0)
            rows.append(("Centre", ", ".join(f"{v:g}" for v in centre)
                         + (" (standing at the origin)" if self.params["at_origin"] else " (where the file puts it)")))
        elif self.params["file"] and not info.get("state") and not info.get("converting"):
            rows.append(("Status", "loading…" if info else "select the node to load it"))
        return rows


def _ffmpeg_tool(name):
    import shutil
    return shutil.which(name)


def probe(path):
    """ffprobe's description of a media file ({'format': ..., 'streams': [...]}) or {}."""
    import json
    import subprocess
    tool = _ffmpeg_tool("ffprobe")
    if not tool or not path or not os.path.isfile(path):
        return {}
    p = subprocess.run([tool, "-v", "error", "-show_format", "-show_streams", "-of", "json", path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return json.loads(p.stdout) or {}
    except ValueError:
        return {}


def _rate(text):
    try:
        num, den = (float(x) for x in str(text).split("/"))
        return num / den
    except (ValueError, ZeroDivisionError):
        return 0.0


def _bits(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return ""
    return f"{v / 1e6:.2f} Mb/s" if v >= 1e6 else f"{v / 1e3:.0f} kb/s"


def _size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _audio_line(a):
    parts = [a.get("codec_name", "")]
    if a.get("profile"):
        parts[0] += f" ({a['profile']})"
    if a.get("sample_rate"):
        parts.append(f"{int(a['sample_rate']) / 1000:g} kHz")
    if a.get("channel_layout") or a.get("channels"):
        n = a.get("channels")
        parts.append(a.get("channel_layout") or {1: "mono", 2: "stereo"}.get(n, f"{n} channels"))
    if a.get("bits_per_raw_sample") or a.get("bits_per_sample"):
        parts.append(f"{a.get('bits_per_raw_sample') or a.get('bits_per_sample')}-bit")
    parts.append(_bits(a.get("bit_rate")))
    return " · ".join(x for x in parts if x and x != "0-bit")


def _file_rows(info):
    """File size / overall bitrate / container duration, then the file's own tags that say something."""
    f = info.get("format", {})
    rows = []
    line = " · ".join(x for x in (_size(f.get("size")), _bits(f.get("bit_rate")),
                                  f"{float(f['duration']):.2f} s" if f.get("duration") else "") if x)
    if line:
        rows.append(("File", line))
    tags = {k.lower(): v for k, v in (f.get("tags") or {}).items()}
    for key, label in (("title", "Title"), ("artist", "Artist"), ("creation_time", "Created"), ("encoder", "Encoder")):
        if tags.get(key):
            rows.append((label, tags[key].replace("T", " ").replace(".000000Z", " UTC") if key == "creation_time"
                         else tags[key]))
    return rows


def video_rows(path):
    """Info rows for a video file, 'Video' first (size · fps · length of the video stream)."""
    info = probe(path)
    streams = info.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    if not v:
        return []
    fps = _rate(v.get("avg_frame_rate")) or _rate(v.get("r_frame_rate"))
    length = v.get("duration") or info.get("format", {}).get("duration")
    rows = [("Video", " · ".join(x for x in (f"{v.get('width')}×{v.get('height')}", f"{fps:g} fps" if fps else "",
                                              f"{float(length):.2f} s" if length else "") if x))]
    if str(v.get("nb_frames", "")).isdigit():
        rows.append(("Frames", v["nb_frames"]))
    codec = v.get("codec_name", "") + (f" ({v['profile']})" if v.get("profile") else "")
    rows.append(("Codec", " · ".join(x for x in (codec, v.get("pix_fmt", ""), _bits(v.get("bit_rate"))) if x)))
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    rows += [("Audio", _audio_line(a)) for a in audio] or [("Audio", "none")]
    return rows + _file_rows(info)


# ffmpeg pixel-format names in plain words (a format translation - nothing about any model)
PIXEL_FORMATS = {"rgb24": "8-bit RGB", "rgba": "8-bit RGBA (with alpha)", "rgb48be": "16-bit RGB",
                 "rgba64be": "16-bit RGBA (with alpha)", "gray": "8-bit greyscale", "gray16be": "16-bit greyscale",
                 "ya8": "8-bit greyscale + alpha", "pal8": "palette (256 colours)", "monob": "1-bit black & white",
                 "yuvj420p": "8-bit YCbCr 4:2:0", "yuvj422p": "8-bit YCbCr 4:2:2", "yuvj444p": "8-bit YCbCr 4:4:4",
                 "bgra": "8-bit RGBA (with alpha)", "bgr24": "8-bit RGB"}


def image_rows(path):
    """Info rows for an image: 'Image' (size · format · colour) first, orientation, file size, then the text the
    file embeds (generators often store the prompt and settings there; long values are shortened)."""
    info = probe(path)
    v = next((st for st in info.get("streams", []) if st.get("codec_type") == "video"), None)
    if not v:
        return []
    from PySide6.QtGui import QImageIOHandler, QImageReader
    reader = QImageReader(path)
    fmt = reader.format().data().decode().upper() or v.get("codec_name", "").upper()
    colour = PIXEL_FORMATS.get(v.get("pix_fmt", ""), v.get("pix_fmt", ""))
    rows = [("Image", " · ".join(x for x in (f"{v.get('width')}×{v.get('height')}", fmt, colour) if x))]
    if reader.transformation() != QImageIOHandler.TransformationNone:
        rows.append(("Orientation", "the file says to rotate / flip it (shown turned)"))
    rows += [r for r in _file_rows(info) if r[0] != "File"]
    size = _size(info.get("format", {}).get("size"))
    if size:
        rows.append(("File", size))
    for key in reader.textKeys():
        value = " ".join(reader.text(key).split())
        if len(value) > 200:
            kind = "JSON, " if value[:1] in "{[" else ""
            value = f"{value[:120]}…  ({kind}{len(value):,} characters)"
        rows.append((key, value))
    return rows


def audio_rows(path):
    """Info rows for an audio file, 'Audio' first."""
    info = probe(path)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not a:
        return []
    return [("Audio", _audio_line(a))] + _file_rows(info)


@register_node
class PromptNode(Node):
    """Plain text. The same node works as a prompt, a negative prompt, text to translate or speak..."""

    type_name = "prompt"
    label = "Prompt"
    shape_name = "terminal"
    HEIGHT = 42  # same height as Video
    color = QColor(120, 100, 50)
    inputs = []
    outputs = [("text", TEXT)]
    parameters = [
        Param("text", "Prompt", "text", ""),
        # optional: pick a file to load a saved prompt; Save writes the text there (or asks where).
        # Without a file the text lives in the graph only.
        Param("file", "File", "file", "", filter="Text (*.txt);;All files (*)"),
        Param("save", "Save to file", "button", None),
        Param("spell", "Spell check", "choice", spellcheck.DEFAULT, options=SPELL_OPTIONS),
    ]

    def __init__(self, name):
        super().__init__(name)
        self.note = ""  # result of the last load / save

    def value(self):
        return self.params["text"]

    def set_param(self, name, value):
        """Picking (or typing) a file loads it. Only edits made here do - opening a graph sets its values
        directly, so a graph's saved text is never replaced by the file's."""
        changed = name == "file" and value != self.params.get("file")
        super().set_param(name, value)
        if changed and value and os.path.isfile(value):
            try:
                with open(value, encoding="utf-8") as f:
                    text = f.read()
            except (OSError, UnicodeDecodeError) as e:
                self.note = f"Couldn't load {os.path.basename(value)}: {getattr(e, 'strerror', None) or e}"
            else:
                super().set_param("text", text)
                self.note = f"Loaded {os.path.basename(value)}"
            self.notify()

    def action(self, name):
        from PySide6.QtWidgets import QFileDialog
        path = self.params["file"]
        try:
            if name == "save":
                if not path:
                    path, _ = QFileDialog.getSaveFileName(None, "Save prompt", f"{self.name}.txt",
                                                          "Text (*.txt);;All files (*)")
                    if not path:
                        return
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.params["text"])
                if path != self.params["file"]:
                    Node.set_param(self, "file", path)  # just remember it - loading it back isn't wanted here
                self.note = f"Saved to {os.path.basename(path)}"
        except OSError as e:
            self.note = f"Couldn't {name} {os.path.basename(path)}: {e.strerror or e}"
        self.notify()

    def info_rows(self):
        return [("File", self.note)] if self.note else []

    def subtitle(self):
        text = " ".join(self.params["text"].split())
        return text if len(text) <= 40 else text[:40] + "…"


@register_node
class MergeNode(Node):
    """Up to 5 inputs of ONE type, kept in index order (0..4).

    The first connection decides the type; the other inputs then only accept that type.
    Disconnect everything to make it accept any type again.
    """

    type_name = "merge"
    label = "Merge"
    color = QColor(70, 70, 70)
    shape_name = "bulge"
    HEIGHT = round(100 / 3.3 * 1.2)  # the bulge's own proportions, 20% taller (user's choice)
    SOCKET_RADIUS = 5
    show_socket_labels = True
    inputs = [(str(i), ANY, False) for i in range(5)]
    outputs = [("list", ANY)]
    output_is_list = True

    def __init__(self, name):
        super().__init__(name)
        self.locked = ANY

    def upstream_changed(self):
        connected = [(s, s.edges[0]) for s in self.input_sockets if s.edges]
        if not connected:
            new = ANY
        elif self.locked == ANY:
            new = next((e.source.type for _, e in connected if e.source.type != ANY), ANY)
        else:
            new = self.locked
        if new != self.locked:
            self.locked = new
            for sock in self.sockets:
                sock.set_type(new)
            self.notify()

    def value(self):
        """Values by index; gaps stay as None so index 1 is always 'the second input'."""
        vals = [None] * 5
        for i, sock in enumerate(self.input_sockets):
            if sock.edges:
                v = sock.edges[0].source.node.value()
                vals[i] = v[0] if isinstance(v, list) and len(v) == 1 else v
        while vals and vals[-1] is None:
            vals.pop()
        return vals

    def named_values(self):
        """Each connected input's (name, value), in index order - the names of the nodes feeding it."""
        return [pair for sock in self.input_sockets if sock.edges
                for pair in sock.edges[0].source.node.named_values()]

    def subtitle(self):
        n = sum(1 for s in self.input_sockets if s.edges)
        return f"{n} × {TYPE_LABELS[self.locked].lower()}" if n else "empty · accepts any type"

    info_title = "Inputs"

    def info_rows(self):
        """The nodes plugged in, by input index (0 first) - the order models receive them in."""
        return [(str(i), sock.edges[0].source.node.name) for i, sock in enumerate(self.input_sockets) if sock.edges]


# ---- lists of material: Data collects, Delete leaves items out ----------------------------------------------
def _item_text(kind, value):
    """'image · fox.png' - what an item is and which file (or the start of a text)."""
    if isinstance(value, str):
        shown = os.path.basename(value) if kind != TEXT else (value[:40] + "…" if len(value) > 40 else value)
    else:
        shown = getattr(value, "name", str(value))
    return f"{kind} · {shown}"


def _count_kinds(items):
    counts = {}
    for _n, _v, kind in items:
        counts[kind] = counts.get(kind, 0) + 1
    return " · ".join(f"{n} {k}" for k, n in counts.items())


@register_node
class DataNode(Node):
    """The material for a model, in order: pictures, videos, sounds, JSON, other files (not prompts - those go to
    a Modality's prompt input). Inputs 0-4 in order, each one item or a list (a Merge, another Data node: more
    than five). Its list goes to a Modality's data input; Isolate takes one item out, Delete leaves items out."""

    type_name = "data"
    label = "Data"
    color = QColor(70, 70, 70)
    shape_name = "hexagon"
    HEIGHT = round(100 / 3.3 * 1.2)  # like the Merge: five labelled inputs
    SOCKET_RADIUS = 5
    show_socket_labels = True
    inputs = [(str(i), DATA, False) for i in range(5)]
    outputs = [("data", DATA)]
    output_is_list = True
    info_title = "Items"

    def list_items(self):
        """[(name, value, kind)] in input order (a list on an input keeps its own order)."""
        return [item for sock in self.input_sockets for item in self.data_items(sock.name)]

    def value(self):
        return [v for _n, v, _k in self.list_items()]

    def named_values(self):
        return [(n, v) for n, v, _k in self.list_items()]

    def upstream_changed(self):
        sig = tuple((n, str(v), k) for n, v, k in self.list_items())
        if sig != getattr(self, "_sig", None):
            self._sig = sig
            self.notify()

    def subtitle(self):
        items = self.list_items()
        return _count_kinds(items) if items else "empty · pictures, videos, sounds, files"

    def info_rows(self):
        """Every item a model receives, numbered in the order it receives them."""
        return [(str(i), f"{name} · {_item_text(kind, v)}") for i, (name, v, kind) in enumerate(self.list_items(), 1)]


@register_node
class DeleteNode(Node):
    """Leaves items out of a list (a Data node's, a Merge's): the numbers as the list shows them (1 = first)."""

    type_name = "delete"
    label = "Delete"
    color = QColor(70, 70, 70)
    shape_name = "backspace"
    show_socket_labels = "hover"
    inputs = [("list", ANY, False)]
    outputs = [("list", ANY)]
    output_is_list = True
    info_title = "Items"
    parameters = [
        # the item to leave out, as the list numbers it (1 = the first); more: another Delete after this one
        Param("item", "Delete item", "int", 1, step=1, minimum=1, maximum=99, hint="Which item to leave out"),
    ]

    def accepts(self, socket, source):
        return source.node.output_is_list  # a list: a Data node, a Merge, another Delete

    def incoming(self):
        return self.data_items("list")

    def list_items(self):
        gone = int(self.params["item"] or 0)
        return [item for i, item in enumerate(self.incoming(), 1) if i != gone]

    def value(self):
        return [v for _n, v, _k in self.list_items()]

    def named_values(self):
        return [(n, v) for n, v, _k in self.list_items()]

    def upstream_changed(self):
        sock = self.input_sockets[0]
        t = sock.edges[0].source.type if sock.edges else ANY
        if self.output_sockets[0].type != t:
            self.output_sockets[0].set_type(t)  # a list of models stays a list of models
            self.notify()

    def param_changed(self, name):
        self.notify()

    def subtitle(self):
        n_in, n_out = len(self.incoming()), len(self.list_items())
        return f"{n_out} of {n_in}" + (f" · without {self.params['item']}" if n_out != n_in else "")

    def problem(self):
        n = len(self.incoming())
        return f"the list has {n} items - no {self.params['item']}" if (self.input_sockets[0].edges
                                                                           and int(self.params["item"] or 0) > n) else ""

    def info_rows(self):
        gone = int(self.params["item"] or 0)
        return [(str(i), ("✗ " if i == gone else "") + f"{name} · {_item_text(kind, v)}")
                for i, (name, v, kind) in enumerate(self.incoming(), 1)]


# ---- models ---------------------------------------------------------------
ROLE_CHOICES = [("auto", "Auto-detect")] + [
    (r, mi.ROLE_LABELS[r]) for r in (
        mi.DIFFUSION, mi.TEXT_ENCODER, mi.VAE, mi.LORA, mi.IMAGE_ENCODER, mi.CHECKPOINT,
        mi.SPEECH_RECOGNITION, mi.TRANSLATION, mi.LANGUAGE_MODEL, mi.TEXT_TO_SPEECH, mi.OTHER)
]


def _role_is(*roles):
    return lambda n: n.role in roles


from . import online as _online  # noqa: E402

ONLINE = "online"  # the role of a model that runs on a service (it has no files here to detect a role from)
RUNS_ON = [("local", "Local"), ("online", "Online")]
SERVICE_PREFIX = "in:"  # a service model's own input, as a Model node setting ("in:duration")


def _service_param(f):
    """A service model's input (from its schema, svc_*.fields) as a Model node setting."""
    name, label, d = SERVICE_PREFIX + f["name"], f["title"], f["default"]
    hint = (f["description"].split(". ")[0][:90]) if f["description"] else ""
    if f["options"]:
        opts = [(o, str(o)) for o in f["options"]]
        if d is None:
            opts = [("", "service default")] + opts
        return Param(name, label, "choice", "" if d is None else d, options=opts, hint=hint)
    if f["type"] == "boolean":
        return Param(name, label, "bool", bool(d), hint=hint)
    if f["type"] in ("integer", "number") and d is not None:
        lo, hi = f["minimum"], f["maximum"]
        integer = f["type"] == "integer"
        step = None
        if lo is not None and hi is not None:
            step = 1 if integer else (0.01 if hi - lo <= 1 else 0.05 if hi - lo <= 5 else 0.1)
        return Param(name, label, "int" if integer else "float", int(d) if integer else float(d),
                     step=step, minimum=lo, maximum=hi, hint=hint)
    text = "" if d is None else json.dumps(d) if isinstance(d, (list, dict)) else str(d)
    kind = "text" if "prompt" in f["name"] and f["type"] == "string" else "string"
    return Param(name, label, kind, text, placeholder="service default" if d is None else None, hint=hint,
                 lines=3 if kind == "text" else None)


def _from_field(f, v):
    """A setting's value as the service's input expects it (typed text → number / list...)."""
    if f["type"] == "integer" and isinstance(v, str):
        try:
            return int(float(v))
        except ValueError:
            return None
    if f["type"] == "number" and isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    if f["type"] in ("array", "object") and isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return [s.strip() for s in v.split(",") if s.strip()] if f["type"] == "array" else None
    return v


@register_node
class ModelNode(Node):
    """Any AI model (file or Hugging Face folder). It detects what it is and shows the settings that fit."""

    type_name = "model"
    label = "Model"
    shape_name = "robot"
    color = QColor(60, 70, 80)
    # no inputs: what a model is asked comes from the prompts wired into the Modality (or an Executor's Prompt box)
    inputs = []
    outputs = [("model", MODEL)]
    info_title = "Detected"
    parameters = [
        # where the model runs: here (a file / folder on disk) or an online service (online.py)
        Param("runs_on", "Runs on", "choice", "local", options=RUNS_ON),
        # online: the service, then one of its online models - each a settings file set up in the Library
        # (+ Model > Online...; online_models/<name>.json)
        Param("provider", "Provider", "choice", _online.CLAUDE_CODE, options=_online.provider_options,
              visible=lambda n: n.online),
        Param("online_model", "Model", "choice", "", options=_online.model_options, visible=lambda n: n.online),
        Param("file", "File", "file", "",  # a weight file; a folder of them can be pasted or dropped
              filter="Model weights (*.safetensors *.sft *.ckpt *.pt *.pth *.bin *.gguf)", accept_folders=True,
              visible=lambda n: not n.online),
        Param("python_env", "Python env", "folder", "",  # the venv this model runs in; empty = app default
              placeholder=lambda: "default: " + default_python_env(), visible=lambda n: not n.online),
        Param("role", "Role", "choice", "auto", options=ROLE_CHOICES, visible=lambda n: not n.online),
        # diffusion (image generation)
        Param("width", "Width", "int", 1024, visible=_role_is(mi.DIFFUSION, mi.CHECKPOINT),
              step=1, minimum=64, maximum=4096),
        Param("height", "Height", "int", 1024, visible=_role_is(mi.DIFFUSION, mi.CHECKPOINT),
              step=1, minimum=64, maximum=4096),
        Param("steps", "Steps", "int", 20, visible=_role_is(mi.DIFFUSION, mi.CHECKPOINT), step=1, minimum=1, maximum=200),
        Param("cfg", "Guidance", "float", 4.0, visible=_role_is(mi.DIFFUSION, mi.CHECKPOINT),
              step=0.1, minimum=0.0, maximum=20.0),
        Param("negative", "Negative prompt", "text", "",  # what to avoid; takes effect with Guidance > 1
              visible=_role_is(mi.DIFFUSION, mi.CHECKPOINT)),
        Param("frames", "Frames", "int", 49,  # video models: how many frames (e.g. MiniMax H3 at 24 fps)
              visible=lambda n: n.role in (mi.DIFFUSION, mi.CHECKPOINT) and "video" in (n.info.media or ""),
              step=1, minimum=1, maximum=500),
        Param("seed", "Seed", "int", 0, visible=_role_is(mi.DIFFUSION, mi.CHECKPOINT, mi.LANGUAGE_MODEL, mi.TEXT_TO_SPEECH),
              step=1, minimum=0, maximum=2**31 - 1),
        # LoRA / adapter
        Param("strength", "Strength", "float", 1.0, visible=_role_is(mi.LORA), step=0.05, minimum=0.0, maximum=2.0),
        # translation
        Param("source_lang", "From language", "string", "", visible=_role_is(mi.TRANSLATION)),
        Param("target_lang", "To language", "string", "", visible=_role_is(mi.TRANSLATION)),
        # speech recognition
        Param("language", "Language", "string", "", visible=_role_is(mi.SPEECH_RECOGNITION)),
        # language model (what it is asked comes from the Modality's prompts)
        Param("thinking", "Thinking", "bool", False,  # only when the model's chat template has the switch
              visible=lambda n: n.role == mi.LANGUAGE_MODEL and n.thinking_switch),
        Param("max_tokens", "Max new tokens", "int", 512, visible=_role_is(mi.LANGUAGE_MODEL, mi.TRANSLATION),
              step=64, minimum=16, maximum=8192),
        Param("temperature", "Temperature", "float", 0.7, visible=_role_is(mi.LANGUAGE_MODEL),
              step=0.05, minimum=0.0, maximum=2.0),
        # text to speech
        Param("voice", "Voice sample", "file", "", filter="Audio (*.wav *.flac *.mp3 *.ogg);;All files (*)",
              visible=_role_is(mi.TEXT_TO_SPEECH)),
    ]

    def __init__(self, name):
        super().__init__(name)
        self.info = mi.ModelInfo()
        self.origin = []    # evidence about where the model comes from (model_info.sources)
        self.contents = []  # what the files on disk contain (model_info.contents)
        self.thinking_switch = False  # its chat template declares enable_thinking
        # a service model's own settings ("in:<input>") come from its schema: this node's list = the type's + those
        self.parameters = list(type(self).parameters)

    @property
    def online(self):
        """Runs on an online service (Claude Code...), not from files here."""
        return self.params.get("runs_on", "local") != "local"

    # ---- a service model's inputs (Replicate...): its own schema becomes this node's settings -------------------
    def service_entry(self):
        """The online model file of a service model (not Claude Code), or None."""
        if not self.online or _online.protocol_of(self.params.get("provider")) in ("", _online.CLAUDE_CODE):
            return None
        return _online.entry(self.params.get("online_model") or "")

    def brain_kind(self):
        """Can this model be an Executor's brain, and how: "claude_code", "gemini" (a Gemini text model) - or ""."""
        if not self.online:
            return ""
        protocol = _online.protocol_of(self.params.get("provider"))
        if protocol == _online.CLAUDE_CODE:
            return "claude_code"
        if protocol == "google":
            d = _online.info(self.service_entry()) or {}
            return "gemini" if d.get("family") == "text" else ""
        return ""

    def service_fields(self):
        d = _online.info(self.service_entry())
        return (d or {}).get("fields") or []

    def accepts_param(self, key):
        return key in self.params or key.startswith(SERVICE_PREFIX)

    def online_info_changed(self):
        self._service_params()

    def _service_params(self):
        """The settings list: the type's own, then the service model's inputs that the Modality doesn't fill (its
        prompt and data go there). Values typed before are kept (also those of fields not known yet)."""
        fields = self.service_fields()
        fed = _online.fed_fields(fields)
        extra = [_service_param(f) for f in fields if f["name"] not in fed]
        names = [p.name for p in self.parameters]
        wanted = list(type(self).parameters) + extra
        if [p.name for p in wanted] == names:
            return
        shown = {p.name: p.default for p in self.parameters if p.name.startswith(SERVICE_PREFIX)}
        for p in extra:
            # a value left at the previous model's default wasn't chosen: it follows this model's default
            if p.name not in self.params or (p.name in shown and self.params[p.name] == shown[p.name]):
                self.params[p.name] = p.default
        self.parameters = wanted

    def service_values(self):
        """{input: value} as the next run would send it plus the schema's defaults - what a cost estimate reads."""
        out = {}
        for f in self.service_fields():
            v = self.params.get(SERVICE_PREFIX + f["name"])
            out[f["name"]] = _from_field(f, v) if v not in (None, "") else f["default"]
        return out

    def service_inputs(self):
        """{input: value} this node sets itself (left empty / at the schema's default = the service decides)."""
        out = {}
        for f in self.service_fields():
            v = self.params.get(SERVICE_PREFIX + f["name"])
            if v in (None, ""):
                continue
            v = _from_field(f, v)
            if v is not None and v != f["default"]:
                out[f["name"]] = v
        return out

    @property
    def role(self):
        if self.online:
            return ONLINE
        chosen = self.params["role"]
        return self.info.role if chosen == "auto" else chosen

    def model_path(self):
        """File, or folder for models made of several files."""
        return mi.model_root(self.params["file"])

    def value(self):
        return self  # a Modality node reads everything it needs from the model node itself

    def param_changed(self, name):
        if name == "runs_on" and self.params["runs_on"] not in ("local", "online"):
            # a graph from before providers: Runs on named the service itself (e.g. "claude_code")
            self.params["provider"], self.params["runs_on"] = self.params["runs_on"], "online"
        if name == "runs_on" and self.online and not self.params["online_model"]:
            choices = _online.model_options(self)  # just switched to Online: its provider's first online model
            if choices:
                self.params["online_model"] = choices[0][0]
        if name == "provider" and self.params["online_model"] not in [v for v, _ in _online.model_options(self)]:
            choices = _online.model_options(self)  # another provider: its first online model
            if choices and _online.entry(self.params["online_model"]) is not None:
                self.params["online_model"] = choices[0][0]
        if name in ("runs_on", "provider", "online_model"):
            self._service_params()
        if name == "file":
            path = self.params["file"]
            self.info = mi.inspect(path) if path else mi.ModelInfo()
            exists = bool(path) and os.path.exists(path)
            self.origin = mi.sources(path) if exists else []
            self.contents = mi.contents(path) if exists else []
            self.thinking_switch = exists and "enable_thinking" in mi.chat_template(path)

    def env_folder(self):
        """This model's Python environment: its own setting, or the app default when left empty."""
        return self.params["python_env"].strip() or default_python_env()

    def python(self):
        """The python executable of this model's environment, or None."""
        from .engine.client import python_in
        return python_in(self.env_folder())

    def problem(self):
        if self.online:
            from . import online
            return online.problem(self.params["provider"], self)
        if self.params["file"] and self.info.error and self.info.role == mi.UNKNOWN:
            return self.info.error
        if not self.python():
            return "No python.exe in the Python env folder"
        return ""

    def subtitle(self):
        if self.online:
            label = _online.providers().get(self.params["provider"], self.params["provider"])
            _provider, model, _settings, e = _online.resolve(self)
            return label + " · " + (e["label"] if e else model or "its default model")
        if not self.params["file"]:
            return "no file"
        declared = mi.declared_part(self.params["file"])
        if declared:  # what the pipeline's model_index.json calls it
            return f"{declared[0]} · {declared[1].split('.')[-1]}"
        parts = [mi.ROLE_LABELS[self.role], self.info.media or self.info.family_label]
        return " · ".join(p for p in parts if p)

    def info_rows(self):
        if self.online:
            from . import online
            return online.info_rows(self.params["provider"], self)
        if not self.params["file"]:
            return []
        rows = [("Path", self.model_path()),
                ("Runs with", (self.python() or f"no python.exe in {self.env_folder()}")
                 + ("" if self.params["python_env"].strip() else " (app default)"))]
        if self.info.error:
            rows.append(("Status", self.info.error))
        declared = mi.declared_part(self.params["file"])
        if declared:
            rows.append(("Declared", f"{declared[0]}: {declared[1]} (model_index.json)"))
        root = self.model_path()
        if os.path.isdir(root):  # the files it uses, relative to its folder
            files = [os.path.basename(f) for f in mi.model_files(root)]
            if files:
                rows.append(("Uses", "\n".join(files)))
        if self.params["role"] != "auto":
            rows.append(("Detected as", mi.ROLE_LABELS[self.info.role] + " (overridden)"))
        rows += list(self.info.details)
        rows += self.contents
        rows += self.origin
        return rows


# ---- hover hints (a few words on what changing each parameter does) --------------------------------------------
set_hints(GenericNode, {"value": "A number", "count": "A whole number", "enabled": "On or off", "note": "A short note"})
set_hints(ImageNode, {"file": "The picture file"})
set_hints(AudioNode, {"file": "The sound file"})
set_hints(VideoNode, {"file": "The video file"})
set_hints(PromptNode, {"text": "Text the model receives", "file": "Text file to load / save",
                       "save": "Write this text to File", "spell": "Spell-check language"})
set_hints(ModelNode, {
    "runs_on": "Run here, or on an online service", "provider": "The online service",
    "online_model": "Online model (Library › + Model › Online)",
    "file": "The model's weights (file or folder)", "python_env": "Python env this model runs in",
    "role": "This part's job in the pipeline", "width": "Output width in pixels", "height": "Output height in pixels",
    "steps": "More steps: finer but slower", "cfg": "How strictly the prompt is followed",
    "negative": "What the result should avoid", "frames": "Video length in frames", "seed": "Same seed, same result",
    "strength": "How strongly this LoRA applies", "detection": "When the specialist adapter switches on",
    "source_lang": "Language of the input text", "target_lang": "Language to translate into",
    "language": "Spoken language (empty: detect)", "instruction": "What the language model should do",
    "spell": "Spell-check language", "thinking": "Let the model think first",
    "max_tokens": "Longest answer, in tokens", "temperature": "Higher: more varied answers",
    "voice": "The voice to speak with"})
