"""A worker on another machine (a rented GPU pod) reached over SSH - the same worker.py, the same JSON lines.

A Python environment written as  ssh://user@host:port/path/to/venv?gpu=1&key=C:/Users/me/.ssh/id_ed25519
runs its models there. What differs from a local worker is only files:
- the engine's code is copied over when the worker starts (engine/*.py + assets);
- a job's local paths are mapped: models in the app's HF store → the pod's HF store (a missing repo revision is
  downloaded there first, at datacenter speed); other files → uploaded once (keyed by path, size, date) into
  <files>/; the output folder → <out>/<key>; the stop flag → a file on the pod;
- results written there are fetched into the job's own output folder and the result names the local file.
Keys never go to the pod; only ssh's own key file is used, by ssh. Jobs run one at a time, like a local worker.
Query options: gpu (CUDA_VISIBLE_DEVICES), key (ssh identity file), hf (the pod's HF_HOME, default /workspace/hf),
work (the pod's folder for code / files / results, default /workspace/tauui).
"""

import base64
import hashlib
import io
import json
import os
import queue
import re
import subprocess
import tarfile
import threading
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, Signal

HERE = os.path.dirname(os.path.abspath(__file__))
CHUNK = 8 << 20  # bytes per upload message
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_LOCAL_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def is_remote(env):
    return (env or "").strip().lower().startswith("ssh://")


