"""Online services the app can use - each one a small settings file in <project>/services/<name>.json:

    {"protocol": "replicate", "label": "Replicate", "key_env": "REPLICATE_API_TOKEN", "settings": {}}

The protocol is code (how to talk to the service - svc_replicate.py, svc_google.py, Claude Code in online.py); the
file only says which one and in which Windows user variable the key lives. The key itself is never written into a
service file, a model file or a graph. Adding a service = writing its file (Library › + Model › Service…), removing
it = deleting the file (its online models stay as files, shown greyed until the service is back).

Claude Code needs no file: it is always there (it runs on the account `claude` is signed in to).

Calls to a service run in a background thread (in_background) and come back on the Qt thread, so the app never waits.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from PySide6.QtCore import QObject, Signal

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES_DIR = os.path.join(PROJECT_ROOT, "services")

CLAUDE_CODE = "claude_code"
# protocol: (label, Windows user variable holding the key, what the service gives)
PROTOCOLS = {
    CLAUDE_CODE: ("Claude Code", "", "Claude as a brain or an answering model, on your Claude plan"),
    "replicate": ("Replicate", "REPLICATE_API_TOKEN", "thousands of models (video, image, 3D, speech...), paid per run"),
    "google": ("Google AI Studio", "GEMINI_API_KEY", "Gemini, Nano Banana (images), Veo (video), TTS, music"),
}
README = """Online services - one .json file each (made by the Library's + Model > Service...; edit freely).

{
  "protocol": "replicate",              how the app talks to it: replicate | google (claude_code needs no file)
  "label": "Replicate",                 how the app lists it
  "key_env": "REPLICATE_API_TOKEN",     the Windows user variable that holds your key (setx NAME value)
  "settings": {"confirm_over": 1.0}     ask before a run that may cost more than this many dollars
}

The key is never stored here. Delete the file to remove the service; its online models stay.
"""

_entries = {"stamp": None, "list": []}
_status = {}    # service name -> {"state": "checking" | "ok" | "failed", "account", "error", "at"}


def _builtin():
    return {"name": CLAUDE_CODE, "path": "", "protocol": CLAUDE_CODE, "label": PROTOCOLS[CLAUDE_CODE][0],
            "key_env": "", "settings": {}, "error": ""}


def entries():
    """Every service: Claude Code, then the files in services/ (read again when the folder changes)."""
    import glob
    import time
    now = time.monotonic()
    if _entries["stamp"] is not None and now - _entries.get("checked", 0) < 1.0:
        return list(_entries["list"])  # looked at the folder less than a second ago (nodes ask while painting)
    _entries["checked"] = now
    files = sorted(glob.glob(os.path.join(SERVICES_DIR, "*.json")))
    stamp = tuple((f, os.path.getmtime(f)) for f in files)
    if stamp != _entries["stamp"]:
        out = [_builtin()]
        for f in files:
            e = {"name": os.path.splitext(os.path.basename(f))[0], "path": f, "protocol": "", "label": "",
                 "key_env": "", "settings": {}, "error": ""}
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                protocol = str(data.get("protocol", ""))
                known = PROTOCOLS.get(protocol, ("", "", ""))
                e.update(protocol=protocol, label=str(data.get("label") or known[0] or e["name"]),
                         key_env=str(data.get("key_env") or known[1]),
                         settings=data.get("settings") if isinstance(data.get("settings"), dict) else {})
                if protocol not in PROTOCOLS:
                    e["error"] = f"protocol {protocol!r} - the app speaks: {', '.join(PROTOCOLS)}"
            except (OSError, ValueError) as err:
                e.update(label=e["name"], error=f"can't read it: {err}")
            if e["name"] == CLAUDE_CODE:
                continue  # the built-in one
            out.append(e)
        _entries.update(stamp=stamp, list=out)
    return list(_entries["list"])


def entry(name):
    return next((e for e in entries() if e["name"] == name), None)


def labels():
    """{service name: label} - what a Model node's Provider field lists."""
    return {e["name"]: e["label"] for e in entries()}


def add(protocol, label=None, key_env=None):
    """Write a service file; returns its name (the protocol's name, else name_2...)."""
    os.makedirs(SERVICES_DIR, exist_ok=True)
    readme = os.path.join(SERVICES_DIR, "README.txt")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write(README)
    name, i = protocol, 2
    while os.path.exists(os.path.join(SERVICES_DIR, name + ".json")):
        name, i = f"{protocol}_{i}", i + 1
    with open(os.path.join(SERVICES_DIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump({"protocol": protocol, "label": label or PROTOCOLS[protocol][0],
                   "key_env": key_env or PROTOCOLS[protocol][1], "settings": {"confirm_over": 1.0}},
                  f, indent=2, ensure_ascii=False)
    _status.pop(name, None)
    _entries["checked"] = 0  # read the folder again at once
    return name


def remove(name):
    e = entry(name)
    if e is not None and e["path"]:
        os.remove(e["path"])
        _entries["checked"] = 0
    _status.pop(name, None)


def key(e):
    """The service's key: the app's environment, else the user's stored variables (set with setx after the app
    started). Empty if there is none."""
    var = e.get("key_env") or ""
    if not var:
        return ""
    value = os.environ.get(var, "")
    if not value and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                value = winreg.QueryValueEx(k, var)[0]
        except OSError:
            value = ""
    return value.strip()


def protocol_module(protocol):
    if protocol == "replicate":
        from . import svc_replicate
        return svc_replicate
    if protocol == "google":
        from . import svc_google
        return svc_google
    return None


def status(name, refresh=False, done=None):
    """The service's state (asked in the background once, then cached): {"state", "account", "error"}. done(st) is
    called when a check finishes."""
    e = entry(name)
    if e is None:
        return {"state": "failed", "error": f"no service {name!r} (Library › + Model › Service…)"}
    if e["protocol"] == CLAUDE_CODE:
        return {"state": "ok"}
    st = _status.get(name)
    if st and not refresh and not (st["state"] == "failed" and time.time() - st.get("at", 0) > 30):
        return dict(st)
    if e["error"]:
        _status[name] = {"state": "failed", "error": e["error"], "at": time.time()}
        return dict(_status[name])
    k = key(e)
    if not k:
        _status[name] = {"state": "failed", "at": time.time(),
                         "error": f"no key: set the Windows user variable {e['key_env']} (setx {e['key_env']} …), "
                                  "then refresh"}
        return dict(_status[name])
    if st and st["state"] == "checking":
        return dict(st)
    _status[name] = {"state": "checking", "at": time.time()}
    mod = protocol_module(e["protocol"])

    def finished(account, err):
        _status[name] = ({"state": "failed", "error": str(err), "at": time.time()} if err else
                         {"state": "ok", "account": account or "", "at": time.time()})
        if done is not None:
            done(dict(_status[name]))
        for cb in _listeners:
            cb(name, dict(_status[name]))
    in_background(mod.account, k, done=finished)
    return dict(_status[name])


_listeners = []  # callables(name, status): something to redraw when a service's check finishes


def on_status(cb):
    _listeners.append(cb)


# ---- background calls -----------------------------------------------------------------------------------------
class _Bridge(QObject):
    done = Signal(object, object, object)  # callback, result, error - delivered on the Qt thread

    def __init__(self):
        super().__init__()
        self.done.connect(lambda cb, result, err: cb is not None and cb(result, err))


_bridge = []


def _the_bridge():
    if not _bridge:
        bridge = _Bridge()
        # it delivers on the Qt thread even when the first call came from a worker thread (it would belong there)
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance()
        if app is not None and bridge.thread() is not app.thread():
            bridge.moveToThread(app.thread())
        _bridge.append(bridge)
    return _bridge[0]


def in_background(fn, *args, done=None):
    """fn(*args) in a thread; done(result, error) afterwards on the Qt thread."""
    bridge = _the_bridge()

    def work():
        try:
            result, err = fn(*args), None
        except Exception as e:  # reported to the caller, never raised in the thread
            result, err = None, e
        bridge.done.emit(done, result, err)
    threading.Thread(target=work, daemon=True).start()


def to_qt(fn, *args):
    """Run fn(*args) on the Qt thread (from a worker thread)."""
    _the_bridge().done.emit(lambda _r, _e: fn(*args), None, None)


# ---- HTTP (stdlib only: the app's venv has no requests) ---------------------------------------------------------
class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message


UA = "tauUI/1.0 (+desktop app)"


def http(method, url, headers=None, body=None, timeout=60, raw=False):
    """A request; returns parsed JSON (or bytes with raw=True). Errors become HttpError with the service's own
    message when it gives one."""
    data = body
    hdrs = {"User-Agent": UA, **(headers or {})}
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            content = r.read()
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(text)
            msg = j.get("detail") or j.get("error") or j.get("title") or text
            if isinstance(msg, dict):
                # which limit (Google: QuotaFailure ids + values) - a daily request quota and a spend cap read alike
                which = [f"{v.get('quotaId') or v.get('quotaMetric')}={v.get('quotaValue', '?')}"
                         for d in msg.get("details") or [] for v in d.get("violations") or []
                         if v.get("quotaId") or v.get("quotaMetric")]
                msg = (msg.get("message") or json.dumps(msg)) + (f" [{'; '.join(which)}]" if which else "")
        except ValueError:
            msg = text[:300]
        raise HttpError(e.code, str(msg)) from None
    except urllib.error.URLError as e:
        raise HttpError(0, f"can't reach {urllib.parse.urlsplit(url).netloc}: {e.reason}") from None
    if raw:
        return content
    return json.loads(content.decode("utf-8")) if content else {}


MAGIC = ((b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"), (b"glTF", ".glb"), (b"RIFF", None),
         (b"ID3", ".mp3"), (b"fLaC", ".flac"), (b"OggS", ".ogg"), (b"\x1aE\xdf\xa3", ".webm"), (b"PK", ".zip"),
         (b"{", ".json"), (b"ply", ".ply"))


def sniff(head):
    """A file's extension from its first bytes (when neither its URL nor the server named the type)."""
    if head[4:8] == b"ftyp":
        return ".mp4"
    if head[:4] == b"RIFF":
        return {b"WAVE": ".wav", b"WEBP": ".webp", b"AVI ": ".avi"}.get(head[8:12], ".bin")
    return next((ext for magic, ext in MAGIC if ext and head.startswith(magic)), ".bin")


def download(url, path, headers=None, timeout=600):
    """Save a URL to a file (streamed). A path ending in .bin (the URL named no type) gets the extension the server's
    Content-Type or the file's own first bytes say. Returns the path written."""
    import mimetypes
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    tmp = path + ".part"
    with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
        ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
        head = b""
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            if not head:
                head = chunk[:16]
            f.write(chunk)
    if path.endswith(".bin"):
        ext = (mimetypes.guess_extension(ctype) if ctype not in ("", "application/octet-stream") else None) or sniff(head)
        path = path[:-4] + {".jpe": ".jpg"}.get(ext, ext)
    os.replace(tmp, path)
    return path


def multipart(fields, files):
    """(body, content type) for a multipart/form-data upload: fields {name: text}, files {name: path}."""
    import mimetypes
    import uuid
    boundary = "----tauUI" + uuid.uuid4().hex
    out = []
    for name, value in fields.items():
        out += [f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"), b"\r\n"]
    for name, path in files.items():
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        fname = os.path.basename(path)
        with open(path, "rb") as f:
            content = f.read()
        out += [f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{fname}"\r\n'.encode(),
                f"Content-Type: {mime}\r\n\r\n".encode(), content, b"\r\n"]
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"


def safe_name(text):
    return re.sub(r"[^\w.-]+", "_", text.strip().lower()).strip("_") or "item"
