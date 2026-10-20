"""Models that run on an online service (a Model node's "Runs on"). So far: Claude Code on the user's Max plan.

Claude Code is the `claude` program, run headless (`claude -p`, executor_session.py). It must use the account it is
signed in to (the Max plan), not an API key: ANTHROPIC_API_KEY is set for this Windows user, and Claude Code prefers
an API key when it sees one - so it is removed from the environment Claude Code starts in (child_env).
Its state (installed? version? signed in?) is checked in the background once and cached: a Model node asks on every
redraw, and `claude auth status` takes a second.
"""

import json
import os
import shutil
import time
import weakref

from PySide6.QtCore import QProcess, QProcessEnvironment

CLAUDE_CODE = "claude_code"
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# The Model field's choices: Claude Code's own default, then the models Anthropic's model list names (GET /v1/models -
# the account's models, newest first; free to ask). The list object is shared with the Model node's parameter, so
# filling it in place updates every Model field.
MODEL_OPTIONS = [("", "Claude Code's default")]
_models = {"state": "", "error": ""}   # "" not asked yet / "asking" / "done" / "failed"

_state = {}             # {"version": str, "logged_in": bool, "method": str, "error": str} once checked
_checking = []          # the running QProcess(es)
_waiting = weakref.WeakSet()  # nodes to redraw when the check is done


def claude_exe():
    return shutil.which("claude") or ""


def child_env():
    """This process's environment without API keys: Claude Code then uses the account it is signed in to."""
    env = QProcessEnvironment.systemEnvironment()
    for k in _API_KEY_VARS:
        env.remove(k)
    return env


def child_env_dict():
    return {k: v for k, v in os.environ.items() if k not in _API_KEY_VARS}


def api_key():
    """The Anthropic API key of this Windows user (the app's environment, else the user's stored variables) - used
    only to ask Anthropic's API which models there are, never given to Claude Code."""
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = winreg.QueryValueEx(k, "ANTHROPIC_API_KEY")[0]
        except OSError:
            key = ""
    return key


def _fetch_models():
    """Fill MODEL_OPTIONS from Anthropic's model list (in the background)."""
    from PySide6.QtCore import QUrl
    from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
    key = api_key()
    if not key:
        _models.update(state="failed", error="no ANTHROPIC_API_KEY to ask Anthropic for the model list")
        return
    _models["state"] = "asking"
    manager = QNetworkAccessManager()
    request = QNetworkRequest(QUrl("https://api.anthropic.com/v1/models?limit=100"))
    request.setRawHeader(b"x-api-key", key.encode("ascii"))
    request.setRawHeader(b"anthropic-version", b"2023-06-01")
    reply = manager.get(request)

    def done():
        try:
            if reply.error() != QNetworkReply.NoError:
                raise ValueError(reply.errorString())
            data = json.loads(bytes(reply.readAll()).decode("utf-8")).get("data", [])
            del MODEL_OPTIONS[1:]
            MODEL_OPTIONS.extend((m["id"], m.get("display_name") or m["id"]) for m in data if m.get("id"))
            _models.update(state="done", error="")
        except ValueError as e:
            _models.update(state="failed", error=f"couldn't get the model list: {e}")
        reply.deleteLater()
        _checking_net.clear()
        for n in list(_waiting):
            n.notify()

    reply.finished.connect(done)
    _checking_net[:] = [manager, reply]  # kept alive until the answer comes


_checking_net = []


def status(node=None, refresh=False):
    """Claude Code's state, or {} while it is being checked (the node is redrawn when the answer comes)."""
    if node is not None:
        _waiting.add(node)
    if not _models["state"]:
        _fetch_models()
    if refresh or (_state and not _state.get("logged_in") and time.time() - _state.get("at", 0) > 15):
        _state.clear()  # a "not signed in" answer is asked again after a while: the user may have signed in since
    if not _state and not _checking:
        _check()
    return dict(_state)


def _check():
    exe = claude_exe()
    if not exe:
        _state.update(error="Claude Code isn't installed (no `claude` on PATH)")
        return
    result = {}

    def run(args, done):
        p = QProcess()
        p.setProcessEnvironment(child_env())

        def finished(*_):
            try:
                out = bytes(p.readAllStandardOutput()).decode("utf-8", "replace")
            except RuntimeError:  # the app is closing: the process object is already gone
                return
            done(out)
        p.finished.connect(finished)
        _checking.append(p)
        p.start(exe, args)

    def got_version(out):
        result["version"] = out.strip().split(" (")[0]
        run(["auth", "status"], got_auth)

    def got_auth(out):
        try:
            info = json.loads(out[out.index("{"):])
        except ValueError:
            info = {}
        result["logged_in"] = bool(info.get("loggedIn"))
        result["method"] = str(info.get("authMethod") or "")
        result["subscription"] = str(info.get("subscriptionType") or "")
        result["at"] = time.time()
        _state.update(result)
        _checking.clear()
        for n in list(_waiting):
            n.notify()

    run(["--version"], got_version)


