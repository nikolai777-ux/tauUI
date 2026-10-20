"""Shot boundary detection: a video in, a .shots file (JSON) a person can read out.

Which models run here is read from the model's own card (README.md front matter): the tag
`shot-boundary-detection`, and the library its usage code imports. So far the engine knows how to call one such
library, `omnishotcut` (OmniShotCut - load(weight file) -> .inference(video, mode="default")).

<name>.json with "format": "shots" (frame numbers 1-based, first and last included = the Edit Media timeline; seconds from the video
stream's own frame rate):
  {"about", "video", "fps", "fps_value", "frames", "seconds", "detector",
   "shots": [{"shot", "first", "last", "frames", "start", "end", "seconds", "begins", "transition"}],
   "transitions": [{"after_shot", "before_shot", "type", "first", "last", "frames", "start", "end"}]}
begins: video_start / hard_cut / sudden_jump / after_transition (the model's own relation label otherwise);
transition: the type of the transition just before the shot (dissolve, fade, wipes... - the library's names), or null.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import time
from fractions import Fraction


def card(folder):
    """(front matter text, whole card) of a model card README.md, or ('', '')."""
    try:
        with open(os.path.join(folder, "README.md"), encoding="utf-8", errors="replace") as f:
            text = f.read(60000)
    except OSError:
        return "", ""
    front = text.split("---", 2)[1] if text.startswith("---") else ""
    return front, text


def card_tags(front):
    """The `tags:` list of a card's front matter."""
    block = re.search(r"^tags:[ \t]*\n((?:[ \t]*-[ \t]*.*\n?)+)", front, re.M)
    inline = re.search(r"^tags:[ \t]*\[(.*)\]", front, re.M)
    if block:
        return [t.strip(" -'\"\t") for t in block.group(1).strip().splitlines()]
    if inline:
        return [t.strip(" '\"") for t in inline.group(1).split(",")]
    return []


def card_imports(text):
    """Top-level packages the card's usage code imports (```python blocks), in order."""
    names = []
    for block in re.findall(r"```(?:python|py)\s*\n(.*?)```", text, re.S):
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_]\w*)", block, re.M):
            if m.group(1) not in names:
                names.append(m.group(1))
    return names


def detector(folder):
    """The library a shot-boundary-detection model card says to run it with, when that library is installed:
    (library, None) | (None, reason) | None when the card isn't tagged shot-boundary-detection."""
    front, text = card(folder)
    if "shot-boundary-detection" not in card_tags(front):
        return None
    imports = card_imports(text)
    installed = [n for n in imports if importlib.util.find_spec(n) is not None]
    if "omnishotcut" in installed:
        return "omnishotcut", None
    if not imports:
        return None, "a shot boundary detection model whose card has no usage code naming its library"
    missing = [n for n in imports if n not in installed]
    if missing:
        return None, f"a shot boundary detection model whose card uses {', '.join(missing)} (not installed in this env)"
    return None, f"a shot boundary detection model run with {', '.join(imports)} (the engine can't call that yet)"


def weight_file(path):
    """The checkpoint to load: the file itself, or the one the folder's config.json names ("weight_file")."""
    if os.path.isfile(path):
        return path
    try:
        with open(os.path.join(path, "config.json"), encoding="utf-8") as f:
            name = json.load(f).get("weight_file")
    except (OSError, ValueError):
        name = None
    if name and os.path.isfile(os.path.join(path, name)):
        return os.path.join(path, name)
    raise ValueError(f"{path}: no weight file (config.json names none that exists)")


