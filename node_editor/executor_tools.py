"""What an Executor's brain can do in the app: the tools it calls (through mcp_server.py -> executor.py's bridge).

Coarse and general - the same tools for any task (image, video, sound, text; later 3D): read the task, look at
pictures, list the models in the library and the node types, build a graph (create / set / connect / delete),
test, finish. Talking with the user is not a tool: the brain's replies are the conversation. What a node type or a model can do is read from the app and the models'
own files at the time of the call - nothing here knows about particular models.

Boundaries: the user's own nodes are read-only - the brain may wire FROM them, but it only changes, wires INTO or
deletes nodes it created (in this build, or earlier builds of the same conversation). Taken names are refused,
never silently renamed. Rendering: render_node (the brain's tests); render_built renders everything it built
(when the user asks - the pane has no Render in Executor mode); free_memory unloads what those renders loaded.
"""

import base64
import json
import os
import shutil
import subprocess
import tempfile
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from PySide6.QtGui import QImage, QImageReader

from . import undo
from .node import Node
from .registry import NODE_TYPES

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
AUDIO_EXTS = {".wav", ".flac", ".mp3", ".ogg", ".m4a"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
UI_ONLY = ("image_view", "geometry_view", "curve_editor", "text_view", "log_view", "audio_view", "video_view", "data_view", "media_picture", "media_timeline",
           "toolbar", "button", "icon", "chat_view")


class ToolError(Exception):
    """A refusal or a mistake the brain should read and correct."""


class Later:
    """A tool answer that comes later (a render takes minutes): the bridge answers when check() returns something.
    check() -> None while waiting, else the answer; `timeout` seconds at most."""

    def __init__(self, check, timeout):
        self.check, self.timeout = check, timeout


TOOLS = []  # [{"name", "description", "inputSchema", "fn"}]


def tool(description, **properties):
    """Register a tool; properties: name -> (json type, description[, required=True])."""
    def wrap(fn):
        props, required = {}, []
        for pname, spec in properties.items():
            ptype, pdesc, *rest = spec
            props[pname] = {"type": ptype, "description": pdesc} if isinstance(ptype, str) else {**ptype,
                                                                                                "description": pdesc}
            if not rest or rest[0]:
                required.append(pname)
        TOOLS.append({"name": fn.__name__, "description": description,
                      "inputSchema": {"type": "object", "properties": props, "required": required}, "fn": fn})
        return fn
    return wrap


def side_by_side(fn):
    """(Above @tool.) Calls of this tool may run together. Claude Code sends MCP calls one after another unless the
    tool is marked read-only - so tools that only read, or only wait for something no other call needs (a render:
    several renders asked for at once run together), are marked so."""
    next(t for t in TOOLS if t["fn"] is fn)["annotations"] = {"readOnlyHint": True}
    return fn


def listing():
    return [{k: t[k] for k in ("name", "description", "inputSchema", "annotations") if k in t} for t in TOOLS]


def call(ctx, name, args):
    """Run a tool: MCP content blocks + whether it failed. When the tool changed which nodes are the build's own,
    that is recorded at once (the graph's "built" + state.json) - a turn can last hours, and a crash before its end
    left every node it made looking like the user's (read-only) after the restart."""
    mine = _owned(ctx)
    try:
        return _call(ctx, name, args)
    finally:
        if _owned(ctx) != mine and hasattr(ctx, "_save_state"):
            ctx._save_state()


def _owned(ctx):
    return [(id(n), n.name) for n in getattr(ctx, "created", [])]


def _call(ctx, name, args):
    t = next((t for t in TOOLS if t["name"] == name), None)
    if t is None:
        return [{"type": "text", "text": f"no tool named {name}"}], True
    try:
        out = t["fn"](ctx, **args)
        if isinstance(out, Later):
            return out, False
    except ToolError as e:
        return [{"type": "text", "text": str(e)}], True
    except TypeError as e:  # wrong / missing arguments
        return [{"type": "text", "text": f"{name}: {e}"}], True
    except Exception as e:  # noqa: BLE001 - never leave a call unanswered: the brain would wait for ever
        import traceback
        traceback.print_exc()
        return [{"type": "text", "text": f"{name} failed in the app: {type(e).__name__}: {e}"}], True
    if isinstance(out, list) and out and all(isinstance(b, dict) and b.get("type") in ("text", "image") for b in out):
        return out, False  # content blocks (look_at: a picture + a caption)
    text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1)
    return [{"type": "text", "text": text}], False


# ---- helpers ------------------------------------------------------------------------------------------------------
def _docs(ctx):
    return list(ctx.window.tabs.docs)


def _find(ctx, name, scene=None):
    """A node by name: in the build's tab first, then the executor's, then any tab."""
    scenes = [s for s in (scene, ctx.target.scene, ctx.node.scene()) if s is not None]
    scenes += [d.scene for d in _docs(ctx) if d.scene not in scenes]
    for s in scenes:
        n = s.node_named(name)
        if n is not None:
            return n
    raise ToolError(f"no node named {name!r}")


def _own(ctx, node):
    if node not in ctx.created:
        raise ToolError(f"{node.name} is the user's node - it is only read (wire FROM it). Only nodes created in "
                        f"this build can be changed, wired into or deleted.")


def _media_facts(path):
    """A short line about a file: size of a picture, length of a video / sound."""
    if not path or not os.path.exists(path):
        return "missing"
    if os.path.isdir(path):
        return "folder"
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXTS:
        size = QImageReader(path).size()
        return f"image {size.width()}×{size.height()}" if size.isValid() else "image"
    if ext in VIDEO_EXTS | AUDIO_EXTS:
        from .nodes import probe
        info = probe(path)
        dur = float(info.get("format", {}).get("duration") or 0)
        v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
        if v:
            return f"video {v.get('width')}×{v.get('height')} · {dur:.2f} s"
        return f"sound {dur:.2f} s"
    return f"file {os.path.getsize(path)} bytes"


def _param(node, name):
    p = next((p for p in node.parameters if p.name == name), None)
    if p is None:
        raise ToolError(f"{node.name} ({node.type_name}) has no parameter {name!r}; it has: "
                        + ", ".join(q.name for q in node.parameters if q.kind not in UI_ONLY))
    return p


def _coerce(node, p, value):
    k = p.kind
    try:
        if k == "int":
            return int(round(float(value)))
        if k == "float":
            return float(value)
        if k == "bool":
            return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
        if k == "choice":
            values = [str(v) for v, _l in p.choices(node)]
            if str(value) not in values:
                raise ToolError(f"{p.name}: {value!r} isn't one of {values}")
            return str(value)
        if k in UI_ONLY:
            raise ToolError(f"{p.name} is shown by the app, not set")
        if k == "data":
            return value
        return "" if value is None else str(value)
    except (TypeError, ValueError) as e:
        raise ToolError(f"{p.name}: {e}")


def _summary(node):
    return {"name": node.name, "type": node.type_name, "pos": [round(node.pos().x()), round(node.pos().y())],
            "subtitle": node.subtitle(), "problem": node.problem()}


# ---- reading --------------------------------------------------------------------------------------------------
def _store_env():
    from .settings import _store, INITIAL_PYTHON_ENV
    return _store().value("default_python_env", INITIAL_PYTHON_ENV)


@side_by_side
@tool("The task: the prompt(s) connected to the Executor, with their node names (words the user typed in the node "
      "came in their message and come first), every input connected to it (pictures, videos, sounds, data - with "
      "node names, which say who / what they are), the models connected with the brain (yours to use: wire FROM "
      "their nodes), where to build by default, and the tabs that exist. Call this first.")