# ---- online models: one settings file each (set globally: Library › + Model › Online…) ----------------------------
# <project>/online_models/<name>.json = {"provider", "model", "label", "settings": {...}}. A Model node running Online
# picks a provider, then one of that provider's files; its settings are read from the file each time it is used.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONLINE_DIR = os.path.join(PROJECT_ROOT, "online_models")
CACHE_DIR = os.path.join(ONLINE_DIR, ".cache")  # what each service model takes and costs, as the service said it
SETTINGS_HELP = {CLAUDE_CODE: {"instructions": "Executor mode: the brain's instructions file (default: prompts/executor.txt)",
                               "system": "normal mode: its system prompt (default: You are a helpful assistant.)"}}


def providers():
    """{service name: label}: Claude Code and the services set up in services/ (services.py)."""
    from . import services
    return services.labels()


def protocol_of(provider):
    from . import services
    e = services.entry(provider)
    return e["protocol"] if e else ""
README = """Online models - one .json file each (made by the Library's + Model > Online...; edit freely).

{
  "provider": "claude_code",          the service: claude_code = Claude Code on your Claude plan
  "model": "claude-sonnet-5-5",       the service's model id ("" = the service's own default)
  "label": "Claude Sonnet 5.5",       how the app lists it
  "settings": {                       read each time the model is used:
    "instructions": "F:/.../my.txt",  claude_code, Executor mode: the brain's instructions (default prompts/executor.txt)
    "system": "You are ..."           claude_code, normal mode: its system prompt (default: You are a helpful assistant.)
  }
}
"""
_entries = {"stamp": None, "list": []}


