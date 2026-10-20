"""A Modality in normal mode with a SERVICE model (Replicate, ...): one paid job on the service.

What goes where follows one rule for every model, read from the model's own inputs (its schema):
- the Modality's prompt → the model's text input (online.text_field: "prompt", else its first required text);
- each data item → a file input: a node NAMED like an input goes there (image1 = image); the others of a kind go to
  that kind's main input (named after the kind, else the one taking several, else the first) - several items into
  an input that takes one: the first, the rest are named as not sent;
- the Model node's own settings → the inputs they are named after (only those set away from the service's default).

Files are uploaded, the job is started, polled every POLL seconds, and its files are downloaded into the Modality's
output folder at once (a service may delete them soon). The cost: estimated before (costs.estimate), recorded after
from the service's measured units (logs/online_spend.jsonl).

Pacing: at most `max_running` jobs (the service file's settings, default MAX_RUNNING) run on one service at a time;
the others wait here, not sent yet - stopping one of those costs nothing. A job the service dropped before it
started, saying to retry (its own record: nothing measured but the wait), is sent again - up to RETRIES times, a while later.
"""

import os
import re
import threading
import time

from PySide6.QtCore import QObject

from . import costs, online, services

KIND_OF_EXT = {**{e: "image" for e in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")},
               **{e: "video" for e in (".mp4", ".webm", ".mov", ".mkv", ".avi")},
               **{e: "audio" for e in (".wav", ".mp3", ".flac", ".ogg", ".m4a")},
               **{e: "geometry" for e in (".glb", ".gltf", ".obj", ".fbx", ".ply", ".stl", ".usdz", ".dae")},
               ".json": "data", ".txt": "text"}


MAX_RUNNING = 3   # jobs at a time on one service, unless its file says otherwise (settings.max_running)
RETRIES = 2       # sending again a job the service dropped before it started
RETRY_WAIT = 30   # seconds before the first new try (twice that before the second)
_running = {}     # service -> the runs it has now
_queued = {}      # service -> runs waiting for a free place, in order


def _limit(service):
    try:
        return max(1, int((service.get("settings") or {}).get("max_running") or MAX_RUNNING))
    except (TypeError, ValueError):
        return MAX_RUNNING


def _never_started(pred):
    """The service dropped the job without doing the work and says to retry - by its own record: it measures what
    a run used (metrics), and nothing was measured but the time waited (Replicate's "Prediction interrupted; please
    retry (code: PA)": total_time only, no predict_time / output seconds). Sending it again costs nothing extra."""
    metrics = pred.get("metrics")
    return (isinstance(metrics, dict) and not set(metrics) - {"total_time"}
            and "retry" in str(pred.get("error") or "").lower())


def _base(name):
    return re.sub(r"\d+$", "", name.lower()).rstrip("_").rstrip("s")


def route(fields, items):
    """{input: [(item name, path)]}, [names not sent], [missing required inputs] - the data items (name, path,
    kind) on the model's file inputs."""
    files = [f for f in fields if f["type"] == "uri"]
    plan = {f["name"]: [] for f in files}
    left = []
    by_name = {f["name"].lower(): f for f in files}
    by_base = {_base(f["name"]): f for f in files}
    for name, path, kind in items:  # a node named like an input goes there
        f = by_name.get(name.lower()) or by_base.get(_base(name))
        if f is not None and (f["multi"] or not plan[f["name"]]):
            plan[f["name"]].append((name, path))
        else:
            left.append((name, path, kind))
    unused = []
    for kind in ("image", "video", "audio", "geometry", "data", "json", "file", "text"):
        mine = [(n, p) for n, p, k in left if k == kind]
        if not mine:
            continue
        cands = [f for f in files if f["media"] == kind] or [f for f in files if f["media"] == "file"]
        main = (next((f for f in cands if _base(f["name"]) == kind), None)
                or next((f for f in cands if f["multi"]), None)
                or next((f for f in cands if f["required"] and not plan[f["name"]]), None)
                or next((f for f in cands if not plan[f["name"]]), None))
        if main is None:
            unused += [n for n, _p in mine]
            continue
        if main["multi"]:
            plan[main["name"]] += mine
        else:
            if not plan[main["name"]]:
                plan[main["name"]].append(mine[0])
                mine = mine[1:]
            unused += [n for n, _p in mine]
    missing = [f"{f['name']} ({f['media']})" for f in files if f["required"] and not plan[f["name"]]]
    return {k: v for k, v in plan.items() if v}, unused, missing


class ServiceRun(QObject):
    """One job on a service for a Modality (node): node._on_status / node._on_end report as for the engine."""

    def __init__(self, node, model_node, job):
        super().__init__()
        self.node, self.model_node, self.job = node, model_node, job
        self.stopped = False
        self.t0 = time.time()
        self.prediction = None

    def plan(self):
        """(service entry, model details, inputs without files, file plan, unused, problem) for this run."""
        e = self.model_node.service_entry()
        s = services.entry(self.model_node.params.get("provider"))
        d = online.info(e)
        if e is None or s is None:
            return None, None, None, None, None, "no online model chosen"
        if d is None:
            online.refresh_info(e)
            return s, None, None, None, None, "the model's details haven't arrived yet - try again in a moment"
        fields = d.get("fields") or []
        inputs = dict(self.model_node.service_inputs())
        t = online.text_field(fields)
        if t and self.job.get("prompt"):
            inputs[t] = self.job["prompt"]
        items = [(n, v, k) for n, v, k in self.node.data() if isinstance(v, str) and v]
        plan, unused, missing = route(fields, items)
        req_text = next((f for f in fields if f["name"] == t and f["required"]), None)
        if req_text and not inputs.get(t):
            missing.insert(0, f"{t} (text: connect a prompt)")
        if missing:
            return s, d, inputs, plan, unused, ("needs " + ", ".join(missing)
                                                + " - connect it to the Modality (a node named like the input "
                                                  "goes there)")
        return s, d, inputs, plan, unused, ""

    def estimate(self):
        s, d, inputs, plan, unused, problem = self.plan()
        if d is None or not d.get("price"):
            return None
        values = {**self.model_node.service_values(), **(inputs or {})}
        est = costs.estimate(d["price"], values)
        if est is not None and est["high"] is None:  # billed by tokens: the count is only known after the run
            est = costs.token_bound(d, values, bool(plan)) or est
        return est

    def start(self):
        s, d, inputs, plan, unused, problem = self.plan()
        if problem:
            return self._end("Failed: " + problem)
        token = services.key(s)
        if not token:
            return self._end(f"Failed: no key - set the Windows user variable {s['key_env']}")
        mod = services.protocol_module(s["protocol"])
        if mod is None or not hasattr(mod, "create"):
            return self._end(f"Failed: the app can't run jobs on {s['label']} yet")
        self.service, self.details, self.inputs, self.unused = s, d, inputs, unused
        self.routing = " · ".join(f"{k} ← {', '.join(n for n, _p in v)}" for k, v in plan.items())
        values = {**self.model_node.service_values(), **inputs}
        self.estimate_before = costs.estimate(d.get("price"), values) if d.get("price") else None
        self._go = (mod, token, d, dict(inputs), plan)
        self.retries = 0
        running = _running.setdefault(s["name"], set())
        if len(running) < _limit(s):
            return self._launch()
        _queued.setdefault(s["name"], []).append(self)
        self.node._on_status(f"waiting: {len(running)} jobs run on {s['label']} (at most {_limit(s)} at a time) - "
                             f"not sent yet")

    def _launch(self):
        _running.setdefault(self.service["name"], set()).add(self)
        self.node._on_status(f"starting on {self.service['label']}…")
        self.t0 = time.time()
        threading.Thread(target=self._work, args=self._go, daemon=True).start()

    def stop(self):
        self.stopped = True
        waiting = _queued.get(getattr(self, "service", {}).get("name"), [])
        if self in waiting:  # never sent: nothing to cancel, nothing paid
            waiting.remove(self)
            self._end("Stopped before it was sent - nothing paid")

    def _release(self):
        """This run ends: its place on the service goes to the next one waiting."""
        svc = getattr(self, "service", None)
        if not svc:
            return
        _running.get(svc["name"], set()).discard(self)
        waiting = _queued.get(svc["name"], [])
        while waiting and len(_running.get(svc["name"], ())) < _limit(svc):
            nxt = waiting.pop(0)
            if nxt.node is not None and not nxt.stopped:
                nxt._launch()

    # ---- the worker thread -----------------------------------------------------------------------------------
    def _status(self, text):
        services.to_qt(lambda: self.node is not None and self.node._on_status(text))

    def _work(self, mod, token, d, inputs, plan):
        try:
            for field, items in plan.items():
                urls = []
                for name, path in items:
                    if self.stopped:
                        return services.to_qt(self._end, "Stopped")
                    self._status(f"uploading {name}…")
                    urls.append(mod.upload(token, path))
                f = next(x for x in d["fields"] if x["name"] == field)
                inputs[field] = urls if f["multi"] else urls[0]
            self._status("starting the job…")
            self._sent = inputs  # (uploaded) - a retry sends the same
            pred = mod.create(token, d, inputs)
            self.prediction = pred
            services.to_qt(self._remember, pred)
            self._follow(mod, token, pred)
        except Exception as err:  # every failure ends the run with the service's own words
            services.to_qt(self._end, f"Failed: {err}", None, self.prediction)

    def _follow(self, mod, token, pred):
        """Poll a started job until it ends, then download what it made (worker thread)."""
        try:
            last = ""
            while pred.get("status") not in ("succeeded", "failed", "canceled"):
                if self.stopped:
                    mod.cancel(token, pred)
                    return services.to_qt(self._end, "Stopped (cancelled on the service - a started run may "
                                                      "still be billed)")
                time.sleep(getattr(mod, "POLL_SECONDS", 2.0))
                pred = mod.get(token, pred)
                logs = (pred.get("logs") or "").strip().splitlines()
                line = logs[-1][:120] if logs else ""
                elapsed = int(time.time() - self.t0)
                text = f"{pred.get('status', '…')} · {elapsed} s" + (f" · {line}" if line else "")
                if text != last:
                    self._status(text)
                    last = text
            if pred.get("status") != "succeeded":
                why = pred.get("error") or pred.get("status")
                if hasattr(self, "_go") and _never_started(pred) and self.retries < RETRIES and not self.stopped:
                    return self._retry(mod, token, pred, why)
                return services.to_qt(self._end, f"Failed on {self.service['label']}: {why}", None, pred)
            urls, text = mod.outputs(pred)
            out_dir = self.job["output_dir"]
            os.makedirs(out_dir, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            saved = []
            for i, url in enumerate(urls):
                ext, suffix = mod.file_name(url, i, len(urls))
                path = os.path.join(out_dir, f"{self.job['name']}_{stamp}{suffix}{ext}")
                self._status(f"downloading {i + 1}/{len(urls)}…")
                fetch = getattr(mod, "fetch", None)  # a service whose files need its key (Google)
                saved.append(fetch(token, url, path) if fetch else services.download(url, path))
            if text and not saved:
                path = os.path.join(out_dir, f"{self.job['name']}_{stamp}.txt")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                saved.append(path)
            services.to_qt(self._done, pred, saved, text)
        except Exception as err:  # every failure ends the run with the service's own words
            services.to_qt(self._end, f"Failed: {err}", None, self.prediction)

    def _retry(self, mod, token, pred, why):
        """(Worker thread.) The service dropped the job before it ran and says to retry: log it ($0), wait, send it
        again with the same inputs (already uploaded)."""
        services.to_qt(self._record, pred, pred.get("status") or "failed")
        self.retries += 1
        wait = RETRY_WAIT * self.retries
        for left in range(wait, 0, -1):
            if self.stopped:
                return services.to_qt(self._end, "Stopped")
            if left % 5 == 0 or left == wait:
                self._status(f"{self.service['label']} dropped it before it started ({str(why)[:80]}) - sending "
                             f"again in {left} s ({self.retries} of {RETRIES})")
            time.sleep(1)
        try:
            self._status("starting the job again…")
            pred = mod.create(token, self._go[2], self._sent)
            self.prediction = pred
            services.to_qt(self._remember, pred)
            self._follow(mod, token, pred)
        except Exception as err:
            services.to_qt(self._end, f"Failed: {err}", None, self.prediction)

    # ---- a job started before the app closed ------------------------------------------------------------------
    def resume(self, job):
        """Follow a job this node started before (its id kept in the graph): the result is fetched if the service
        still has it. Nothing is started or paid again."""
        s = services.entry(job.get("service") or "")
        mod = services.protocol_module(s["protocol"]) if s else None
        token = services.key(s) if s else ""
        if mod is None or not token:
            return self._end(f"Not resumed: service {job.get('service')!r} isn't set up (or has no key)")
        self.service, self.inputs, self.routing, self.unused = s, {}, "", []
        self.job["ordered_by"] = job.get("ordered_by") or self.job.get("ordered_by")  # its cost counts for that Executor
        e = self.model_node.service_entry() if self.model_node is not None else None
        self.details = online.info(e) or {"id": job.get("model")}
        self.estimate_before = None
        _running.setdefault(s["name"], set()).add(self)  # it holds a place on the service like any running job
        pred = dict(job.get("pred") or {"id": job.get("id"), "urls": {"get": job.get("get")}})
        pred.setdefault("status", "processing")
        self.prediction = pred
        self.node._on_status(f"picking up the job started {job.get('started', '')} on {s['label']}…")
        threading.Thread(target=self._follow, args=(mod, token, pred), daemon=True).start()

    # ---- back on the Qt thread -------------------------------------------------------------------------------
    def _remember(self, pred):
        """The job's id, kept in the node (a crash can then find it again): enough of it to ask the service later."""
        if self.node is not None:
            keep = {k: pred[k] for k in ("id", "urls", "operation", "duration") if k in pred}
            self.node.params["online_job"] = {"service": self.service["name"], "model": self.details.get("id"),
                                              "id": pred.get("id"), "get": (pred.get("urls") or {}).get("get"),
                                              "pred": keep, "started": time.strftime("%Y-%m-%d %H:%M:%S"),
                                              "ordered_by": self.job.get("ordered_by") or ""}

    def _done(self, pred, saved, text):
        if not saved:
            return self._end("Failed: the service returned nothing", None, pred)
        # several files (e.g. a 3D model + preview videos): the richest kind is the result, the others are listed
        rank = ("geometry", "video", "audio", "image", "data", "text")
        kinds = [KIND_OF_EXT.get(os.path.splitext(p)[1].lower(), "file") for p in saved]
        best = min(range(len(saved)), key=lambda i: rank.index(kinds[i]) if kinds[i] in rank else len(rank))
        first, kind = saved[best], kinds[best] if kinds[best] in rank else "data"
        seconds = round(time.time() - self.t0, 1)
        result = {"kind": kind, "path": first, "seconds": seconds}
        if kind == "text":
            with open(first, encoding="utf-8", errors="replace") as fh:
                result["text"] = fh.read()
        notes = []
        if self.routing:
            notes.append("Inputs: " + self.routing)
        if self.unused:
            notes.append("Not sent (no input for them): " + ", ".join(self.unused))
        if len(saved) > 1:
            notes.append(f"{len(saved)} files: " + ", ".join(os.path.basename(p) for p in saved))
        cost = self._record(pred, "succeeded")
        if cost:
            notes.append("Cost " + cost)
        self._end(f"Done in {seconds:.0f} s" + "".join(f" · {n}" for n in notes), result, None)

    def _record(self, pred, outcome):
        """Write the run to the spend log; returns the cost text (or "")."""
        if pred is None:
            return ""
        price = self.details.get("price") if getattr(self, "details", None) else None
        metrics = pred.get("metrics") or {}
        values = {**(self.model_node.service_values() if self.model_node is not None else {}), **self.inputs}
        after = costs.estimate(price, values, metrics) if price and outcome == "succeeded" else None
        cost = after["high"] if after else None  # a range (tier not certain): the upper end, to be safe
        costs.record(service=self.service["name"], model=self.details.get("id"), node=self.job.get("name"),
                     id=pred.get("id"), outcome=outcome, cost=cost if cost is not None else 0.0,
                     estimate=self.estimate_before["text"] if self.estimate_before else "",
                     after=after["text"] if after else "", metrics=metrics,
                     **({"executor": self.job["ordered_by"]} if self.job.get("ordered_by") else {}))
        return after["text"] if after else ""

    def _end(self, state, result=None, pred=None):
        if pred is not None and result is None:
            self._record(pred, pred.get("status") or "failed")  # failed runs are free on Replicate - logged at $0
        self._release()
        node, self.node = self.node, None
        if node is not None:
            node._online = None
            node.params["online_job"] = None
            node._on_end(state, result)
