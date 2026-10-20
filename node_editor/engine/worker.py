"""Engine worker process: runs AI models away from the UI.

Protocol (one JSON object per line):
  stdin  <- {"cmd": "run", "id": ..., "job": {...}} | {"cmd": "quit"}
  stdout -> {"event": "ready"} | {"id", "event": "status"|"progress"|"done"|"error"|"cancelled", ...}
Anything libraries print goes to stderr, so stdout carries only protocol messages.

stdin is only read while idle, from the main thread: on Windows a thread blocked reading
a pipe can stall other code touching the same handle (seen as a hang while importing torch).
So "stop" is signalled with a flag file (job["cancel_file"]) instead of a stdin message.
"""

import json
import os
import sys
import traceback

_out = sys.stdout
sys.stdout = sys.stderr  # keep stray prints off the protocol channel
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def emit(**msg):
    _out.write(json.dumps(msg) + "\n")
    _out.flush()


class CancelFlag:
    def __init__(self, path):
        self.path = path

    def is_set(self):
        return bool(self.path) and os.path.exists(self.path)

    def clear(self):
        if self.is_set():
            os.remove(self.path)


class gpu_turn:
    """A GPU shared by several apps (env TAUUI_GPU_LOCK = a lock file, set by engine/remote.py): one job at a time
    on it - the others wait for their turn - and the GPU memory is given back after each job."""

    def __init__(self, send):
        self.send, self.f = send, None

    def __enter__(self):
        path = os.environ.get("TAUUI_GPU_LOCK")
        if path:
            import fcntl
            self.f = open(path, "a")
            try:
                fcntl.flock(self.f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.send(event="status", message="Waiting for the GPU (another app's job is running on it)…")
                fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if self.f is not None:
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            self.f.close()  # releases the lock
        return False


def files(msg):
    """have: which of these paths are missing here · put: write a file (base64) · get: send a file back."""
    import base64
    cmd, rid = msg["cmd"], msg.get("id")
    try:
        if cmd == "have":
            missing = []
            for p in msg["paths"]:
                if os.path.exists(p):
                    try:
                        os.utime(p)  # in use: the pod's cleaner deletes only files unused for hours
                    except OSError:
                        pass
                else:
                    missing.append(p)
            emit(id=rid, event="have", missing=missing)
        elif cmd == "put":
            os.makedirs(os.path.dirname(msg["path"]), exist_ok=True)
            tmp = msg["path"] + ".part"
            with open(tmp, "ab" if msg.get("append") else "wb") as f:  # big files come in pieces
                f.write(base64.b64decode(msg["data"]))
            if msg.get("last", True):
                os.replace(tmp, msg["path"])
            emit(id=rid, event="put", path=msg["path"])
        else:
            with open(msg["path"], "rb") as f:
                emit(id=rid, event="file", path=msg["path"], data=base64.b64encode(f.read()).decode("ascii"))
    except OSError as e:
        emit(id=rid, event="file_error", path=msg.get("path", ""), message=str(e))


def main():
    # Nothing heavy is imported until a job needs it, so the worker starts in any Python environment;
    # if the environment lacks a library, the job reports the real import error.
    emit(event="ready", python=sys.version.split()[0], executable=sys.executable)
    recipes = None
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("cmd") == "quit":
            break
        if msg.get("cmd") in ("have", "put", "get"):  # files for a worker on another machine (engine/remote.py)
            files(msg)
            continue
        job_id, job = msg.get("id"), msg["job"]
        cancel = CancelFlag(job.get("cancel_file"))
        send = lambda **m: emit(id=job_id, **m)  # noqa: E731
        try:
            if recipes is None:
                send(event="status", message="Loading libraries…")
                import recipes  # noqa: F811 - torch, diffusers... from THIS environment
            if cancel.is_set():
                raise recipes.Cancelled()
            with gpu_turn(send):
                result = recipes.run(job, send, cancel)
            send(event="done", **result)
        except Exception as e:  # noqa: BLE001 - report every failure to the UI
            if recipes is not None and isinstance(e, recipes.Cancelled):
                send(event="cancelled")
            else:
                traceback.print_exc()
                send(event="error", message=f"{type(e).__name__}: {e}")
        finally:
            cancel.clear()
            if os.environ.get("TAUUI_GPU_LOCK") and _ram_limit_gb() < 100 and recipes is not None:
                # a shared GPU on a pod with little RAM: other apps' models need it between our jobs
                recipes._free()
                try:
                    import runners
                    runners.free()
                except Exception:  # noqa: BLE001
                    pass


def _ram_limit_gb():
    """The memory this process may use: the container's limit (cgroup v2 / v1), else the machine's."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            v = open(path).read().strip()
            if v.isdigit() and int(v) < 1 << 50:
                return int(v) / 1024**3
        except OSError:
            pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except (ValueError, OSError, AttributeError):
        return 1e9


if __name__ == "__main__":
    main()