def stream_info(video):
    """Frame rate (as recorded, e.g. '24000/1001'), width, height of the file's first video stream (ffprobe)."""
    probe = shutil.which("ffprobe")
    if not probe:
        raise RuntimeError("ffprobe is not on PATH")
    p = subprocess.run([probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=avg_frame_rate,r_frame_rate,width,height", "-of", "json", video],
                       capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    st = json.loads(p.stdout or "{}").get("streams", [{}])[0]
    rate = next((r for r in (st.get("avg_frame_rate"), st.get("r_frame_rate")) if r and r not in ("0/0", "0")), "")
    return rate, st.get("width"), st.get("height")


def describe(ranges, intra, inter, fps):
    """The detector's ranges ([start, end) 0-based frames, labels per range) as readable shots + transitions."""
    def span(a, b):  # 0-based [a, b) -> 1-based first/last + seconds
        return {"first": a + 1, "last": b, "frames": b - a,
                "start": round(a / fps, 3), "end": round(b / fps, 3)}

    shots, transitions = [], []
    pending = None  # a transition waiting for the shot after it
    for (a, b), kind, relation in zip(ranges, intra, inter):
        kind, relation = kind.lower(), relation.lower()
        if kind not in ("general", "padding"):  # the frames of a dissolve / wipe / fade... between two shots
            pending = {"after_shot": shots[-1]["shot"] if shots else None, "before_shot": None, "type": kind,
                       **span(a, b)}
            transitions.append(pending)
            continue
        if kind == "padding":
            continue
        s = {"shot": len(shots) + 1, **span(a, b)}
        s["seconds"] = round(s["frames"] / fps, 3)
        if pending:
            s["begins"], s["transition"] = "after_transition", pending["type"]
            pending["before_shot"] = s["shot"]
            pending = None
        else:
            s["begins"] = "video_start" if not shots and a == 0 else relation
            s["transition"] = None
        shots.append(s)
    return shots, transitions


def dumps(data):
    """JSON with one line per shot / transition (a person reads it; json.load reads it the same)."""
    lines = []
    for k, v in data.items():
        if isinstance(v, list) and v and isinstance(v[0], dict):
            rows = ",\n".join("  " + json.dumps(x, ensure_ascii=False) for x in v)
            lines.append(f" {json.dumps(k)}: [\n{rows}\n ]")
        else:
            lines.append(f" {json.dumps(k)}: {json.dumps(v, ensure_ascii=False)}")
    return "{\n" + ",\n".join(lines) + "\n}\n"


def run(model, video, out, status, cached):
    """Detect the shots of `video` with the shot-boundary model `model` (engine job model dict) -> out (json).
    cached(key, build) keeps the loaded model between runs (and frees other models first)."""
    import torch
    folder = model["path"] if os.path.isdir(model["path"]) else os.path.dirname(model["path"])
    lib, reason = detector(folder) or (None, "not a shot boundary detection model (its card has no such tag)")
    if lib is None:
        raise ValueError(f"{model['name']}: {reason}")
    import omnishotcut
    ckpt = weight_file(model["path"])
    detect = cached(("shots", ckpt), lambda: (status("Loading shot detector…"), omnishotcut.load(ckpt))[1])
    if not os.path.isfile(video):
        raise ValueError(f"{video}: no such file")
    rate, width, height = stream_info(video)
    if not rate:
        raise ValueError(f"{video}: the video stream records no frame rate")
    fps = float(Fraction(rate))
    status("Finding shots…")
    t = time.time()
    # MIOpen's batch norm (the model's ResNet) crashes on ROCm here; the plain kernels work. Only for this call.
    cudnn = torch.backends.cudnn.enabled
    torch.backends.cudnn.enabled = False
    try:
        ranges, intra, inter = detect.inference(video, mode="default")
    finally:
        torch.backends.cudnn.enabled = cudnn
    took = time.time() - t
    frames = ranges[-1][1] if ranges else 0
    shots, transitions = describe(ranges, intra, inter, fps)
    try:
        from importlib.metadata import version
        lib_version = f"{lib} {version(lib)}"
    except Exception:  # noqa: BLE001
        lib_version = lib
    data = {
        "format": "shots",  # what this JSON is (a .json file like any other - its content says shots)
        "about": "Shots in the video. Frame numbers start at 1 and include first and last (as on the Edit Media "
                 "timeline); seconds come from the video stream's frame rate. 'begins' says how a shot starts; "
                 "'transition' names the transition just before it. Transition frames belong to no shot.",
        "video": os.path.abspath(video),
        "fps": rate, "fps_value": round(fps, 4), "frames": frames, "seconds": round(frames / fps, 3),
        "size": [width, height],
        "detector": {"model": model["name"], "weights": ckpt, "library": lib_version, "mode": "default",
                     "seconds": round(took, 1)},
        "shots": shots,
        "transitions": transitions,
    }
    with open(out, "w", encoding="utf-8") as f:
        f.write(dumps(data))
    return data
