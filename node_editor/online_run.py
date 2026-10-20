"""A Modality in normal mode with an online model: one plain request - the prompt (and pictures) in, the answer out,
the way a local language model answers. (Executor mode is the same model building a graph - executor.py.)

Claude Code: `claude -p` with no tools, its own work folder, API keys kept out of its environment (online.py), the
message as stream-json so pictures can go with it. The answer is saved like a local model's: <name>_<stamp>.txt in
the Modality's output folder.
"""

import base64
import json
import os
import time

from PySide6.QtCore import QObject, QProcess

from . import online

WORK = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "NodeEditor", "online")
DEFAULT_SYSTEM = "You are a helpful assistant."  # an online model file's "system" setting replaces it
PICTURE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
                 ".gif": "image/gif"}


def message_content(job):
    """The user message as content blocks: one text as it is; several inputs as blocks named after their nodes
    (<prompt1>…</prompt1>, a picture between <photo1> and </photo1>) - the same layout a local model gets.
    Returns (blocks, names of inputs that can't be sent)."""
    named = job.get("named") or {}
    texts = named.get("text") or ([["text", job["prompt"]]] if job.get("prompt") else [])
    pictures = named.get("image") or []
    unused = [n for kind in ("video", "audio") for n, _v in named.get(kind) or []]
    unused += [n for n, _v, _k in job.get("files") or []]
    blocks = []
    for name, path in pictures:
        mime = PICTURE_TYPES.get(os.path.splitext(path)[1].lower())
        if mime is None or not os.path.isfile(path):
            unused.append(name)
            continue
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        blocks += [{"type": "text", "text": f"<{name}>"},
                   {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}},
                   {"type": "text", "text": f"</{name}>"}]
    if len(texts) == 1 and not blocks:
        return [{"type": "text", "text": texts[0][1]}], unused
    blocks += [{"type": "text", "text": f"<{name}>\n{text}\n</{name}>"} for name, text in texts]
    return blocks, unused


class OnlineRun(QObject):
    """One request to an online model for a Modality (node): node._on_status / node._on_end report as for the engine."""

    def __init__(self, node, model_node, job):
        super().__init__()
        self.node, self.model_node, self.job = node, model_node, job
        self.proc, self.buf, self.answer, self.error, self.t0 = None, b"", None, "", time.time()
        self.stopped = False

    def start(self):
        provider, model, settings, _entry = online.resolve(self.model_node)
        if provider != online.CLAUDE_CODE:
            return self._end(f"Failed: the app can't run {provider} yet")
        exe = online.claude_exe()
        if not exe:
            return self._end("Failed: Claude Code isn't installed (no `claude` on PATH)")
        blocks, self.unused = message_content(self.job)
        if all(b["type"] == "text" and not b["text"].strip() for b in blocks):
            return self._end("Failed: nothing to answer - connect a prompt")
        os.makedirs(WORK, exist_ok=True)
        args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--tools", "", "--strict-mcp-config", "--setting-sources", "local", "--permission-mode", "dontAsk",
                "--system-prompt", settings.get("system") or DEFAULT_SYSTEM]
        if model:
            args += ["--model", model]
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(online.child_env())
        self.proc.setWorkingDirectory(WORK)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.proc.start(exe, args)
        if not self.proc.waitForStarted(10000):
            return self._end("Failed: Claude Code didn't start")
        msg = {"type": "user", "message": {"role": "user", "content": blocks}}
        self.proc.write((json.dumps(msg) + "\n").encode("utf-8"))
        self.proc.closeWriteChannel()  # one message: it answers, then ends
        self.node._on_status(f"asking {self.model_node.subtitle()}…")

    def stop(self):
        self.stopped = True
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()

    def _read(self):
        self.buf += bytes(self.proc.readAllStandardOutput())
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                e = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if e.get("type") == "result":
                if e.get("is_error"):
                    why = str(e.get("result") or e.get("subtype") or "an error")
                    if "authenticat" in why.lower() or "login" in why.lower():
                        why += " - in a terminal: claude auth login"
                    self.error = why
                else:
                    self.answer = str(e.get("result") or "")

    def _finished(self, *_):
        self._read()
        if self.stopped:
            return self._end("Stopped")
        if self.answer is None:
            return self._end("Failed: " + (self.error or "Claude Code ended without an answer"))
        os.makedirs(self.job["output_dir"], exist_ok=True)
        path = os.path.join(self.job["output_dir"], f"{self.job['name']}_{time.strftime('%Y%m%d_%H%M%S')}.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.answer)
        seconds = round(time.time() - self.t0, 1)
        note = f" · Note: not sent (this model takes text and pictures): {', '.join(self.unused)}" if self.unused else ""
        self._end(f"Done in {seconds:.0f} s{note}", {"kind": "text", "text": self.answer, "path": path,
                                                   "seconds": seconds})

    def _end(self, state, result=None):
        node, self.node = self.node, None
        if node is not None:
            node._online = None
            node._on_end(state, result)