def entries():
    """Every online model file, as {"name", "path", "provider", "model", "label", "settings", "error"} (read again
    when the folder changes)."""
    import glob
    import time
    now = time.monotonic()
    if _entries["stamp"] is not None and now - _entries.get("checked", 0) < 1.0:
        return list(_entries["list"])  # looked at the folder less than a second ago (nodes ask while painting)
    _entries["checked"] = now
    files = sorted(glob.glob(os.path.join(ONLINE_DIR, "*.json")))
    stamp = tuple((f, os.path.getmtime(f)) for f in files)
    if stamp != _entries["stamp"]:
        out = []
        for f in files:
            e = {"name": os.path.splitext(os.path.basename(f))[0], "path": f, "provider": "", "model": "",
                 "label": "", "settings": {}, "error": ""}
            try:
                with open(f, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
                e.update(provider=str(data.get("provider", "")), model=str(data.get("model", "")),
                         label=str(data.get("label") or data.get("model") or e["name"]),
                         settings=data.get("settings") if isinstance(data.get("settings"), dict) else {})
                if e["provider"] not in providers():
                    e["error"] = (f"service {e['provider']!r} isn't set up - Library › + Model › Service… "
                                  f"(set up: {', '.join(providers())})")
            except (OSError, ValueError) as err:
                e.update(label=e["name"], error=f"can't read it: {err}")
            out.append(e)
        _entries.update(stamp=stamp, list=out)
    else:  # a service set up / removed since: the files' errors follow
        known = providers()
        for e in _entries["list"]:
            if e["error"].startswith("service ") and e["provider"] in known:
                e["error"] = ""
            elif not e["error"] and e["provider"] not in known:
                e["error"] = f"service {e['provider']!r} isn't set up - Library › + Model › Service…"
    return list(_entries["list"])


def entry(name):
    return next((e for e in entries() if e["name"] == name), None)


def entry_for_file(path):
    return next((e for e in entries() if os.path.normcase(e["path"]) == os.path.normcase(os.path.abspath(path))), None)


def add_entry(provider, model, label, settings=None):
    """Write a new online model file; returns its name (made from the label, never overwriting another)."""
    import re
    os.makedirs(ONLINE_DIR, exist_ok=True)
    readme = os.path.join(ONLINE_DIR, "README.txt")
    if not os.path.exists(readme):
        with open(readme, "w", encoding="utf-8") as f:
            f.write(README)
    base = re.sub(r"[^\w.-]+", "_", (label or model or provider).strip().lower()).strip("_") or "online_model"
    name, i = base, 2
    while os.path.exists(os.path.join(ONLINE_DIR, name + ".json")):
        name, i = f"{base}_{i}", i + 1
    with open(os.path.join(ONLINE_DIR, name + ".json"), "w", encoding="utf-8") as f:
        json.dump({"provider": provider, "model": model, "label": label or model or name,
                   "settings": settings or {}}, f, indent=2, ensure_ascii=False)
    forget_cache()
    return name


def forget_cache():
    """The folder changed here: the next entries() reads it again at once."""
    _entries["checked"] = 0


def provider_options(node=None):
    """The Provider field: the services that have online model files (all set-up services while there are none)."""
    known = providers()
    used = [p for p in known if any(e["provider"] == p for e in entries())]
    return [(p, known[p]) for p in (used or known)]


# ---- what a service's model takes and costs (asked once, cached beside its file) -------------------------------
def info(e):
    """The cached facts about an online model of a service ({fields, output, price, description, url, version,
    official, fetched}) or None when not asked yet (refresh_info asks)."""
    if e is None:
        return None
    path = os.path.join(CACHE_DIR, e["name"] + ".json")
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    hit = _info_cache.get(path)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if data.get("model") != e["model"]:  # the file now names another model: ask again
        return None
    _info_cache[path] = (stamp, data)
    return data


_info_cache = {}
_asking = set()


def refresh_info(e, done=None):
    """Ask the service what the model takes (its schema) and costs (its price table); cached. done(info, error)."""
    from . import services
    s = services.entry(e["provider"])
    mod = services.protocol_module(s["protocol"]) if s else None
    if mod is None or not hasattr(mod, "model"):
        if done:
            done(None, ValueError("this service has no model details to ask for"))
        return
    if e["name"] in _asking:
        return
    _asking.add(e["name"])
    token = services.key(s)

    def ask():
        details = mod.model(token, e["model"])
        try:
            details["price"] = mod.price(e["model"], s)
        except Exception as err:  # the price page may be unreachable: the model still works
            details["price"], details["price_error"] = None, str(err)
        details.update(model=e["model"], fetched=time.strftime("%Y-%m-%d %H:%M"))
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, e["name"] + ".json"), "w", encoding="utf-8") as f:
            json.dump(details, f, indent=1, ensure_ascii=False)
        return details

    def finished(details, err):
        _asking.discard(e["name"])
        for cb in ([done] if done else []) + _info_listeners:
            cb(details, err)
    services.in_background(ask, done=finished)


_info_listeners = []  # callables(info, error): redraw Model nodes when a model's details arrive


def on_info(cb):
    _info_listeners.append(cb)


def text_field(fields):
    """The input a Modality's prompt goes to: "prompt", else the first required plain text input."""
    names = [f["name"] for f in fields if f["type"] == "string" and not f["options"]]
    if "prompt" in names:
        return "prompt"
    return next((f["name"] for f in fields if f["name"] in names and f["required"]), None)


def fed_fields(fields):
    """Inputs the Modality fills (its prompt and its data): not shown as the Model node's own settings."""
    t = text_field(fields)
    return {f["name"] for f in fields if f["type"] == "uri" or f["name"] == t}


def model_options(node=None):
    """The Model field: the online model files of the node's provider (all of them for a type listing)."""
    provider = node.params.get("provider") if node is not None else None
    return [(e["name"], e["label"]) for e in entries() if provider in (None, e["provider"]) and not e["error"]]


def resolve(node):
    """(provider, model id, settings, file entry or None) for a Model node running online. Its Model value names
    an online model file; a value that names none is read as a model id (graphs saved before the files existed)."""
    provider = node.params.get("provider") or CLAUDE_CODE
    value = node.params.get("online_model") or ""
    e = entry(value)
    if e is not None:
        return e["provider"], e["model"], e["settings"], e
    return provider, value, {}, None


