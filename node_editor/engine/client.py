"""UI side of the engine: runs a worker process per Python environment and relays its messages as Qt signals.

Models run in whatever Python environment their Model node points to (its venv folder), so each
model can have the library versions it needs. The editor itself only needs Qt.
"""

import itertools
import json
import os
import tempfile
from collections import deque

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
LOG_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "engine.log")
_ids = itertools.count(1)


def python_in(env_folder):
    """The python executable inside a virtual environment folder (or the path itself if it's a python.exe)."""
    env_folder = (env_folder or "").strip().strip('"')
    if env_folder.lower().startswith("ssh://"):  # a worker on another machine (engine/remote.py)
        return env_folder
    if env_folder.lower().endswith(("python.exe", "python")) and os.path.isfile(env_folder):
        return env_folder
    for rel in (("Scripts", "python.exe"), ("python.exe",), ("bin", "python")):
        candidate = os.path.join(env_folder, *rel)
        if env_folder and os.path.isfile(candidate):
            return candidate
    return None


class Engine(QObject):
    status = Signal(str, str)          # job id, message
    progress = Signal(str, int, int)   # job id, step, total
    finished = Signal(str, dict)       # job id, result {"path", "seconds", ...}
    failed = Signal(str, str)          # job id, message
    cancelled = Signal(str)            # job id

    def __init__(self, python):
        super().__init__()
        self.python = python
        self._proc = None
        self._buffer = b""
        self._stderr = deque(maxlen=40)
        self._pending = []  # job ids sent to the worker, oldest first
        self._cancel_file = os.path.join(tempfile.gettempdir(),
                                         f"node_editor_cancel_{os.getpid()}_{abs(hash(python))}.flag")

    def submit(self, job) -> str:
        job_id = f"job{next(_ids)}"
        if not self._pending and os.path.exists(self._cancel_file):
            os.remove(self._cancel_file)  # stale stop request from an earlier job
        self._ensure_started()
        self._pending.append(job_id)
        self._send({"cmd": "run", "id": job_id, "job": {**job, "cancel_file": self._cancel_file}})
        return job_id

    def cancel(self):
        """Stop the running job (the worker checks this flag file between steps)."""
        if self._pending:
            open(self._cancel_file, "w").close()

    @property
    def busy(self):
        return bool(self._pending)

    @property
    def running(self):
        """A worker process is alive (and may be holding models in memory)."""
        return self._proc is not None and self._proc.state() != QProcess.NotRunning

    def shutdown(self):
        if self._proc:
            self._proc.kill()
            self._proc.waitForFinished(3000)
            self._proc = None

    # ---- process -------------------------------------------------------
    def _ensure_started(self):
        if self._proc and self._proc.state() != QProcess.NotRunning:
            return
        self._proc = QProcess(self)
        env = QProcessEnvironment.systemEnvironment()  # full PATH etc.; GPU libraries need it
        env.insert("PYTHONUNBUFFERED", "1")
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("HF_HUB_OFFLINE", "1")  # never reach out to the internet while rendering
        # models by repo id come from the app's own store, whatever HF_HOME Windows has; torch's downloads
        # (e.g. torchvision backbones a library fetches) live beside it
        from ..settings import hf_store
        store = hf_store()
        env.insert("HF_HOME", store)
        env.insert("TORCH_HOME", os.path.join(store, "torch"))
        env.remove("PYTHONHOME")
        env.remove("PYTHONPATH")
        self._proc.setProcessEnvironment(env)
        self._proc.readyReadStandardOutput.connect(self._read_stdout)
        self._proc.readyReadStandardError.connect(self._read_stderr)
        self._proc.finished.connect(self._on_exit)
        self._log(f"\n===== starting worker with {self.python} =====\n")
        self._proc.start(self.python, ["-u", WORKER])

    def _send(self, msg):
        self._proc.write((json.dumps(msg) + "\n").encode("utf-8"))

    def _read_stdout(self):
        self._buffer += bytes(self._proc.readAllStandardOutput())
        *lines, self._buffer = self._buffer.split(b"\n")
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            self._dispatch(msg)

    def _read_stderr(self):
        text = bytes(self._proc.readAllStandardError()).decode("utf-8", "replace")
        self._stderr.extend(l for l in text.splitlines() if l.strip())
        self._log(text)

    @staticmethod
    def _log(text):
        try:  # full engine output (warnings, tracebacks) for troubleshooting
            os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(text)
        except OSError:
            pass

    def _dispatch(self, msg):
        job_id, event = msg.get("id"), msg.get("event")
        if event == "status":
            self.status.emit(job_id, msg["message"])
        elif event == "progress":
            self.progress.emit(job_id, msg["step"], msg["total"])
        elif event in ("done", "error", "cancelled"):
            if job_id in self._pending:
                self._pending.remove(job_id)
            if event == "done":
                self.finished.emit(job_id, msg)
            elif event == "error":
                self.failed.emit(job_id, msg["message"])
            else:
                self.cancelled.emit(job_id)

    def _on_exit(self, code, _status):
        # The worker died (crash, out of memory, python couldn't start...): fail whatever was still running.
        detail = self._stderr[-1] if self._stderr else f"exit code {code}"
        for job_id in self._pending:
            self.failed.emit(job_id, f"Engine stopped: {detail}")
        self._pending.clear()
        self._proc = None


_engines = {}   # python executable -> Engine
_on_new = []    # callbacks run for each new Engine (to connect its signals)


def _key(python):
    return python.strip() if python.strip().lower().startswith("ssh://") else os.path.normcase(os.path.abspath(python))


def get_engine(python) -> Engine:
    """The engine (worker process) for a Python executable, created on first use."""
    key = _key(python)
    if key not in _engines:
        if key.lower().startswith("ssh://"):
            from .remote import RemoteEngine
            _engines[key] = RemoteEngine(key, Engine._log)
        else:
            _engines[key] = Engine(python)
        for callback in _on_new:
            callback(_engines[key])
    return _engines[key]


def existing_engine(python):
    """The engine for a Python executable if one was started, else None (doesn't start one)."""
    return _engines.get(_key(python)) if python else None


def on_new_engine(callback):
    _on_new.append(callback)
    for engine in _engines.values():
        callback(engine)


def shutdown_all():
    for engine in _engines.values():
        engine.shutdown()