def get_task(ctx):
    node = ctx.node
    prompts = [{"from": n, "text": v} for n, v in node.input_named("text") if isinstance(v, str) and v.strip()]
    inputs = [{"name": n, "kind": k, "path": v, "facts": _media_facts(v)}  # the data input's items, in order
              for n, v, k in node.data_items("data") if isinstance(v, str) and v]
    brain = node.brain()
    others = [n for n in node.scene().nodes() if n is not node]
    box = None
    if others:
        xs = [n.pos().x() for n in others]
        ys = [n.pos().y() for n in others]
        box = [round(min(xs)), round(min(ys)), round(max(xs)), round(max(ys))]
    return {
        "prompts": prompts, "inputs": inputs,
        "brain": brain.name + " · " + (brain.subtitle() if brain.online else "a language model running here"),
        # wired in with the brain: models it may use - wire FROM these nodes into the nodes it builds
        "models_connected": [{"node": m.name, "role": m.subtitle(), "path": m.model_path()}
                             for m in node.other_models()],
        "executor": {"name": node.name, "tab": ctx.tab_name(node.scene()), "pos": [round(node.pos().x()),
                                                                                  round(node.pos().y())]},
        "build_in": {"tab": ctx.tab_name(ctx.target.scene), "note": "the Executor's tab unless the prompt says "
                     "otherwise - use_tab() changes it"},
        "occupied_area": box,
        "tabs": [d.name for d in _docs(ctx)],
        "layout": "nodes are 100 wide, 40-110 tall; inputs on top, outputs at the bottom - leave ~170 between "
                  "columns and ~150 between rows; data flows downward",
        "output_folder": ctx.output_dir(),
        "this_computer": _this_computer(),
    }


def _remote_gpu(env):
    """Local models of this app run on another machine (a pod): its GPU, as that machine reports it."""
    from .engine.remote import Target
    t = Target(env)
    try:
        gpu = t.run((f"CUDA_VISIBLE_DEVICES={t.gpu} " if t.gpu else "") + f"{t.venv}/bin/python -c \"import torch; "
                    "print('; '.join(f'{torch.cuda.get_device_name(i)}, {torch.cuda.mem_get_info(i)[1] / 2**30:.0f} GB' "
                    "for i in range(torch.cuda.device_count())))\"", timeout=120).strip()
    except Exception as e:  # noqa: BLE001
        gpu = f"not reachable now ({e})"
    return {"where": "a rented GPU machine reached over SSH (files go there and results come back by themselves; "
                     "this computer's own GPU is not used for local models)", "gpu": gpu}


def _this_computer():
    """What the computer has (read from Windows) and the tools set up in the app's Preferences."""
    from . import machine, settings
    from .engine.client import python_in
    facts = machine.facts()
    tools = settings.tools_folder()
    readme = next((os.path.join(tools, f) for f in ("README.md", "README.txt") if tools
                   and os.path.isfile(os.path.join(tools, f))), "")
    env = settings.default_python_env()
    return {**facts,
            "app_folder": os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "default_python_env": env if env and python_in(env) else "not set",
            **({"models_run_on": _remote_gpu(env)} if env.lower().startswith("ssh://") else {}),
            "blender": settings.blender_exe() or "not found",
            "tools_folder": ({"folder": tools, "readme": readme or "none"} if tools else
                             "none set (File › Preferences › External tools folder)")}


