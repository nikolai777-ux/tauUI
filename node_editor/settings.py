"""App-wide settings (stored per user by Qt: Windows registry, HKCU\\Software\\tauUI)."""

import os

from PySide6.QtCore import QSettings

# none until the user picks one: File > Preferences > Default Python env...
INITIAL_PYTHON_ENV = ""


def _store():
    return QSettings("tauUI", "tauUI")


_default_env = {"value": None, "at": 0.0}


def default_python_env():
    """The Python environment models run in when their Model node doesn't name one. Kept a second (every Model
    node asks while a graph is checked - tens of thousands of registry reads opening a big graph)."""
    import time
    if os.environ.get("TAUUI_PYTHON_ENV"):  # this app instance only (e.g. its own GPU slice on a pod)
        return os.environ["TAUUI_PYTHON_ENV"]
    now = time.monotonic()
    if _default_env["value"] is None or now - _default_env["at"] > 1.0:
        _default_env["value"] = _store().value("default_python_env", INITIAL_PYTHON_ENV)
        _default_env["at"] = now
    return _default_env["value"]


def set_default_python_env(path):
    _store().setValue("default_python_env", path)
    _default_env["value"] = None


INITIAL_SDCPP = ""  # File > stable-diffusion.cpp (sd-cli.exe)... (only needed for GGUF models)


def sdcpp_exe():
    """stable-diffusion.cpp's sd-cli.exe - runs GGUF models (e.g. MiniMax H3)."""
    return _store().value("sdcpp_exe", INITIAL_SDCPP)


def set_sdcpp_exe(path):
    _store().setValue("sdcpp_exe", path)


INITIAL_HF_STORE = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")


def library_order():
    """The Library's arrangement: [{"repo": id} | {"label": text}] - purely visual."""
    import json
    try:
        data = json.loads(_store().value("library_order", "[]"))
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def set_library_order(entries):
    import json
    _store().setValue("library_order", json.dumps(entries))


def local_models():
    """The user's own models (folders or files outside the store) listed in the Library, by path."""
    import json
    try:
        data = json.loads(_store().value("local_models", "[]"))
    except ValueError:
        return []
    return [p for p in data if isinstance(p, str)] if isinstance(data, list) else []


def set_local_models(paths):
    import json
    _store().setValue("local_models", json.dumps(list(dict.fromkeys(paths))))


def apply_import(path):
    """Settings handed over in a file (e.g. prepared by a tool that can't reach this user's registry): applied once
    at start-up, then the file is renamed *.applied.json. Keys:
      default_python_env: a venv folder;
      tools_folder: the external tools folder (the Executor's brain is told about it);
      local_models: paths to add to the Library;
      library_add: [{"label": name, "items": [{"local": path} | {"repo": id}]}] - into the user's arrangement: after
                   the label's own block when that label exists, else as a new label at the end.
    Returns what it did (lines), or []."""
    import json
    import os
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [f"couldn't read {path}: {e}"]
    done = []
    if data.get("tools_folder"):
        set_tools_folder(data["tools_folder"])
        done.append(f"external tools folder: {data['tools_folder']}")
    if data.get("default_python_env"):
        set_default_python_env(data["default_python_env"])
        done.append(f"default Python env: {data['default_python_env']}")
    if data.get("local_models"):
        before = local_models()
        set_local_models(before + [p for p in data["local_models"] if p not in before])
        done.append(f"{len(local_models()) - len(before)} models of your own added")
    order = library_order()
    key = lambda e: ("label", e["label"]) if "label" in e else ("local", os.path.normcase(e["local"])) \
        if "local" in e else ("repo", e.get("repo"))
    present = {key(e) for e in order}
    for group in data.get("library_add", []):
        items = [e for e in group.get("items", []) if key(e) not in present]
        if not items:
            continue
        labels = [i for i, e in enumerate(order) if e.get("label") == group["label"]]
        if labels:
            at = labels[0] + 1
            while at < len(order) and "label" not in order[at]:
                at += 1
            order[at:at] = items
        else:
            order += [{"label": group["label"]}] + items
        present |= {key(e) for e in items}
        done.append(f"{group['label']}: {len(items)} placed")
    set_library_order(order)
    os.replace(path, os.path.splitext(path)[0] + ".applied.json")
    return done


