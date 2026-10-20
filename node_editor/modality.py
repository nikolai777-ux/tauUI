"""Modality node: takes ONE model (or a Merge of models) plus inputs, and renders.

No knowledge about particular models lives here: everything connected is handed to the engine,
which reports what is missing when it tries to run. Only facts are shown - what is connected, the
task a model card declares, and what the last render actually produced.

With the Executor tick, the model is a brain (Claude Code) that builds, tests and runs a graph instead
(executor.py: ExecutorMode) - typed to in the node's own Prompt box.
"""

import os

from PySide6.QtGui import QColor

from . import node_shapes, spellcheck
from .engine.client import existing_engine, get_engine, on_new_engine
from .executor import BUSY, ExecutorMode
from .node import ANY, AUDIO, DATA, GEOMETRY, IMAGE, JSON, MODEL, TEXT, VIDEO, Node, Param
from .registry import register_node
from .settings import sdcpp_exe

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUTPUT = os.path.join(PROJECT_ROOT, "output")

IN_MODEL, IN_TEXT, IN_DATA = "model", "text", "data"
OUTPUT_TYPES = {"image": IMAGE, "text": TEXT, "audio": AUDIO, "video": VIDEO, "data": JSON, "geometry": GEOMETRY}
EXT_KINDS = {".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image",
             ".wav": "audio", ".flac": "audio", ".mp3": "audio", ".ogg": "audio",
             ".mp4": "video", ".webm": "video", ".mov": "video", ".txt": "text", ".json": "data", ".shots": "data", ".chunks": "data",
             **{e: "geometry" for e in (".glb", ".gltf", ".obj", ".fbx", ".ply", ".stl", ".usdz", ".dae")}}


def _exec(n):
    return n.executor_on


def _normal(n):
    return not n.executor_on


def _online_brain(n):
    """Web search is the online service's own (Claude Code's); a brain running here has none yet."""
    brain = n.brain()
    return n.executor_on and brain is not None and brain.online and brain.brain_kind() == "claude_code"


# Executor mode: the brain's folds drawn dark red instead of the light grey cut-out lines
EXEC_GROOVE = QColor(120, 18, 18)