@side_by_side
@tool("Look at a picture (a file, or a node's picture / result), or at one frame of a video. Use it to see who is "
      "who, what a garment looks like, what a drawn mark on a picture points at.",
      path=("string", "file path (image or video)", False),
      node=("string", "a node whose file / result to look at, instead of path", False),
      time=("number", "a video: seconds into it (default: the middle)", False),
      max_side=("integer", "longest side of the picture you get (default 1024, at most 1568)", False),
      region=({"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
              "zoom into a part: [left, top, width, height] as fractions of the picture (0-1), e.g. a face to "
              "judge eyes and skin", False))
def look_at(ctx, path=None, node=None, time=None, max_side=1024, region=None):
    if node:
        n = _find(ctx, node)
        value = n.value() if hasattr(n, "value") else None
        path = value if isinstance(value, str) else n.params.get("file", "")
    if not path or not os.path.isfile(path):
        raise ToolError(f"no such file: {path!r}")
    ext = os.path.splitext(path)[1].lower()
    note = ""
    if ext in VIDEO_EXTS:
        from .nodes import probe
        dur = float(probe(path).get("format", {}).get("duration") or 0)
        t = dur / 2 if time is None else max(0.0, min(float(time), dur))
        frame = os.path.join(tempfile.gettempdir(), "NodeEditor", "look_frame.png")
        os.makedirs(os.path.dirname(frame), exist_ok=True)
        subprocess.run([shutil.which("ffmpeg"), "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", path, "-frames:v", "1",
                        frame], creationflags=NO_WINDOW)
        note, path = f" - frame at {t:.2f} s of {dur:.2f} s", frame
    img = QImage(path)
    if img.isNull():
        raise ToolError(f"can't read {path} as a picture")
    w, h = img.width(), img.height()
    if region:
        rx, ry, rw, rh = (max(0.0, min(1.0, float(v))) for v in region)
        box = (int(rx * w), int(ry * h), max(1, int(rw * w)), max(1, int(rh * h)))
        img = img.copy(*box)
        note += f" - region {box[2]}×{box[3]} px at ({box[0]}, {box[1]}), shown at its own resolution or less"
    side = max(64, min(int(max_side or 1024), 1568))
    if max(w, h) > side:
        img = img.scaled(side, side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.WriteOnly)
    img.convertToFormat(QImage.Format_RGB888).save(buf, "JPEG", 88)
    return [{"type": "image", "data": base64.b64encode(bytes(data)).decode("ascii"), "mimeType": "image/jpeg"},
            {"type": "text", "text": f"{os.path.basename(path)}: {w}×{h}{note}"}]


@side_by_side
@tool("Files in a folder (e.g. reference pictures the prompt points to), with sizes of pictures.",
      folder=("string", "folder path"), pattern=("string", "only names containing this (optional)", False))
def list_files(ctx, folder, pattern=""):
    if not os.path.isdir(folder):
        raise ToolError(f"no such folder: {folder}")
    out = []
    for name in sorted(os.listdir(folder), key=str.lower):
        if pattern and pattern.lower() not in name.lower():
            continue
        p = os.path.join(folder, name)
        out.append({"name": name, "path": p, "facts": _media_facts(p)})
        if len(out) >= 300:
            break
    return {"folder": folder, "files": out}


@side_by_side
@tool("Read a text file (a template, a script, notes, a README, a JSON) - wherever it is on the disk. Long files: "
      "a part at a time (start / length in characters).",
      path=("string", "the file"), start=("integer", "first character", False),
      length=("integer", "how many characters (at most 60000)", False))
def read_file(ctx, path, start=0, length=60000):
    if not os.path.isfile(path):
        raise ToolError(f"no such file: {path}")
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            text = f.read()
    except OSError as e:
        raise ToolError(f"can't read {path}: {e}")
    start, length = max(0, int(start)), max(1, min(int(length), 60000))
    part = text[start:start + length]
    return {"path": path, "chars": len(text), "from": start, "to": start + len(part), "text": part,
            **({"more": f"read on with start={start + len(part)}"} if start + len(part) < len(text) else {})}


def _measured(folder):
    """Renders of this model on this machine (logs/model_runs.jsonl): the latest few, as facts."""
    from .modality import RUNS_LOG
    try:
        with open(RUNS_LOG, encoding="utf-8") as f:
            runs = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return "no renders recorded yet"
    key = os.path.normcase(os.path.normpath(folder))
    mine = [r for r in runs if any(os.path.normcase(os.path.normpath(p)).startswith(key) for p in r.get("models", []))]
    if not mine:
        return "no renders recorded yet"
    out = []
    for r in mine[-4:]:
        s = r.get("settings", {})
        size = f"{s['width']}×{s['height']}, " if s.get("width") else ""
        steps = f"{s['steps']} steps, " if s.get("steps") else ""
        extra = f" (render {r['render_seconds']:.0f} s)" if r.get("render_seconds") else ""
        out.append(f"{size}{steps}{r.get('seconds', 0):.0f} s{extra} on {r.get('when', '')}")
    return out


@side_by_side
@tool("The models installed in the user's library (Hugging Face store): what each one's own files declare - task "
      "(model card pipeline_tag), class, card tags, licence, base model, size on disk, its parts, and the path to "
      "put in a Model node's File - and how long its renders took on this machine so far (measured_here; a "
      "model's first run includes loading it). Sizes hint at memory; the GPU has 32 GB. Nothing is guessed: 'not "
      "declared' means the files don't say - read_model_docs and model_code tell more. runs_here says when the "
      "app's engine can't run a model at all.")
def list_models(ctx):
    from .settings import all_models
    out = []
    for r in all_models():
        if r.kind not in ("models", "local") or not r.snapshot:
            continue
        cls = r.class_label
        parts = [name + ("" if present else " (not downloaded)") for name, c, present in r.components() if c]
        weights = [f.path for f in r.files if f.path.lower().endswith((".safetensors", ".gguf", ".pth", ".pt",
                                                                        ".ckpt", ".bin", ".onnx"))]
        if r.snapshot.lower().endswith(".onnx"):
            weights = [os.path.basename(r.snapshot)]
        # what the engine can't run at all (the Library says the same)
        runs = ("no - ONNX: the engine has no ONNX runner yet" if weights and all(w.lower().endswith(".onnx")
                for w in weights) else "")
        out.append({"repo": r.repo_id, "source": "the user's own (by path)" if r.kind == "local" else "Hugging Face store",
                    "task": r.task or "not declared", "class": cls[0] if cls else "not declared",
                    "tags": r.card.get("tags", ""), "license": r.card.get("license", ""),
                    "base_model": r.card.get("base_model", ""), "size_gb": round(r.total / 1024 ** 3, 1),
                    "incomplete": bool(r.incomplete), "parts": parts,
                    "weight_files": weights[:12] + (["…"] if len(weights) > 12 else []),
                    "path": r.snapshot, "measured_here": _measured(r.snapshot)} | ({"runs_here": runs} if runs else {}))
    return {"models": out, "how": "a whole diffusers repo (it has parts): one Model node with File = path. Single "
            "weight files (GGUF parts, upscalers): one Model node per file, File = path + '/' + weight file, "
            "several merged into a Merge node, then into the Modality's model input."}


def _repo_folder(repo):
    """A library repo id (or a folder / file path) -> the model's folder on disk."""
    from .settings import all_models
    if os.path.exists(repo):
        return repo if os.path.isdir(repo) else os.path.dirname(repo)
    r = next((r for r in all_models() if r.repo_id == repo and r.snapshot), None)
    if r is None:
        raise ToolError(f"no model {repo!r} in the library (list_models) and no such path")
    return r.snapshot


TEXT_EXTS = (".md", ".json", ".yaml", ".yml", ".txt", ".py", ".jinja", ".cfg", ".toml")


@side_by_side
@tool("A model's own documentation: its model card (README) - usage examples with the settings its makers use, "
      "limits, how inputs are referred to - and the list of its other text files (configs, scheduler, examples). "
      "Pass file to read one of them. Read this before choosing a model's settings.",
      repo=("string", "library repo id (list_models) or a model path"),
      file=("string", "a file inside the model folder (relative path), instead of the card", False))
def read_model_docs(ctx, repo, file=None):
    folder = os.path.realpath(_repo_folder(repo))
    if file:
        path = os.path.realpath(os.path.join(folder, file))
        if not path.startswith(folder + os.sep) or not os.path.isfile(path):
            raise ToolError(f"no file {file!r} in {repo}")
        if not path.lower().endswith(TEXT_EXTS):
            raise ToolError(f"{file} isn't a text file")
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        return text[:60000] + ("\n…(cut)" if len(text) > 60000 else "")
    card = ""
    readme = os.path.join(folder, "README.md")
    if os.path.isfile(readme):
        with open(readme, encoding="utf-8", errors="replace") as f:
            card = f.read()
    files = []
    for root, _dirs, names in os.walk(folder):
        for n in names:
            p = os.path.join(root, n)
            if n.lower().endswith(TEXT_EXTS) and n != "README.md" and os.path.getsize(p) < 2_000_000:
                files.append(f"{os.path.relpath(p, folder)} ({os.path.getsize(p)} bytes)")
    return {"repo": repo, "card": card[:40000] or "no README.md (model card) in the folder",
            "text_files": sorted(files)[:200]}


_CODE_SCRIPT = r'''
import inspect, json, os, re, sys
root, grep = sys.argv[1], sys.argv[2]
idx = json.load(open(os.path.join(root, "model_index.json"), encoding="utf-8"))
name = idx.get("_class_name", "")
import diffusers
cls = getattr(diffusers, name, None)
if cls is None:
    print(json.dumps({"error": f"diffusers {diffusers.__version__} has no class {name}"})); sys.exit()
src = inspect.getsourcefile(cls)
text = open(src, encoding="utf-8").read()
lines = text.splitlines()
out = {"class": name, "library": f"diffusers {diffusers.__version__}", "source": src}
if grep:
    hits = [i for i, l in enumerate(lines) if grep.lower() in l.lower()][:25]
    out["matches"] = ["\n".join(f"{j + 1}: {lines[j]}" for j in range(max(0, i - 4), min(len(lines), i + 5)))
                      for i in hits]
else:
    sig = inspect.signature(cls.__call__)
    out["call_parameters"] = {n: (repr(p.default) if p.default is not inspect.Parameter.empty else "required")
                              for n, p in sig.parameters.items() if n != "self"}
    out["call_doc"] = (inspect.getdoc(cls.__call__) or "")[:12000]
    out["constants"] = [l for l in lines if re.match(r"^[A-Z][A-Z0-9_]*\s*=", l)][:40]
    out["notes_in_code"] = [f"{i + 1}: {l.strip()}" for i, l in enumerate(lines)
                            if re.search(r"template|divisible|multiple of|max_area|min_area|resize|max_sequence", l,
                                         re.I)][:60]
print(json.dumps(out))
'''
_code_cache = {}


@side_by_side
@tool("What a model's code says about using it (read from the library installed in its Python env): the pipeline's "
      "settings with their defaults and documentation, its constants, and lines about prompt templates (e.g. how "
      "pictures are named to the model), size limits and resizing. grep = search its source for a word and get "
      "the lines around each hit. A GGUF model (stable-diffusion.cpp) gives sd-cli's own --help instead.",
      repo=("string", "library repo id or model path"),
      grep=("string", "a word to look for in the pipeline's source (optional)", False))
def model_code(ctx, repo, grep=""):
    folder = _repo_folder(repo)
    if repo.lower().endswith(".gguf") or not os.path.isfile(os.path.join(folder, "model_index.json")):
        if any(n.lower().endswith(".gguf") for n in os.listdir(folder)) or repo.lower().endswith(".gguf"):
            from .settings import sdcpp_exe
            p = subprocess.run([sdcpp_exe(), "--help"], capture_output=True, text=True, creationflags=NO_WINDOW,
                               timeout=60)
            return {"runs_with": sdcpp_exe(), "help": (p.stdout or p.stderr)[:40000]}
        raise ToolError(f"{repo}: no model_index.json (not a diffusers pipeline) - read_model_docs has what its files "
                        f"say")
    from .engine.client import python_in
    from .settings import default_python_env
    python = python_in(default_python_env())
    if not python or python.lower().startswith("ssh://"):  # models run on a pod: read the code with a local env
        from .settings import INITIAL_PYTHON_ENV
        python = python_in(_store_env()) or python_in(INITIAL_PYTHON_ENV)
    key = (python, folder, grep)
    if key not in _code_cache:
        p = subprocess.run([python, "-c", _CODE_SCRIPT, folder, grep or ""], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=NO_WINDOW, timeout=300,
                           env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        try:
            _code_cache[key] = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise ToolError(f"couldn't read the pipeline's code: {(p.stderr or p.stdout).strip()[-600:]}")
    return _code_cache[key]


@side_by_side
@tool("The node types there are: inputs, outputs and parameters (name, kind, default, choices) of each.")
def list_node_types(ctx):
    out = []
    for type_name, cls in NODE_TYPES.items():
        doc = " ".join((cls.__doc__ or "").split())[:300]
        params = []
        for p in cls.parameters:
            if p.kind in UI_ONLY:
                continue
            entry = {"name": p.name, "label": p.label, "kind": p.kind, "default": p.default}
            if p.choices():
                entry["choices"] = [str(v) for v, _l in p.choices()]
            params.append(entry)
        out.append({"type": type_name, "label": cls.label, "about": doc,
                    "inputs": [{"name": s[0], "type": s[1]} for s in cls.inputs],
                    "outputs": [{"name": s[0], "type": s[1]} for s in cls.outputs], "parameters": params})
    return {"types": out, "notes": "some parameters show only in some cases (e.g. a Model node's diffusion "
            "settings once its role is known) but can be set any time. A Prompt's text is its 'text'; an Image / "
            "Video / Audio node's file is 'file'. A Modality renders its model (input 'model') with its prompt "
            "(input 'text') and its data (input 'data': pictures, videos, sounds, JSON, other files). EVERY INPUT "
            "TAKES ONE WIRE: several pictures / videos / sounds / files go through a Data node (inputs '0'..'4' in "
            "order - Picture 1 = the first picture in that order; more than 5: a Data node into input '4' of another); "
            "several models (base + LoRA) or several prompts through a Merge node (same order rule). Isolate takes "
            "one item out of a list (Data / Merge) or one shot out of a shots JSON; Delete leaves one item out of a "
            "list. An output can feed any number of inputs."}


@side_by_side
@tool("One node in detail: its parameters (current values), inputs and what feeds them, and what the app says "
      "about it (detected role, problems).", node=("string", "node name"))
def describe_node(ctx, node):
    n = _find(ctx, node)
    params = {}
    for p in n.parameters:
        if p.kind in UI_ONLY:
            continue
        params[p.name] = {"value": n.params.get(p.name), "shown": bool(p.visible(n))}
    return {**_summary(n), "created_by_this_build": n in ctx.created, "tab": ctx.tab_name(n.scene()),
            "parameters": params,
            "inputs": [{"name": s.name, "type": s.type, "from": [f"{e.source.node.name}.{e.source.name}"
                                                                  for e in s.edges]} for s in n.input_sockets],
            "outputs": [{"name": s.name, "type": s.type} for s in n.output_sockets],
            "facts": [[str(a), str(b)] for a, b in n.info_rows()][:40]}


@side_by_side
@tool("The graph of a tab: its nodes (type, position, subtitle, problem) and wires.",
      tab=("string", "tab name (default: the tab being built in)", False))
def get_graph(ctx, tab=None):
    doc = ctx.doc_named(tab) if tab else ctx.target
    if doc is None:
        raise ToolError(f"no tab named {tab!r}")
    nodes = doc.scene.nodes()
    edges = [f"{e.source.node.name}.{e.source.name} -> {e.target.node.name}.{e.target.name}"
             for n in nodes for s in n.input_sockets for e in s.edges]
    return {"tab": doc.name, "nodes": [_summary(n) | {"created_by_this_build": n in ctx.created} for n in nodes],
            "wires": edges}


# ---- building -----------------------------------------------------------------------------------------------------
@tool("Build in another tab (created if it doesn't exist) - when the prompt says where the nodes should go.",
      name=("string", "tab name"))
def use_tab(ctx, name):
    doc = ctx.doc_named(name) or ctx.new_tab(name)
    ctx.set_target(doc)
    return {"building_in": doc.name, "nodes_there": len(doc.scene.nodes())}


@tool("Create a node. The name is kept exactly - a name already taken in the tab is refused. Name nodes after what "
      "they hold or do (hero_face, costume_prompt, hero_portrait).",
      type=("string", "node type (list_node_types)"), name=("string", "node name (no spaces)"),
      x=("number", "position x"), y=("number", "position y"))
def create_node(ctx, type, name, x, y):
    if type not in NODE_TYPES:
        raise ToolError(f"no node type {type!r}; types: {', '.join(NODE_TYPES)}")
    name = "_".join(str(name).split())
    scene = ctx.target.scene
    if not name or any(n.name == name for n in scene.nodes()):
        raise ToolError(f"the name {name!r} is taken in tab {ctx.target.name} - choose another")
    ctx.begin_macro(scene)
    node = scene.create_node(type, QPointF(float(x), float(y)))
    scene.rename_node(node, name)
    ctx.created.append(node)
    ctx.keep_executor_selected()
    return {**_summary(node), "inputs": [s.name for s in node.input_sockets],
            "outputs": [s.name for s in node.output_sockets]}


@tool("Set one parameter of a node created in this build (value as text / number / true-false; a choice by its "
      "value). A Model node reads its File when set - then its detected role and settings appear.",
      node=("string", "node name"), param=("string", "parameter name"),
      value=({"type": ["string", "number", "boolean", "null"]}, "the value"))
def set_param(ctx, node, param, value):
    n = _find(ctx, node)
    _own(ctx, n)
    p = _param(n, param)
    v = _coerce(n, p, value)
    if p.kind in ("file", "image", "video", "audio", "folder") and v and not os.path.exists(v):
        raise ToolError(f"{param}: no such file or folder: {v}")
    with undo.record(n, f"Executor: {n.name} {param}"):
        n.set_param(param, v)
    out = {"node": n.name, param: n.params[param], "subtitle": n.subtitle(), "problem": n.problem()}
    if not p.visible(n):  # kept, but the node doesn't use it as it is now (another mode / model role)
        out["warning"] = (f"{param} has NO effect on {n.name} as it is now - the node doesn't use it in its current "
                          "mode (describe_node: shown = false). A model's text comes from what is wired into "
                          "its text input (e.g. a Prompt node).")
    return out


@tool("Wire an output of one node into an input of a node created in this build. The user's nodes can be sources.",
      from_node=("string", "source node"), to_node=("string", "target node (created in this build)"),
      to_input=("string", "target input name"),
      from_output=("string", "source output name (default: its first)", False))
def connect(ctx, from_node, to_node, to_input, from_output=None):
    a, b = _find(ctx, from_node), _find(ctx, to_node)
    _own(ctx, b)
    if a.scene() is not b.scene():
        raise ToolError(f"{a.name} and {b.name} are in different tabs - wires stay within a tab")
    out = next((s for s in a.output_sockets if s.name == from_output), None) if from_output else \
        (a.output_sockets[0] if a.output_sockets else None)
    inp = next((s for s in b.input_sockets if s.name == to_input), None)
    if out is None or inp is None:
        raise ToolError(f"sockets: {a.name} outputs {[s.name for s in a.output_sockets]}, "
                        f"{b.name} inputs {[s.name for s in b.input_sockets]}")
    scene = b.scene()
    if inp.edges:
        raise ToolError(f"{b.name}.{inp.name} already has a wire (from {inp.edges[0].source.node.name}) - an input "
                        f"takes one (disconnect frees it). For several: a {'Data' if inp.type == 'data' else 'Merge'} node (its inputs "
                        f"0-4 in order), then it into {b.name}.{inp.name}; more than 5: another one into the first "
                        f"one's input 4.")
    if not scene.can_connect(out, inp):
        raise ToolError(f"{a.name}.{out.name} ({out.type}) can't go into {b.name}.{inp.name} ({inp.type}) - "
                        f"wrong type, already connected, or it would make a loop")
    ctx.begin_macro(scene)
    scene.connect_user(out, inp)
    return {"connected": f"{a.name}.{out.name} -> {b.name}.{inp.name}", "target": _summary(b)}


# ---- batches: many nodes / wires / settings in one call - all or nothing ------------------------------------------
def _build(ctx, scene, specs, wires):
    """Create the nodes in `specs` [{type, name, x, y, params (checked like set_param), raw (copied as they are)}] in
    `scene` and the `wires` [{from, to, input, output?}] into them. Any mistake: nothing stays (the nodes made are
    removed) and every mistake found is listed."""
    errors, taken, seen = [], {n.name for n in scene.nodes()}, set()
    for i, s in enumerate(specs):
        name = "_".join(str(s.get("name") or "").split())
        s["name"] = name
        if s.get("type") not in NODE_TYPES:
            errors.append(f"nodes[{i}] {name}: no node type {s.get('type')!r}")
        if not name or name in taken or name in seen:
            errors.append(f"nodes[{i}]: the name {name!r} is taken in tab {_tab(ctx, scene)}")
        seen.add(name)
    if errors:
        raise ToolError("nothing was built: " + "; ".join(errors))
    ctx.begin_macro(scene)
    made = {}
    try:
        for s in specs:
            node = scene.create_node(s["type"], QPointF(float(s.get("x", 0)), float(s.get("y", 0))))
            scene.rename_node(node, s["name"])
            ctx.created.append(node)
            made[s["name"]] = node
        for s in specs:
            node = made[s["name"]]
            raw = {k: v for k, v in (s.get("raw") or {}).items() if node.accepts_param(k)}
            node.params.update(raw)
            for k in raw:
                node.param_changed(k)
            for k, v in (s.get("params") or {}).items():
                try:
                    p = _param(node, k)
                    v = _coerce(node, p, v)
                    if p.kind in ("file", "image", "video", "audio", "folder") and v and not os.path.exists(v):
                        raise ToolError(f"no such file or folder: {v}")
                    node.set_param(k, v)
                except ToolError as e:
                    errors.append(f"{node.name}.{k}: {e}")
        scene.graph_changed()  # output types settle (they follow inputs), then the wires
        for i, w in enumerate(wires):
            try:
                _wire(ctx, scene, made, w)
            except ToolError as e:
                errors.append(f"wires[{i}] {w.get('from')} -> {w.get('to')}.{w.get('input')}: {e}")
    except Exception as e:  # noqa: BLE001 - never leave half a batch behind
        errors.append(f"{type(e).__name__}: {e}")
    if errors:
        nodes = [n for n in made.values() if n.scene() is not None]
        if nodes:
            edges = [e for n in nodes for s in n.sockets for e in s.edges]
            scene.undo_stack.push(undo.RemoveItems(scene, nodes, edges, "Executor: batch undone"))
        ctx.created[:] = [n for n in ctx.created if n not in made.values()]
        raise ToolError("nothing was built - fix these and send the whole batch again: " + "; ".join(errors))
    ctx.keep_executor_selected()
    return made


def _tab(ctx, scene):
    return next((d.name for d in _docs(ctx) if d.scene is scene), "?")


def _wire(ctx, scene, made, w):
    """One wire of a batch: into a node of the batch, from one of the batch or any node in the same tab."""
    b = made.get(w.get("to"))
    if b is None:
        raise ToolError("a batch wires only INTO its own new nodes (connect wires into the others)")
    a = made.get(w.get("from")) or scene.node_named(w.get("from") or "")
    if a is None:
        raise ToolError(f"no node {w.get('from')!r} in tab {_tab(ctx, scene)} (wires stay within a tab)")
    out = next((s for s in a.output_sockets if s.name == w["output"]), None) if w.get("output") else \
        (a.output_sockets[0] if a.output_sockets else None)
    inp = next((s for s in b.input_sockets if s.name == w.get("input")), None)
    if out is None or inp is None:
        raise ToolError(f"sockets: {a.name} outputs {[s.name for s in a.output_sockets]}, "
                        f"{b.name} inputs {[s.name for s in b.input_sockets]}")
    if inp.edges:
        raise ToolError(f"{b.name}.{inp.name} already has a wire - an input takes one (several: a Data / Merge node)")
    if not scene.can_connect(out, inp):
        raise ToolError(f"{a.name}.{out.name} ({out.type}) can't go into {b.name}.{inp.name} ({inp.type})")
    scene.connect_user(out, inp)


def _built_answer(made):
    return {"built": len(made), "nodes": [_summary(n) for n in made.values()],
            "problems": {n.name: n.problem() for n in made.values() if n.problem()}}


_NODE_SPEC = {"type": "object", "properties": {
    "type": {"type": "string"}, "name": {"type": "string"}, "x": {"type": "number"}, "y": {"type": "number"},
    "params": {"type": "object", "description": "parameter -> value, as set_param takes them"}},
    "required": ["type", "name", "x", "y"]}
_WIRE_SPEC = {"type": "object", "properties": {
    "from": {"type": "string"}, "output": {"type": "string", "description": "optional: the first output"},
    "to": {"type": "string"}, "input": {"type": "string"}}, "required": ["from", "to", "input"]}


@tool("Build many nodes in ONE call: create them, set their parameters and wire them (wires go into the new nodes, "
      "from new nodes or any node in the same tab). All or nothing: if anything is wrong, nothing is built and every "
      "mistake is listed - fix them and send the whole batch again. Builds in the current tab (use_tab), or `tab`.",
      nodes=({"type": "array", "items": _NODE_SPEC}, "the new nodes"),
      wires=({"type": "array", "items": _WIRE_SPEC}, "the wires into them", False),
      tab=("string", "tab to build in (created if missing); default: the current one", False))
def build(ctx, nodes, wires=None, tab=""):
    doc = (ctx.doc_named(tab) or ctx.new_tab(tab)) if tab else ctx.target
    specs = [{**s} for s in nodes]
    return _built_answer(_build(ctx, doc.scene, specs, list(wires or [])))


@tool("Copy a set of nodes (e.g. one finished, tested shot) with changes, in ONE call: new names, a shift, other "
      "settings per copy. Wires between the copied nodes are copied; wires coming in from nodes outside the set are "
      "copied too when the copy is in the same tab (into another tab: include those nodes in the set, e.g. the "
      "Model nodes - wires stay within a tab). Results are not copied. All or nothing, like build. The user's nodes "
      "may be copied too (the copies are yours).",
      nodes=({"type": "array", "items": {"type": "string"}}, "the nodes to copy"),
      rename=({"type": "object", "description": "old name -> new name"}, "new names (else old name + suffix)", False),
      suffix=("string", "added to every name not in rename, e.g. _s05", False),
      dx=("number", "shift right", False), dy=("number", "shift down", False),
      tab=("string", "tab to copy into (created if missing); default: the current one", False),
      params=({"type": "object", "description": "new name -> {parameter: value}"}, "settings of the copies", False))
def copy_nodes(ctx, nodes, rename=None, suffix="", dx=0, dy=0, tab="", params=None):
    rename, params = dict(rename or {}), dict(params or {})
    sources = [_find(ctx, n) for n in nodes]
    if len({n.scene() for n in sources}) > 1:
        raise ToolError("copy nodes of one tab at a time")
    src_scene = sources[0].scene() if sources else None
    doc = (ctx.doc_named(tab) or ctx.new_tab(tab)) if tab else ctx.target
    missing = [n.name for n in sources if n.name not in rename and not suffix]
    if missing and doc.scene is src_scene:
        raise ToolError(f"new names needed (rename or suffix) for: {missing}")
    new = {n.name: rename.get(n.name) or n.name + suffix for n in sources}
    specs, wires, not_wired = [], [], []
    for n in sources:
        kinds = {p.name: p.kind for p in n.parameters}
        raw = {k: v for k, v in n.params.items() if v is not None and kinds.get(k) not in undo.NOT_RECORDED
               and k not in n.not_copied}
        specs.append({"type": n.type_name, "name": new[n.name], "x": n.pos().x() + float(dx),
                      "y": n.pos().y() + float(dy), "raw": raw, "params": params.get(new[n.name]) or {}})
        for inp in n.input_sockets:
            for e in inp.edges:
                src = e.source.node
                w = {"to": new[n.name], "input": inp.name, "output": e.source.name}
                if src.name in new:
                    wires.append({**w, "from": new[src.name]})
                elif doc.scene is src_scene:
                    wires.append({**w, "from": src.name})
                else:
                    not_wired.append(f"{src.name}.{e.source.name} -> {new[n.name]}.{inp.name}")
    unknown = [k for k in params if k not in new.values()]
    if unknown:
        raise ToolError(f"params name nodes that aren't copies: {unknown} (copies: {list(new.values())})")
    out = _built_answer(_build(ctx, doc.scene, specs, wires))
    if not_wired:
        out["not_wired"] = {"wires": not_wired, "why": "their sources are in another tab - copy those nodes too, "
                                                       "or wire new ones in"}
    return out


@tool("Set many parameters in ONE call (nodes created in this build). All or nothing: if any change is wrong, none "
      "is made and every mistake is listed.",
      changes=({"type": "array", "items": {"type": "object", "properties": {
          "node": {"type": "string"}, "param": {"type": "string"},
          "value": {"type": ["string", "number", "boolean", "null"]}}, "required": ["node", "param", "value"]}},
          "the changes"))
def set_params(ctx, changes):
    todo, errors = [], []
    for i, c in enumerate(changes):
        try:
            n = _find(ctx, c.get("node", ""))
            _own(ctx, n)
            p = _param(n, c.get("param", ""))
            v = _coerce(n, p, c.get("value"))
            if p.kind in ("file", "image", "video", "audio", "folder") and v and not os.path.exists(v):
                raise ToolError(f"no such file or folder: {v}")
            todo.append((n, p, v))
        except ToolError as e:
            errors.append(f"changes[{i}] {c.get('node')}.{c.get('param')}: {e}")
    if errors:
        raise ToolError("nothing was changed: " + "; ".join(errors))
    unused = []
    for n, p, v in todo:
        ctx.begin_macro(n.scene())
        with undo.record(n, f"Executor: {n.name} {p.name}"):
            n.set_param(p.name, v)
        if not p.visible(n):
            unused.append(f"{n.name}.{p.name}")
    out = {"changed": len(todo), "nodes": {n.name: {"subtitle": n.subtitle(), "problem": n.problem()}
                                           for n in dict.fromkeys(n for n, _p, _v in todo)}}
    if unused:
        out["warning"] = f"no effect as the nodes are now (their current mode doesn't use them): {unused}"
    return out


@tool("Remove the wire going into an input of a node created in this build (the input is free again; nothing "
      "is deleted).",
      to_node=("string", "the node the wire goes into"), to_input=("string", "that node's input"))
def disconnect(ctx, to_node, to_input):
    b = _find(ctx, to_node)
    _own(ctx, b)
    inp = next((s for s in b.input_sockets if s.name == to_input), None)
    if inp is None:
        raise ToolError(f"{b.name} inputs: {[s.name for s in b.input_sockets]}")
    if not inp.edges:
        raise ToolError(f"{b.name}.{inp.name} has no wire")
    scene = b.scene()
    ctx.begin_macro(scene)
    edge = inp.edges[0]
    source = f"{edge.source.node.name}.{edge.source.name}"
    scene.undo_stack.push(undo.RemoveItems(scene, [], [edge], f"Executor: disconnect {b.name}.{inp.name}"))
    return {"disconnected": f"{source} -> {b.name}.{inp.name}", "target": _summary(b)}


@tool("Delete a node created in this build (e.g. a mistake).", node=("string", "node name"))
def delete_node(ctx, node):
    n = _find(ctx, node)
    _own(ctx, n)
    scene = n.scene()
    ctx.begin_macro(scene)
    edges = [e for s in n.sockets for e in s.edges]
    scene.undo_stack.push(undo.RemoveItems(scene, [n], edges, f"Executor: delete {n.name}"))
    ctx.created.remove(n)
    return f"deleted {n.name}"


@side_by_side
@tool("Render ONE node built in this build (a Modality) to test the setup - what it needs (an upstream Modality "
      "without a result) is rendered first - and wait for it (minutes; up to 30). Returns how it went, the result "
      "file and the budget left; then look_at it (region = zoom into faces, hands, text) and judge it against the "
      "request. Several render_node calls in one message run at the same time (paid online jobs: a few per "
      "service at once, the rest wait unsent until a place is free; local models take turns on the GPU), and you "
      "can look_at results while others still render; stop_render calls off the rest. Test as much as the "
      "request asks for and the time allows (measured_here says how long a render takes); render_built renders "
      "everything you built.",
      node=("string", "a Modality (or an Edit Media fed by one) created in this build"))
def render_node(ctx, node):
    from .modality import ModalityNode, upstream_modalities
    n = _find(ctx, node)
    _own(ctx, n)
    if not isinstance(n, ModalityNode):
        return _render_other(ctx, n, upstream_modalities)
    if n.running:
        raise ToolError(f"{n.name} is rendering already")
    _check_budget(ctx, [n] + n.missing_inputs())
    before = {m: m.value() for m in ctx.created if isinstance(m, ModalityNode)}
    n.render()
    if not n.running:
        raise ToolError(f"{n.name} can't render: {n.problem() or n.state or 'not ready'}")
    ctx.log(f"  rendering {n.name}…")
    started = time.time()

    def check():
        if n.scene() is None:
            return f"{n.name} was deleted"
        if n.running:
            return None
        made = [f"{m.name}: {m.state}" for m in before if m is not n and m.value() != before[m]]
        return {"node": n.name, "state": n.state, "result": n.value(), "seconds": round(time.time() - started),
                "also_rendered_first": made,
                **_budget_left(ctx, [n] + [m for m in before if m.value() != before[m]]),
                "next": "look_at the result (region to zoom into details); fix what is wrong and test again, or "
                        "finish" if n.value() else "read the state: the render failed"}
    return Later(check, timeout=1800)


def _render_other(ctx, n, upstream_modalities):
    """render_node on another node that makes a result (e.g. Edit Media): the Modalities feeding it that have no
    result yet render first (a chain), then the node makes its own."""
    if not hasattr(n, "value") or not n.input_sockets:
        raise ToolError(f"{n.name} is a {n.type_name} - it doesn't render (a Modality or an Edit Media does)")
    ups = [m for m in upstream_modalities(n) if not m.value() and not m.running]
    _check_budget(ctx, ups)
    for m in ups:
        m.render()
        if not m.running:
            raise ToolError(f"{m.name} (feeding {n.name}) can't render: {m.problem() or m.state or 'not ready'}")
    if ups:
        ctx.log(f"  rendering {', '.join(m.name for m in ups)} for {n.name}…")
    started = time.time()

    def check():
        if n.scene() is None:
            return f"{n.name} was deleted"
        feeding = upstream_modalities(n)
        if any(m.running for m in feeding):
            return None
        failed = [f"{m.name}: {m.state}" for m in feeding if not m.value()]
        if failed:
            return {"node": n.name, "state": "not made - what feeds it has no result", "feeding": failed}
        value = n.value()
        return {"node": n.name, "result": value if isinstance(value, str) else None,
                "problem": n.problem() or None, "rendered_first": [m.name for m in ups],
                "seconds": round(time.time() - started), **_budget_left(ctx, ups),
                "next": "look_at the result" if value else "read the problem: nothing was made"}
    return Later(check, timeout=1800)


@tool("Render everything you built (every Modality you created), by stage: what feeds another renders first. "
      "Results that already exist are kept unless again=true (or the user ticked Overwrite results), so a run "
      "picks up where the last one stopped. Without wait: starts and answers at once - the outcome (per node, with "
      "files) comes with the user's next message and is in the Log; don't change the nodes while they render. With "
      "wait: answers when all are done (up to 30 min) - for a run whose results you want to look at now.",
      again=("boolean", "render again what already has a result", False),
      wait=("boolean", "wait for the results before answering", False))
def render_built(ctx, again=False, wait=False):
    from .modality import ModalityNode
    if ctx.node.rendering:
        raise ToolError("a run of what was built is going on already - wait for it, or the user stops it")
    mods = [n for n in ctx.created if isinstance(n, ModalityNode) and n.scene() is not None]
    if not mods:
        raise ToolError("nothing to render: you haven't built a Modality yet")
    _check_budget(ctx, [m for m in mods if again or not m.value()])
    out = ctx.node.render_built(mods, again=bool(again), wait=bool(wait))
    if not ctx.node.rendering or not wait:
        if ctx.node.rendering:
            out["note"] = "started - the outcome comes with the user's next message"
        return out
    started = time.time()

    def check():
        if ctx.node.rendering and time.time() - started < 29 * 60:
            return None
        if ctx.node.rendering:  # too long to wait: the whole outcome comes as news when the run ends
            ctx.node._run_wait = False
            return {"still_rendering": True, "so_far": list(ctx.node._run_lines),
                    "note": "the rest comes with the user's next message"}
        return {**out, "outcome": list(ctx.node._run_lines), **_budget_left(ctx, mods)}
    return Later(check, timeout=1800)


@tool("Stop renders of nodes you built (e.g. the rest of a batch once the first results show a mistake). A paid job "
      "still waiting for its turn on the service was never sent: stopping it costs nothing; one already running "
      "is cancelled on the service, which may still bill the part it ran.",
      nodes=({"type": "array", "items": {"type": "string"}}, "the Modalities to stop"))
def stop_render(ctx, nodes):
    out = {}
    for name in nodes:
        n = _find(ctx, name)
        _own(ctx, n)
        if not getattr(n, "running", False):
            out[n.name] = "not rendering"
            continue
        n.action("stop")
        out[n.name] = n.state or "stopped"
    return out


@tool("Unload the models your renders loaded: frees the GPU and RAM (they load again on the next render). Refused "
      "while a render runs.")
def free_memory(ctx):
    from .modality import ModalityNode
    ctx.node.free_memory([n for n in ctx.created if isinstance(n, ModalityNode) and n.scene() is not None])
    return ctx.node.state


@tool("Hand the work over to a fresh session of you (your context has grown long: every step re-reads it, which "
      "costs more and blurs details). Write a short note: what is done, what comes next, decisions made and the "
      "user's wishes so far, open problems, and the files to read first (production table, method, scripts). Then "
      "end your turn with one short line; a fresh session starts with your note, your nodes and what happened since. "
      "Nothing is stopped - renders keep running. Best at a stage boundary.",
      note=("string", "the handover note for your successor"))
def handover(ctx, note):
    if len(note.strip()) < 40:
        raise ToolError("the note is too short for a successor to continue from - say what is done, what is next, "
                        "the decisions and wishes so far, and which files to read")
    ctx.request_handover(note)
    return "Handover accepted: end your turn now with one short line - a fresh session continues from your note."


@tool("The build is done. The app then checks every node created (the problems they report are returned - fix "
      "them, or finish again if they are fine). Then tell the user in your reply; they may write again later "
      "(e.g. to render it) - the conversation goes on.",
      summary=("string", "one line for the app's log: what was built"))
def finish(ctx, summary):
    problems = {n.name: n.problem() for n in ctx.created if n.scene() is not None and n.problem()}
    ctx.on_finish(summary, problems)
    if problems:
        return {"problems": problems, "note": "fix them and call finish again, or call finish again if they are "
                "expected (e.g. a result that only exists after rendering)"}
    return "Build complete. Now end your turn with your reply to the user."



# ---- online services (Replicate, Google...): paid models, what they take and cost --------------------------------
def _service_models():
    from . import costs, online, services
    out = []
    for e in online.entries():
        s = services.entry(e["provider"])
        if s is None or s["protocol"] == services.CLAUDE_CODE:
            continue
        d = online.info(e) or {}
        out.append({"name": e["name"], "service": s["name"], "model": e["model"], "family": d.get("family", ""),
                    "about": (d.get("description") or "")[:300],
                    "inputs": [{**{k: f[k] for k in ("name", "type", "media", "multi", "required", "default", "options")
                                   if f.get(k) not in (None, False, "")},
                                **({"about": f["description"][:120]} if f.get("description") else {})}
                               for f in d.get("fields") or []],
                    "price": [f"{m} ({c})" for c, m in costs.table_rows(d.get("price"))] or ["not shown by the service"],
                    "details": "asked" if d else "not asked yet - add_online_model asks again"})
    return out


@side_by_side
@tool("Paid ONLINE models (services such as Replicate, Google) set up in the app: what each takes (its inputs, from "
      "the service's own schema) and costs (the service's price table), the services and the budget. To use one: a "
      "Model node with runs_on=online, provider=<service>, online_model=<name>; its own settings are params named "
      "in:<input>; the Modality's prompt goes to its text input and its data items to its file inputs (a node NAMED "
      "like an input goes to that input; the others of a kind to that kind's main input). Every paid render needs "
      "the budget (render_node refuses beyond it): say what a run costs before asking for it. search_online_models "
      "finds more; local models (list_models) cost nothing but GPU time.")
def list_online_models(ctx):
    from . import costs, online, services
    svc = [{"service": s["name"], "label": s["label"], "status": services.status(s["name"]).get("state"),
            "gives": services.PROTOCOLS.get(s["protocol"], ("", "", ""))[2]}
           for s in services.entries() if s["protocol"] != services.CLAUDE_CODE]
    # models on the user's own plan (Claude Code): no cost per run; in a Modality (normal mode) one plain request
    plan = [{"name": e["name"], "model": e["model"] or "Claude Code's default", "service": e["provider"],
             "takes": "text and pictures (videos, sounds and other files are not sent)", "price": "$0 per run (the "
             "user's plan; uses its limits)"} for e in online.entries()
            if (services.entry(e["provider"]) or {}).get("protocol") == services.CLAUDE_CODE]
    return {"services": svc, "models": _service_models(), "on_the_users_plan": plan,
            "budget": {**_money(ctx), "note": "per service, set by the user on the Executor (Budget <service> $); "
                                              "0 = ask before any paid run there; counts this conversation's runs"},
            "spent_all_time": round(costs.spent(), 2)}


@side_by_side
@tool("Search a service's catalogue (e.g. Replicate: 'video edit', 'lip sync', 'image to 3d'; Google: 'image', "
      "'veo', 'tts'). With model=<id>: that model's inputs and price instead. Found models are added with "
      "add_online_model.",
      service=("string", "service name (list_online_models)"), query=("string", "words to search for", False),
      model=("string", "a model id to read in detail", False))
def search_online_models(ctx, service, query="", model=""):
    from . import costs, services
    s = services.entry(service)
    if s is None:
        raise ToolError(f"no service {service!r}; set up: {[x['name'] for x in services.entries()]}")
    mod = services.protocol_module(s["protocol"])
    if mod is None or not hasattr(mod, "search"):
        raise ToolError(f"{s['label']} has no catalogue to search")
    token = services.key(s)
    holder = {}

    def work():
        if model:
            d = mod.model(token, model)
            d["price"] = mod.price(model, s)
            return d
        return mod.search(token, query) if query else []
    services.in_background(work, done=lambda r, e: holder.update(r=r, e=e))

    def check():
        if not holder:
            return None
        if holder["e"]:
            return f"{s['label']}: {holder['e']}"
        r = holder["r"]
        if model:
            return {"model": model, "about": r.get("description", ""), "inputs": r.get("fields"),
                    "price": [f"{m} ({c})" for c, m in costs.table_rows(r.get("price"))] or ["not shown"]}
        return {"found": [{"model": x["id"], "about": x["description"][:200], "runs": x["runs"]} for x in r[:40]]}
    return Later(check, timeout=120)


@tool("Add a service's model to the user's online models (a settings file, listed in the Library) and ask the "
      "service what it takes and costs. Then use it in a Model node (list_online_models shows how).",
      service=("string", "service name"), model=("string", "the service's model id, e.g. owner/name"),
      label=("string", "a short name (optional)", False))
def add_online_model(ctx, service, model, label=""):
    from . import online, services
    if services.entry(service) is None:
        raise ToolError(f"no service {service!r}")
    same = next((e for e in online.entries() if e["provider"] == service and e["model"] == model), None)
    name = same["name"] if same else online.add_entry(service, model, label or model.split("/")[-1])
    holder = {}
    online.refresh_info(online.entry(name), done=lambda d, e: holder.update(d=d, e=e))
    ctx.log(f"  added online model {name} ({service}: {model})")
    if getattr(ctx, "window", None) is not None and hasattr(ctx.window, "library"):
        ctx.window.library.refresh_online()

    def check():
        if not holder:
            return None
        if holder["e"]:
            return f"added as {name}, but its details failed: {holder['e']}"
        return {"added": name, **next((m for m in _service_models() if m["name"] == name), {})}
    return Later(check, timeout=120)


@side_by_side
@tool("What a built Modality with an online (paid) model would send and cost: which node goes to which input, "
      "what is not sent, what is missing, and the price for its settings. Check before render_node.",
      node=("string", "a Modality"))
def estimate_cost(ctx, node):
    n = _find(ctx, node)
    if not hasattr(n, "service_preview"):
        raise ToolError(f"{n.name} is not a Modality")
    rows = n.service_preview()
    if not rows:
        return f"{n.name} runs no online service model - it costs nothing but time here"
    return {"node": n.name, "preview": [f"{k}: {v}" if k else v for k, v in rows],
            "budget": _money(ctx).get(_service_of(n))}


def _money(ctx):
    """Per service set up: the budget the user gave, what this conversation's paid runs cost, what runs still going
    will cost (estimates), and what is left."""
    from . import services
    out = {}
    for e in services.entries():
        if e["protocol"] == services.CLAUDE_CODE:
            continue
        name = e["name"]
        budget, spent, going = ctx.node.budget(name), ctx.node.spent(name), ctx.node.committed(name)
        out[name] = {"budget": budget, "spent": round(spent, 4), "running": round(going, 4),
                     "left": round(budget - spent - going, 4)}
    return out


def _budget_left(ctx, mods):
    """{"budget_left": {service: dollars}} for the services these renders used - for a render's answer."""
    used = {_service_of(m) for m in mods} - {""}
    money = _money(ctx)
    return {"budget_left": {s: money[s]["left"] for s in used if s in money}} if used else {}


def _service_of(m):
    """The service a Modality's model runs on (Replicate, Google...), else "" (local, or Claude Code)."""
    model = m.online_model() if hasattr(m, "online_model") else None
    return (model.params.get("provider") or "") if model is not None and model.service_entry() is not None else ""


def _check_budget(ctx, mods):
    """Every render the brain orders counts for this Executor (also once the node is deleted or renamed); paid ones
    beyond a service's budget are refused (the brain must ask the user first). Runs still going count with their
    estimate, so renders asked for together can't overspend."""
    from . import services
    paid = {}  # service -> [(node, estimate)]
    for m in mods:
        m.ordered_by = ctx.node.name
        if hasattr(m, "_service_run") and m._service_run() is not None:
            paid.setdefault(_service_of(m), []).append((m, m._service_run().estimate()))
    refused = []
    for svc, runs in paid.items():
        label = (services.entry(svc) or {}).get("label") or svc
        lines = [f"{m.name}: {est['text']}" if est and est["high"] is not None else
                 f"{m.name}: price not known before the run" for m, est in runs]
        unknown = any(not est or est["high"] is None for _m, est in runs)
        # a token-billed run with pictures: its worst case is the model's whole input limit - counted at what such
        # runs really cost here (est["likely"], from the spend log) once there are enough of them
        total = sum(est.get("likely", est["high"]) for _m, est in runs if est and est["high"] is not None)
        budget, spent, going = ctx.node.budget(svc), ctx.node.spent(svc), ctx.node.committed(svc)
        left = budget - spent - going
        if unknown or total > left + 1e-9:
            refused.append(f"{label} - " + "; ".join(lines) + f". Budget ${budget:.2f}, spent ${spent:.2f}"
                           + (f", running ≈ ${going:.2f}" if going else "") + f", left ${left:.2f}")
    if refused:
        raise ToolError("paid run beyond the budget: " + " | ".join(refused) + f". Tell the user what it costs and "
                        f"why it is worth it, then wait: they raise that service's Budget on {ctx.node.name} (or "
                        f"say no). Cheaper settings or a local model may do.")
    for svc, runs in paid.items():
        for m, est in runs:
            ctx.node._ordered[m] = (svc, est.get("likely", est["high"]) if est else 0.0)