def problem(kind, node=None):
    if node is not None and node.params.get("online_model"):
        e = entry(node.params["online_model"])
        if e is not None and e["error"]:
            return f"{os.path.basename(e['path'])}: {e['error']}"
    if kind not in providers():
        return f"service {kind} isn't set up - Library › + Model › Service…"
    if node is not None and not node.params.get("online_model") and not model_options(node):
        return "No online model yet - add one: Library › + Model › Online…"
    if protocol_of(kind) != CLAUDE_CODE:
        from . import services
        if node is not None:
            _waiting.add(node)
        st = services.status(kind)
        if st.get("state") == "failed":
            return st.get("error") or "the service can't be reached"
        e = entry(node.params.get("online_model")) if node is not None else None
        if e is not None and info(e) is None:
            refresh_info(e)  # its inputs and price: the node is redrawn when they arrive
        return ""
    st = status(node)
    if st.get("error"):
        return st["error"]
    if st and not st.get("logged_in"):
        return "Claude Code isn't signed in to your Claude account - in a terminal: claude auth login"
    return ""


def service_rows(kind, node=None):
    """A service model's facts: the service and account, what it takes (from its schema) and costs (its price
    table), and this node's estimate."""
    from . import costs, services
    s = services.entry(kind)
    st = services.status(kind)
    rows = [("Service", (s["label"] if s else kind) + (f" · {st['account']}" if st.get("account") else "")
             + ("" if st.get("state") != "checking" else " · checking the key…"))]
    if st.get("state") == "failed":
        rows.append(("Status", st.get("error", "")))
    e = entry(node.params.get("online_model")) if node is not None else None
    if e is None:
        return rows
    rows += [("Settings file", e["path"]), ("Model id", e["model"])]
    d = info(e)
    if d is None:
        return rows + [("Details", "asking the service what it takes and costs…")]
    if d.get("description"):
        rows.append(("About", d["description"]))
    rows.append(("Page", d.get("url", "")))
    fields = d.get("fields") or []
    t = text_field(fields)
    if t:
        rows.append(("Prompt goes to", t))
    media = [f for f in fields if f["type"] == "uri"]
    for i, f in enumerate(media):
        rows.append(("Data goes to" if i == 0 else "", f"{f['name']} ({f['media']}{', several' if f['multi'] else ''})"
                     + (" · required" if f["required"] else "")))
    price = d.get("price")
    if price:
        for i, (cond, money) in enumerate(costs.table_rows(price)):
            rows.append(("Price" if i == 0 else "", f"{money} - {cond}"))
        if hasattr(node, "service_values"):
            est = costs.estimate(price, node.service_values())
            if est:
                rows.append(("This run", est["text"]))
    else:
        rows.append(("Price", "the service shows none" + (f" ({d['price_error']})" if d.get("price_error") else "")))
    spent = costs.spent(service=kind)
    if spent:
        rows.append(("Spent so far", f"${spent:.2f} on {s['label'] if s else kind} (logs/online_spend.jsonl)"))
    rows.append(("Asked", d.get("fetched", "")))
    return rows


def info_rows(kind, node=None):
    if protocol_of(kind) not in ("", CLAUDE_CODE):
        return service_rows(kind, node)
    if kind != CLAUDE_CODE:
        return []
    st = status(node)
    rows = []
    if node is not None:
        _provider, model, settings, e = resolve(node)
        if e is not None:
            rows.append(("Settings file", e["path"]))
        rows.append(("Model id", model or "Claude Code's own default"))
        for key, value in settings.items():
            rows.append((key, str(value)))
    rows.append(("Program", claude_exe() or "not found"))
    if not st:
        return rows + [("Status", "checking…")]
    if st.get("error"):
        return rows + [("Status", st["error"])]
    rows.append(("Version", st.get("version", "")))
    signed = "yes" if st.get("logged_in") else "no - run `claude auth login` in a terminal"
    rows.append(("Signed in", signed + (f" ({st['method']})" if st.get("method") and st["method"] != "none" else "")))
    if st.get("subscription"):
        rows.append(("Plan", st["subscription"]))
    rows.append(("Billing", "your Claude account (API keys are kept out of its environment)"))
    listed = len(MODEL_OPTIONS) - 1
    rows.append(("Models", f"{listed} - Anthropic's model list for your account" if _models["state"] == "done"
                 else _models["error"] or "asking Anthropic for the list…"))
    rows.append(("Runs in", "a Modality: normal mode answers the prompt; Executor mode builds as its brain"))
    return rows


def _redraw(*_):
    """A service's check or a model's details came back: the nodes showing them follow (a Model node also rebuilds
    its settings from the model's inputs)."""
    for n in list(_waiting):
        if hasattr(n, "online_info_changed"):
            n.online_info_changed()
        n.notify()


def _listen():
    from . import services
    services.on_status(_redraw)
    on_info(_redraw)


_listen()