@register_node
class ModalityNode(ExecutorMode, Node):
    type_name = "modality"
    label = "Modality"
    color = QColor(55, 75, 95)
    shape_name = "brain"
    WIDTH = 115   # a bit wider and flatter than the brain's own proportions
    HEIGHT = 62
    OUTPUT_X = (node_shapes.lowest_x("brain"),)  # the result comes out of the brain stem
    show_socket_labels = "hover"
    SOCKET_RADIUS = 5  # five inputs are close together: names show on hover
    info_title = "Status"
    not_copied = ("built", "session_id", "log", "online_job", "sessions_before")  # a copy starts its own conversation / job
    # one wire per input: the model (or a Merge of models), the prompt (or a Merge of prompts), and the data - every
    # picture, video, sound, JSON or other file it works on: one item, or a Data node's list (its order = Picture 1,
    # 2...), each keeping its node's name
    inputs = [(IN_MODEL, MODEL), (IN_TEXT, TEXT), (IN_DATA, DATA)]
    outputs = [("out", ANY)]
    parameters = [
        # Executor mode = normal mode + building: only for a model that can call the app's tools (a language model)
        Param("executor", "Executor", "bool", False, row="flags", enabled=lambda n: n.executor_on or n.brain() is not None,
              hint="The model builds, tests and runs a graph (language models)"),
        # the brain may also search the web and read pages (to learn - it still acts only through the app's tools)
        Param("web", "Web search", "bool", True, row="flags", enabled=_online_brain,
              hint="The brain may read web pages (online brains)"),
        # the brain may run PowerShell commands on this PC (e.g. Blender in the background)
        Param("shell", "PowerShell", "bool", True, row="flags", enabled=_online_brain,
              hint="The brain may run commands on this PC"),
        # off: what already has its result is kept (Render picks up where it stopped); on: rendered again
        Param("overwrite", "Overwrite results", "bool", False, row="flags",
              hint="Render again what already has results"),
        Param("reset", "New conversation", "icon", "reset", row="flags", visible=_exec,
              hint="New conversation; built nodes stay"),
        Param("output_dir", "Output folder", "folder", DEFAULT_OUTPUT, visible=_normal,
              hint="Where rendered files are saved"),
        Param("file_name", "File name", "string", "", visible=_normal, hint="Rendered files' name (else node name)"),
        # Executor mode: what you and the brain said (read from its transcript, never stored), and under it where
        # you type to it - Enter sends, Shift+Enter starts a new line
        Param("chat", "Conversation", "chat_view", None, visible=_exec,
              placeholder="Your messages and the brain's answers appear here",
              hint="You and the brain, from its transcript"),
        Param("ask", "Prompt", "text", "", visible=_exec, submit="send", lines=4,
              placeholder="Enter sends, Shift+Enter: new line",
              hint="Your words; before the connected prompt"),
        # Executor mode: only Stop (greyed while nothing runs) - rendering what was built / freeing memory: ask the brain
        Param("render", "Render", "button", None, visible=lambda n: _normal(n) and not n.busy, hint="Render this node"),
        Param("stop", "Stop", "button", None, visible=lambda n: n.busy or _exec(n), enabled=lambda n: n.busy,
              hint="Stop rendering or the brain"),
        # Executor mode: after Stop, a Budget $ per service set up (executor.py, "budget:<service>") - what the brain
        # may spend there without asking (0: it asks first), counted from the start of the conversation
        Param("budget_from", "Budget since", "data", ""),
        Param("free", "Free memory", "button", None, visible=lambda n: _normal(n) and not n.busy,
              hint="Unload the models from memory"),
        # Executor mode: the language the Prompt box is spell-checked in
        Param("spell", "Spell check", "choice", spellcheck.DEFAULT, options=spellcheck.OPTIONS, visible=_exec,
              hint="Spell-check language of the Prompt box"),
        Param("result", "Result", "image_view", "", visible=lambda n: _normal(n) and n.produces == "image"),
        Param("result_text", "Result", "text_view", "", visible=lambda n: _normal(n) and n.produces == "text",
              hint="The text the model wrote"),
        Param("result_reasoning", "Reasoning", "text_view", "",
              visible=lambda n: _normal(n) and bool(n.params["result_reasoning"]), hint="The model's thinking"),
        Param("result_audio", "Result", "audio_view", "", visible=lambda n: _normal(n) and n.produces == "audio"),
        Param("result_video", "Result", "video_view", "", visible=lambda n: _normal(n) and n.produces == "video"),
        # a structured result (e.g. a video's shots): the JSON file, shown the way its content reads best
        Param("result_data", "Result", "data_view", "", visible=lambda n: _normal(n) and n.produces == "data"),
        # a 3D result (e.g. an online image-to-3D model's GLB), in the app's 3D viewer
        Param("result_geometry", "Result", "geometry_view", "", visible=lambda n: _normal(n) and n.produces == "geometry"),
        # Executor mode: everything the brain did
        Param("log", "Log", "log_view", "", visible=_exec, placeholder="Everything the brain does, with its tool calls",
              hint="Everything the brain did, with tool calls"),
        Param("built", "Built", "data", None),       # {"tab", "nodes", "where"} - what the brain built
        Param("session_id", "Session", "data", ""),  # the brain's Claude Code session (resumed by the next message)
        Param("sessions_before", "Earlier sessions", "data", None),  # sessions that handed over (Conversation reads them)
        Param("online_job", "Online job", "data", None),  # a service job while it runs: {service, model, id, get}
    ]

    def __init__(self, name):
        super().__init__(name)
        self.running = False
        self.state = ""          # last run: "Done in 52 s" / "Failed: ..."
        self.stage = ""          # live engine message while running
        self.problems = []
        self._engine = None      # the worker running this node's job
        self._waiting = set()    # upstream Modalities being rendered first (render() of a chain)
        self._online = None      # an online model's request (online_run.OnlineRun / service_run.ServiceRun)
        self.geometry_info = {}  # what the 3D viewer loaded of a 3D result (geometry_view.py)
        self._signature = None
        self._init_executor()
        self.analyze()

    # ---- what is plugged in ------------------------------------------------
    def models(self):
        return [m for m in self.input_values(IN_MODEL) if m is not None]

    def prompt(self):
        """All connected text joined into one prompt (a Merge of texts is read in index order)."""
        return "\n\n".join(t.strip() for t in self.input_values(IN_TEXT) if isinstance(t, str) and t.strip())

    def data(self, kind=None):
        """[(name, value, kind)] of the data input, in order - all, or one kind (image / video / audio / json /
        file / text)."""
        return [it for it in self.data_items(IN_DATA) if kind is None or it[2] == kind and it[1]]

    def images(self):
        return [v for _n, v, _k in self.data(IMAGE)]

    def videos(self):
        return [v for _n, v, _k in self.data(VIDEO)]

    def audios(self):
        return [v for _n, v, _k in self.data(AUDIO)]

    @property
    def produces(self):
        """What the last render actually produced (known only after rendering)."""
        for key, kind in (("result", None), ("result_video", None), ("result_audio", None), ("result_data", None),
                          ("result_geometry", "geometry"), ("result_text", "text")):
            value = self.params[key]
            if value:
                return kind or EXT_KINDS.get(os.path.splitext(value)[1].lower())
        return None

    def online_model(self):
        """The connected model running online (normal mode: it answers like a local language model), or None."""
        return next((m for m in self.models() if getattr(m, "online", False)), None)

    def python(self):
        """The one Python environment all connected models run in (None if missing or conflicting)."""
        pythons = {m.python() for m in self.models()}
        return pythons.pop() if len(pythons) == 1 else None

    @property
    def busy(self):
        """Rendering, or (Executor mode) the brain working / waiting for you / running what it built."""
        return self.running or (self.executor_on and (self.phase in BUSY or self.rendering))

    def analyze(self):
        if self.executor_on:
            self.problems = self.executor_problems()
            self.output.set_type(ANY)
            return
        problems = []
        models = self.models()
        if not models:
            problems.append("Connect a model")
        elif self.online_model() is not None:
            if len(models) > 1:
                problems.append("An online model runs on its own - connect only it")
            elif self.online_model().problem():
                problems.append(self.online_model().problem())
        elif any(not m.python() for m in models):
            problems.append("A model's Python env folder has no python.exe")
        elif len({os.path.normcase(m.python()) for m in models}) > 1:
            problems.append("The connected models use different Python envs")
        self.problems = problems
        self.output.set_type(OUTPUT_TYPES.get(self.produces, ANY))

    def upstream_changed(self):
        self.analyze()
        sig = (tuple(self.problems), tuple(m.name for m in self.models()), self.python(), self.executor_on)
        if sig != self._signature:
            self._signature = sig
            self.notify()

    # ---- display ---------------------------------------------------------
    def problem(self):
        return self.problems[0] if self.problems and not self.busy else ""

    def subtitle(self):
        if self.executor_on:
            return "" if self.problems and not self.busy else self.executor_subtitle()
        if self.running:
            return self.stage or "Starting…"
        if self.problems:
            return ""  # problem() is shown instead
        return " + ".join(m.name for m in self.models()) + (f" · {self.state}" if self.state else "")

    def info_rows(self):
        if self.executor_on:
            rows = self.executor_rows()
            if self.problems:
                rows += [("•", p) for p in self.problems]
            return rows
        rows = []
        for m in self.models():
            declared = next((v for label, v in m.origin if label == "Declared task"), None)
            rows.append(("Model", m.name + (f" - {declared}" if declared else "")))
        names = [n for n, _ in self.input_named(IN_TEXT)]
        if names:  # the names a model is told (e.g. "summarize prompt1")
            rows.append(("Text", ", ".join(names)))
        for i, (name, _v, kind) in enumerate(self.data(), 1):
            rows.append(("Data" if i == 1 else "", f"{i}. {name} · {kind}"))
        if self.online_model() is not None:
            rows.append(("Runs on", self.online_model().subtitle()))
            rows += self.service_preview()
        elif self.python():
            rows.append(("Runs with", self.python()))
        if self.running:
            rows.append(("Status", self.stage or "Starting…"))
        elif self.problems:
            rows.append(("Status", "Not ready"))
            rows += [("•", p) for p in self.problems]
        else:
            rows.append(("Status", self.state or "Ready"))
        for key, kind in (("result", "image"), ("result_video", "video"), ("result_data", "data"),
                          ("result_geometry", "geometry")):
            if self.params[key] and self.produces == kind:
                rows.append(("File", self.params[key]))
        return rows

    # ---- rendering -------------------------------------------------------
    def param_changed(self, name):
        if name == "online_job" and self.params.get("online_job") and not self.running:
            # opened with a service job that was still running when the app closed: follow it (no new run)
            from PySide6.QtCore import QTimer
            QTimer.singleShot(1500, lambda: self.scene() is not None and self.resume_online())
        if name == "executor":  # the same node, another job: problems, look and pane follow
            self.analyze()
            self.update()

    @property
    def body_colors(self):
        top, bottom, detail, groove = Node.body_colors
        return (top, bottom, detail, EXEC_GROOVE if self.executor_on else groove)

    def action(self, name):
        if self.executor_on and name in ("send", "stop", "reset"):
            self.executor_action(name)
        elif name == "render":
            if self.confirm_cost():
                self.render()
        elif name == "stop" and self.running and self._online is not None:
            self._online.stop()
        elif name == "stop" and self.running and self._waiting:
            self._stop_waiting("Stopped")
        elif name == "stop" and self.running and self._engine:
            self._engine.cancel()
        elif name == "free":
            self.free_memory()

    def resume_online(self):
        """A service job that was running when the app closed (or crashed): follow it to its result."""
        job = self.params.get("online_job")
        if not job or self.running or self.executor_on:
            return False
        m = self.online_model()
        from .service_run import ServiceRun
        self._run_facts = None
        self.running, self.stage, self.progress = True, "Picking up…", None
        self._online = ServiceRun(self, m, self.build_job())
        self._online.resume(job)
        self.notify()
        return True

    def _service_run(self):
        m = self.online_model()
        if m is None or m.service_entry() is None:
            return None
        from .service_run import ServiceRun
        return ServiceRun(self, m, self.build_job())

    def service_preview(self):
        """Where each input goes on the service model, and what the run will cost - before it runs."""
        run = self._service_run()
        if run is None:
            return []
        s, d, inputs, plan, unused, problem = run.plan()
        if d is None:
            return [("Inputs", problem)] if problem else []
        rows = []
        fields = {f["name"]: f for f in d.get("fields") or []}
        for i, (k, v) in enumerate((plan or {}).items()):
            rows.append(("Inputs" if i == 0 else "", f"{k} ← {', '.join(n for n, _p in v)}"))
        from . import online
        t = online.text_field(list(fields.values()))
        if t and (inputs or {}).get(t):
            rows.append(("Inputs" if not rows else "", f"{t} ← the prompt"))
        if unused:
            rows.append(("Not sent", ", ".join(unused) + " (no input for them)"))
        if problem:
            rows.append(("•", problem))
        est = run.estimate()
        rows.append(("Cost", est["text"] if est else "the service shows no price for it"))
        return rows

    def confirm_cost(self):
        """Before a paid run the user started: ask when it may cost more than the service's confirm_over dollars
        (services/<name>.json, default 1.00) or when its price can't be told."""
        run = self._service_run()
        if run is None:
            return True
        from . import services
        s = services.entry(run.model_node.params.get("provider")) or {}
        limit = float((s.get("settings") or {}).get("confirm_over", 1.0))
        est = run.estimate()
        high = est["high"] if est else None
        if high is not None and high <= limit:
            return True
        from PySide6.QtWidgets import QApplication, QMessageBox
        what = est["text"] if est else "its price isn't shown by the service"
        answer = QMessageBox.question(QApplication.activeWindow(), "Paid run",
                                      f"Run {run.model_node.subtitle()}?\n\nCost: {what}\n\n(Asked because it may be "
                                      f"over ${limit:.2f} - change confirm_over in {s.get('path') or 'the service file'})",
                                      QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        return answer == QMessageBox.Yes

    def free_memory(self, mods=None):
        """Stop the worker holding this node's models: the GPU/RAM they use is released completely.
        Every Modality using the same Python env shares that worker; the next Render loads models again.
        Executor mode (the brain's free_memory): the workers of what it built (its renders loaded those models)."""
        mods = self.built_modalities() if mods is None else mods
        pythons = ({m.python() for m in mods} - {None}) if self.executor_on else {self.python()}
        engines = [e for e in (existing_engine(p) for p in pythons) if e is not None and e.running]
        if not engines:
            self.state = "Nothing loaded"
        elif any(e.busy for e in engines):
            self.state = "A render is running - stop it first"
        else:
            for e in engines:
                e.shutdown()
            self.state = "Memory freed - models load again on the next Render"
        if self.executor_on:
            self.add_log(self.state)
        self.notify()

    def build_job(self):
        """Everything connected, as facts. The engine decides how (and whether) it can run it."""
        return {
            "models": [{"name": m.name, "path": m.model_path(), "role": m.role, "family": m.info.family,
                        "media": m.info.media, "params": dict(m.params)}
                       for m in self.models()],
            "sdcpp": sdcpp_exe(),  # stable-diffusion.cpp, for GGUF models
            "prompt": self.prompt(),
            "images": self.images(), "videos": self.videos(), "audios": self.audios(),
            # the same inputs with the names of the nodes they come from (so a model can be told "prompt1"); a text
            # among the data is material to read, like a connected prompt
            "named": {"text": [[n, v] for n, v in self.input_named(IN_TEXT) if isinstance(v, str) and v.strip()]
                      + [[n, v] for n, v, _k in self.data(TEXT)],
                      **{kind: [[n, v] for n, v, _k in self.data(kind)] for kind in (IMAGE, VIDEO, AUDIO)}},
            # JSON and other files: listed for the engine (models that take files read them from here)
            "files": [[n, v, k] for n, v, k in self.data() if k in (JSON, "file")],
            "output_dir": self.params["output_dir"] or DEFAULT_OUTPUT,
            "name": self.params["file_name"].strip() or self.name,
            # the Executor that asked for this render (its paid runs count against its budget, also once the node
            # is deleted or renamed)
            "ordered_by": getattr(self, "ordered_by", ""),
        }

    def missing_inputs(self, overwrite=False):
        """The Modalities upstream (directly or through Merge / Edit Media / Isolate...) with no result yet - or,
        with Overwrite results, all of them (rendered again)."""
        return [m for m in upstream_modalities(self) if overwrite or not m.value()]

    def render(self, overwrite=None):
        """Render - after whatever it takes in that hasn't been made yet: an upstream Modality without a result is
        rendered first (and ITS inputs before it), this one follows when they are done. Jobs go to the worker in
        that order, so a chain runs by stage (all of one model, then the next) instead of reloading models.
        Overwrite results (this node's tick, handed down the chain): the upstream ones are rendered again too."""
        self.analyze()
        if self.running or self.problems:
            self.notify()
            return
        overwrite = bool(self.params.get("overwrite")) if overwrite is None else overwrite
        missing = self.missing_inputs(overwrite)
        if missing:
            self.wait_for(missing, start=True, overwrite=overwrite)
            return
        online = self.online_model()
        if online is not None:  # one request: the prompt (and data) in, the answer / files out
            self._run_facts = None
            self.running, self.stage, self.progress = True, "Asking…", None
            if online.service_entry() is not None:  # a service model (Replicate...): a paid job there
                from .service_run import ServiceRun
                self._online = ServiceRun(self, online, self.build_job())
            else:  # Claude Code
                from .online_run import OnlineRun
                self._online = OnlineRun(self, online, self.build_job())
            self._online.start()
            self.notify()
            return
        _watch_engines()
        self._engine = get_engine(self.python())
        job = self.build_job()
        self._run_facts = {"models": [m["path"] for m in job["models"]], "settings": {
            k: v for m in job["models"] for k, v in m["params"].items()
            if m["role"] in ("diffusion", "checkpoint") and (k in ("width", "height", "steps", "cfg")
                                                            or (k == "frames" and "video" in (m["media"] or "")))}}
        job_id = self._engine.submit(job)
        _jobs[job_id] = self
        self.running, self.stage, self.progress = True, "Starting engine…", 0.0
        self.notify()

    def value(self):
        """The result - lets this node feed another node (image path, text, audio path)."""
        if self.executor_on:
            return None
        return {"image": self.params["result"], "text": self.params["result_text"],
                "audio": self.params["result_audio"], "video": self.params["result_video"],
                "data": self.params["result_data"], "geometry": self.params["result_geometry"]}.get(self.produces) or None

    def image_path(self):
        return self.params["result"]

    def remove(self):
        self.executor_remove()
        if self._online is not None:
            self._online.stop()
        if self.running and self._waiting:
            self._stop_waiting("Stopped")
        elif self.running and self._engine:
            self._engine.cancel()
        super().remove()

    def wait_for(self, sources, start=False, overwrite=False):
        """Render once each of these upstream Modalities has finished (start=True: start the ones not running;
        overwrite: they render their own inputs again too)."""
        self._waiting = set(sources)
        self.running, self.progress = True, None
        self.stage = "waiting for " + ", ".join(m.name for m in sources)
        for m in sources:
            _waiters.setdefault(m, []).append(self)
            if start and not m.running:
                m.render(overwrite=overwrite)
            if start and not m.running:  # it can't start (no model, ...): nothing to wait for
                self._stop_waiting(f"Not rendered: {m.name} can't render ({m.problem() or 'not ready'})")
                return
        self.notify()

    def _stop_waiting(self, state):
        """No longer waiting for its inputs to be rendered (they carry on - other nodes may need them)."""
        for waiters in _waiters.values():
            while self in waiters:
                waiters.remove(self)
        self._waiting = set()
        self._on_end(state)

    def _input_done(self, source, ok):
        """An upstream Modality this one waits for has finished."""
        if self not in _waiters.get(source, []) and source not in self._waiting:
            return
        self._waiting.discard(source)
        if not ok:
            self._stop_waiting(f"Not rendered: {source.name} failed")
            return
        if not self._waiting:
            self.running, self.stage = False, ""
            self.render(overwrite=False)  # its inputs are there now (or it waits for what is still missing)
            if not self.running:  # it couldn't start after all
                self._on_end(self.state or "Not rendered: " + (self.problem() or "not ready"))

    def readded(self):
        """Undo brought the node back: a job cancelled by the delete isn't resumed."""
        if self.running:
            self.running, self.progress, self.stage, self.state = False, None, "", "Stopped"

    # called from engine signals
    def _on_status(self, message):
        self.stage = message
        self.notify()

    def _on_progress(self, step, total):
        self.progress = step / total
        self.stage = f"Rendering step {step}/{total}"
        self.notify()

    def _on_end(self, state, result=None):
        self.running, self.progress, self.state, self.stage = False, None, state, ""
        if result:
            kind = result.get("kind", "image")
            values = {"result": result["path"] if kind == "image" else "",
                      "result_video": result["path"] if kind == "video" else "",
                      "result_data": result["path"] if kind == "data" else "",
                      "result_geometry": result["path"] if kind == "geometry" else "",
                      "result_audio": result["path"] if kind == "audio" else "",
                      "result_text": result.get("text", "") if kind == "text" else "",
                      "result_reasoning": result.get("reasoning", "") if kind == "text" else ""}
            for name, value in values.items():  # only the latest result is kept
                self.params[name] = value
            self.set_param("result_text", values["result_text"])  # notifies the pane + downstream nodes
            record_run(getattr(self, "_run_facts", None), result)
        self.analyze()  # the output type follows what was actually produced
        self.notify()
        for waiter in _waiters.pop(self, []):  # what waited for this result goes on (or gives up)
            waiter._input_done(self, result is not None)


RUNS_LOG = os.path.join(PROJECT_ROOT, "logs", "model_runs.jsonl")


def record_run(facts, result):
    """A finished render, as measured on this machine: which model files, the settings, how long (the first run of
    a model includes loading it). Read back by the Executor's list_models - how fast a model really runs here."""
    if not facts or not result:
        return
    import json
    import time
    line = {**facts, "seconds": result.get("seconds"), "render_seconds": result.get("render_seconds"),
            "kind": result.get("kind"), "peak_gb": result.get("peak_gb"), "gpu": result.get("gpu"),
            "when": time.strftime("%Y-%m-%d %H:%M")}
    try:
        os.makedirs(os.path.dirname(RUNS_LOG), exist_ok=True)
        with open(RUNS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


def upstream_modalities(node, seen=None):
    """The nearest Modality nodes feeding `node` - through any other nodes (Merge, Edit Media, Isolate, Chunk...),
    not past a Modality (it looks after its own inputs)."""
    seen = set() if seen is None else seen
    found = []
    for sock in node.input_sockets:
        for e in sock.edges:
            src = e.source.node
            if src in seen:
                continue
            seen.add(src)
            if isinstance(src, ModalityNode):
                found.append(src)
            else:
                found += upstream_modalities(src, seen)
    return found


# ---- engine signal routing (one connection for all Modality nodes) --------
_jobs = {}
_waiters = {}  # Modality -> the Modalities waiting for its result (render() of a chain)
_watching = False


def _node_for(job_id, final=False):
    node = _jobs.pop(job_id, None) if final else _jobs.get(job_id)
    return node if node is not None and node.scene() is not None else None


def _watch_engines():
    global _watching
    if not _watching:
        _watching = True
        on_new_engine(_connect)


def _connect(eng):
    eng.status.connect(lambda j, m: (n := _node_for(j)) and n._on_status(m))
    eng.progress.connect(lambda j, s, t: (n := _node_for(j)) and n._on_progress(s, t))
    eng.finished.connect(lambda j, r: (n := _node_for(j, True)) and n._on_end(
        f"Done in {r['seconds']:.0f} s" + (f" · Note: {r['note']}" if r.get("note") else ""), r))
    eng.failed.connect(lambda j, m: (n := _node_for(j, True)) and n._on_end(f"Failed: {m}"))
    eng.cancelled.connect(lambda j: (n := _node_for(j, True)) and n._on_end("Stopped"))