def all_models():
    """Every model the Library lists: the store's repos, then the user's own (model_store.Repo each)."""
    from . import model_store
    return model_store.scan(hf_store()) + [model_store.read_local(p) for p in local_models()]


def hf_store():
    """Where models downloaded from Hugging Face live (used as HF_HOME: the cache is in <store>\\hub)."""
    return _store().value("hf_store", INITIAL_HF_STORE)


def ui_scale():
    """Whole-UI scale (1.0 = Windows' own scaling), applied at start-up as Qt's QT_SCALE_FACTOR."""
    try:
        return float(_store().value("ui_scale", 1.0))
    except (TypeError, ValueError):
        return 1.0


def set_ui_scale(value):
    _store().setValue("ui_scale", float(value))


def blender_exe():
    """Blender (converts Alembic for the 3D viewer): the one chosen in File › Preferences › Blender…, else the newest
    found under Program Files\\Blender Foundation; "" when there is none."""
    import glob
    chosen = _store().value("blender_exe", "")
    if chosen and os.path.isfile(chosen):
        return chosen
    found = glob.glob(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Blender Foundation", "*",
                                   "blender.exe"))

    def version(p):
        import re
        return [int(x) for x in re.findall(r"\d+", os.path.basename(os.path.dirname(p)))]
    return max(found, key=version) if found else ""


def set_blender_exe(path):
    _store().setValue("blender_exe", path)


def tools_folder():
    """A folder of command-line tools outside the app (its README describes them) - File › Preferences › External
    tools folder…; told to the Executor's brain. "" = none."""
    path = _store().value("tools_folder", "") or ""
    return path if path and os.path.isdir(path) else ""


def set_tools_folder(path):
    _store().setValue("tools_folder", path or "")


# how the app draws (the 3D viewer, composed windows) - Qt's RHI backends; applied at start-up (main.py). Direct3D 12
# is left out: the 3D viewer drew nothing with it here (2026-10-01, Qt 6.11.2, R9700); Vulkan and OpenGL drew the same
# picture as Direct3D 11
GRAPHICS_APIS = (("d3d11", "Direct3D 11"), ("vulkan", "Vulkan"), ("opengl", "OpenGL"))
DEFAULT_GRAPHICS_API = "opengl"  # the user's choice (2026-10-01); all three worked here


def graphics_api():
    value = _store().value("graphics_api", DEFAULT_GRAPHICS_API)
    return value if value in dict(GRAPHICS_APIS) else DEFAULT_GRAPHICS_API


def set_graphics_api(value):
    _store().setValue("graphics_api", value)


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTOSAVE_DEFAULTS = {"on": True, "folder": os.path.join(PROJECT_DIR, "autosave"), "minutes": 5, "count": 3}


def autosave():
    """File > Preferences > Autosave: on, folder, every how many minutes, how many copies (the oldest is replaced)."""
    import json
    try:
        data = json.loads(_store().value("autosave", "{}"))
    except ValueError:
        data = {}
    return {**AUTOSAVE_DEFAULTS, **(data if isinstance(data, dict) else {})}


def set_autosave(values):
    import json
    _store().setValue("autosave", json.dumps({**autosave(), **values}))


def pane_box(key):
    """A text box in the parameter pane (e.g. "executor/log"): {"h": height in px, "folded": bool} - or {}."""
    import json
    try:
        data = json.loads(_store().value("pane_boxes", "{}"))
    except ValueError:
        data = {}
    return data.get(key, {}) if isinstance(data, dict) else {}


def set_pane_box(key, values):
    import json
    try:
        data = json.loads(_store().value("pane_boxes", "{}"))
    except ValueError:
        data = {}
    data = data if isinstance(data, dict) else {}
    box = {**data.get(key, {}), **values}
    data[key] = {k: v for k, v in box.items() if v not in (None, False)}
    _store().setValue("pane_boxes", json.dumps(data))


def tick_sound():
    """Rulers click as their ticks pass the marker (File > Preferences > UI)."""
    return str(_store().value("tick_sound", "true")).lower() == "true"


def set_tick_sound(on):
    _store().setValue("tick_sound", "true" if on else "false")