class Target:
    """Where the worker runs, read from the ssh:// address."""

    def __init__(self, url):
        u = urlparse(url.strip())
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        self.url = url.strip()
        self.user_host = f"{u.username}@{u.hostname}" if u.username else u.hostname
        self.port = str(u.port or 22)
        self.venv = u.path.rstrip("/") or "/workspace/venv"
        self.gpu = q.get("gpu", "")
        self.key = q.get("key", "")
        self.hf = q.get("hf", "/workspace/hf").rstrip("/")
        self.work = q.get("work", "/workspace/tauui").rstrip("/")
        # share=1: other apps use the same GPU - one job at a time (a lock on the pod), memory given back after each
        self.lock = f"/tmp/tauui_gpu{self.gpu or 'all'}.lock" if q.get("share") in ("1", "true", "yes") else ""
        self.tag = f"tauui-{os.getpid()}-{self.gpu or 'all'}"  # names this app's worker on the pod
        self.code = f"{self.work}/engine_{self.tag}"

    def ssh(self, command):
        args = ["ssh", "-p", self.port, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ServerAliveInterval=30", "-o", "ConnectTimeout=20"]
        if self.key:
            args += ["-i", os.path.expanduser(self.key)]
        return args + [self.user_host, command]

    def run(self, command, data=None, timeout=3600):
        p = subprocess.run(self.ssh(command), input=data, capture_output=True, timeout=timeout,
                           creationflags=NO_WINDOW)
        if p.returncode:
            raise RuntimeError((p.stderr or p.stdout).decode("utf-8", "replace").strip()[-800:]
                               or f"ssh exit code {p.returncode}")
        return p.stdout.decode("utf-8", "replace")


def _code_tar():
    """engine/*.py and its assets, as a gzipped tar."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, names in os.walk(HERE):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for n in names:
                if n.endswith(".pyc"):
                    continue
                full = os.path.join(root, n)
                tar.add(full, arcname=os.path.relpath(full, HERE).replace("\\", "/"))
    return buf.getvalue()


def pod_path(t, path, plan):
    """Where a local file / folder is on the pod. Models in the app's HF store → the pod's store (plan["models"]:
    repo revisions that must be there); anything else → <work>/files/<key>/<name> (plan["uploads"]: pod path →
    local file; key = path + size + date, so a file used by many jobs goes once)."""
    from ..settings import hf_store
    store = os.path.normcase(os.path.abspath(hf_store()))
    full = os.path.abspath(path)
    norm = os.path.normcase(full)
    if norm == store or norm.startswith(store + os.sep):
        rel = os.path.relpath(full, hf_store()).replace("\\", "/")
        parts = rel.split("/")
        if len(parts) >= 4 and parts[0] == "hub" and parts[2] == "snapshots":
            plan["models"][f"{t.hf}/hub/{parts[1]}/snapshots/{parts[3]}"] = (parts[1], parts[3])
        return f"{t.hf}/{rel}"
    st = os.stat(full)
    key = hashlib.sha1(f"{norm}|{st.st_size}|{st.st_mtime_ns}".encode()).hexdigest()[:16]
    base = f"{t.work}/files/{key}/{os.path.basename(full.rstrip(os.sep))}"
    if os.path.isdir(full):
        for root, _dirs, names in os.walk(full):
            for n in names:
                f = os.path.join(root, n)
                plan["uploads"][f"{base}/{os.path.relpath(f, full).replace(os.sep, '/')}"] = f
    else:
        plan["uploads"][base] = full
    return base


def download_there(t, folder, rev):
    """A Hugging Face repo revision → the pod's store, downloaded there. False when the pod can't (e.g. a gated
    repo: no Hugging Face token goes to the pod) - then the caller uploads it from here."""
    kind, _, repo = folder.partition("--")
    rtype = {"models": "model", "datasets": "dataset", "spaces": "space"}.get(kind, "model")
    try:
        t.run(f"HF_HOME={t.hf} HF_HUB_ENABLE_HF_TRANSFER=1 {t.venv}/bin/hf download {repo.replace('--', '/')} "
              f"--repo-type {rtype} --revision {rev} > /dev/null", timeout=6 * 3600)
        return True
    except RuntimeError:
        return False


def local_snapshot(t, folder, rev):
    """{pod path: local file} of a repo revision in the app's HF store (links resolved)."""
    from ..settings import hf_store
    snap = os.path.join(hf_store(), "hub", folder, "snapshots", rev)
    files = {}
    for root, _dirs, names in os.walk(snap):
        for n in names:
            f = os.path.join(root, n)
            files[f"{t.hf}/hub/{folder}/snapshots/{rev}/{os.path.relpath(f, snap).replace(os.sep, '/')}"] =                 os.path.realpath(f)
    return files


class RemoteEngine(QObject):
    """Same signals and calls as client.Engine; the work happens in a thread, signals reach the Qt thread queued."""
    status = Signal(str, str)
    progress = Signal(str, int, int)
    finished = Signal(str, dict)
    failed = Signal(str, str)
    cancelled = Signal(str)

    def __init__(self, url, log):
        super().__init__()
        self.python = url
        self.t = Target(url)
        self._log = log
        self._jobs = queue.Queue()
        self._pending = []
        self._proc = None
        self._lines = None
        self._thread = None
        self._uploaded = set()   # remote paths known to exist (this session)
        self._seq = 0

    # ---- the calls the app makes ------------------------------------------------------------------------
    def submit(self, job):
        from .client import _ids
        job_id = f"job{next(_ids)}"
        self._pending.append(job_id)
        self._jobs.put((job_id, job))
        if self._thread is None:
            self._thread = threading.Thread(target=self._pump, daemon=True)
            self._thread.start()
        return job_id

    def cancel(self):
        if self._pending:
            threading.Thread(target=self._quiet, args=(f"touch {self._flag()}",), daemon=True).start()

    @property
    def busy(self):
        return bool(self._pending)

    @property
    def running(self):
        return self._proc is not None and self._proc.poll() is None

    def shutdown(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.stdin.close()
            except OSError:
                pass
            proc.kill()
        # the pod side: the worker (and its GPU memory) goes with the connection's end, or by name
        threading.Thread(target=self._quiet, args=(f"pkill -f {self.t.tag} || true",), daemon=True).start()

    # ---- worker on the pod ------------------------------------------------------------------------------
    def _flag(self):
        return f"/tmp/{self.t.tag}.stop"

    def _quiet(self, command):
        try:
            self.t.run(command, timeout=60)
        except Exception as e:  # noqa: BLE001 - best effort
            self._log(f"[remote] {command}: {e}\n")

    def _start(self):
        if self.running:
            return
        self._log(f"\n===== starting remote worker {self.t.url} =====\n")
        self.t.run(f"mkdir -p {self.t.code} && tar xzf - -C {self.t.code}", data=_code_tar(), timeout=300)
        env = (f"HF_HOME={self.t.hf} TORCH_HOME={self.t.hf}/torch HF_HUB_OFFLINE=1 PYTHONUNBUFFERED=1 "
               f"PYTHONIOENCODING=utf-8" + (f" CUDA_VISIBLE_DEVICES={self.t.gpu}" if self.t.gpu else "")
               + (f" TAUUI_GPU_LOCK={self.t.lock}" if self.t.lock else ""))
        cmd = f"cd {self.t.code} && rm -f {self._flag()} && {env} exec {self.t.venv}/bin/python -u worker.py {self.t.tag}"
        self._proc = subprocess.Popen(self.t.ssh(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        self._lines = queue.Queue()
        proc = self._proc

        def read_out():
            for raw in proc.stdout:
                try:
                    self._lines.put(json.loads(raw))
                except ValueError:
                    pass
            self._lines.put(None)  # the connection ended

        def read_err():
            for raw in proc.stderr:
                self._log(raw.decode("utf-8", "replace"))

        threading.Thread(target=read_out, daemon=True).start()
        threading.Thread(target=read_err, daemon=True).start()
        msg = self._next(timeout=120)
        if not msg or msg.get("event") != "ready":
            raise RuntimeError("the remote worker didn't start - see logs/engine.log")
        self._uploaded.clear()

    def _send(self, msg):
        self._proc.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
        self._proc.stdin.flush()

    def _next(self, timeout=None):
        try:
            msg = self._lines.get(timeout=timeout)
        except queue.Empty:
            return None
        if msg is None:
            raise RuntimeError("the connection to the pod ended")
        return msg

    def _ask(self, msg, event):
        """Send a file command and wait for its answer (no job runs meanwhile)."""
        self._seq += 1
        rid = f"f{self._seq}"
        self._send({**msg, "id": rid})
        while True:
            m = self._next()
            if m.get("id") == rid and m.get("event") in (event, "file_error"):
                if m["event"] == "file_error":
                    raise RuntimeError(f"pod file {m.get('path')}: {m.get('message')}")
                return m

    # ---- one job ----------------------------------------------------------------------------------------
    def _pump(self):
        while True:  # lives as long as the app (a daemon thread waiting for jobs)
            job_id, job = self._jobs.get()
            try:
                self._run(job_id, job)
            except Exception as e:  # noqa: BLE001 - every failure reaches the node
                self._log(f"[remote] {type(e).__name__}: {e}\n")
                if not self.running:
                    self._proc = None
                self.failed.emit(job_id, f"Remote worker: {e}")
            finally:
                if job_id in self._pending:
                    self._pending.remove(job_id)

    def _run(self, job_id, job):
        self._start()
        self.status.emit(job_id, "Sending files to the pod…")
        mapped, plan = self._map(job)
        self._models_there(job_id, plan["models"])
        self._upload(job_id, plan["uploads"])
        self._send({"cmd": "run", "id": job_id, "job": {**mapped, "cancel_file": self._flag()}})
        while True:
            m = self._next()
            if m.get("id") != job_id:
                continue
            ev = m.get("event")
            if ev == "status":
                self.status.emit(job_id, m["message"])
            elif ev == "progress":
                self.progress.emit(job_id, m["step"], m["total"])
            elif ev == "done":
                self.status.emit(job_id, "Fetching the result from the pod…")
                self.finished.emit(job_id, self._fetch(m, plan["out"], job.get("output_dir") or ""))
                return
            elif ev == "error":
                self.failed.emit(job_id, m["message"])
                return
            elif ev == "cancelled":
                self.cancelled.emit(job_id)
                return

    def _map(self, job):
        """The job with pod paths, and what has to be there first."""
        out_local = job.get("output_dir") or ""
        out = f"{self.t.work}/out/{hashlib.sha1(os.path.normcase(out_local).encode()).hexdigest()[:12]}"
        plan = {"models": {}, "uploads": {}, "out": out}

        def walk(v, key=None):
            if key == "output_dir":
                return out
            if key == "cancel_file":
                return self._flag()
            if key == "sdcpp":
                return ""  # a Windows program - GGUF models need their own build on the pod
            if isinstance(v, dict):
                return {k: walk(x, k) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, str) and _LOCAL_PATH.match(v) and os.path.exists(v):
                return pod_path(self.t, v, plan)
            return v

        return walk(job), plan

    def _models_there(self, job_id, models):
        if not models:
            return
        missing = self._ask({"cmd": "have", "paths": list(models)}, "have")["missing"]
        for path in missing:
            folder, rev = models[path]
            self.status.emit(job_id, f"Downloading {folder.split('--', 1)[-1].replace('--', '/')} on the pod…")
            if not download_there(self.t, folder, rev):
                self._upload(job_id, local_snapshot(self.t, folder, rev))  # e.g. gated: from this PC's store

    def _upload(self, job_id, uploads):
        todo = {r: l for r, l in uploads.items() if r not in self._uploaded}
        if todo:
            missing = set(self._ask({"cmd": "have", "paths": list(todo)}, "have")["missing"])
            total = sum(os.path.getsize(todo[r]) for r in missing)
            if missing:
                self.status.emit(job_id, f"Uploading {len(missing)} file(s), {total / 1e6:.1f} MB…")
            for remote in sorted(missing):
                with open(todo[remote], "rb") as f:
                    first = True
                    while True:
                        data = f.read(CHUNK)
                        last = len(data) < CHUNK
                        if data or first:
                            self._ask({"cmd": "put", "path": remote, "append": not first, "last": last,
                                       "data": base64.b64encode(data).decode("ascii")}, "put")
                        first = False
                        if last:
                            break
            self._uploaded.update(todo)

    def _fetch(self, msg, out, out_local):
        """Results written on the pod → the job's output folder here; the message names the local files."""
        got = {}

        def bring(v, key=None):
            if isinstance(v, dict):
                return {k: bring(x, k) for k, x in v.items()}
            if isinstance(v, list):
                return [bring(x) for x in v]
            if isinstance(v, str) and (v.startswith(out + "/") or (key == "path" and v.startswith("/"))):
                if v not in got:
                    data = base64.b64decode(self._ask({"cmd": "get", "path": v}, "file")["data"])
                    os.makedirs(out_local, exist_ok=True)
                    local = os.path.join(out_local, os.path.basename(v))
                    with open(local, "wb") as f:
                        f.write(data)
                    got[v] = local
                return got[v]
            return v

        return bring({k: v for k, v in msg.items() if k not in ("id", "event")})
