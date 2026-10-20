"""A Gemini model (Google AI Studio, svc_google) as the Executor's brain.

The app holds the conversation, as for a local brain (executor.LocalBrainSession): each turn of the model is one
generateContent request with the app's tools as function declarations (their JSON schemas as they are); the tools
run here and their answers go back as function responses - pictures from look_at as real pictures (Gemini sees
them). The model's own turns are kept exactly as Gemini sent them (with their thought signatures, which Gemini 3
needs back), saved beside the transcript (<session>.gemini.json) so a later message resumes the same conversation.
The transcript itself is written in Claude Code's layout, so the Conversation, Log and the rest work unchanged.

What the brain costs is computed from the token counts Gemini returns and the price table in services/google.json;
it is logged (logs/online_spend.jsonl, kind "brain") and shown in the stats - separate from the render Budget.
"""

import json
import os
import time
import uuid

from PySide6.QtCore import QTimer

from . import costs, executor_tools, online, services
from .executor import (DONE, FAILED, PROMPT_FILE, STOPPED, WAIT_ANSWER, WORKING, ExecutorSession,
                       LocalBrainSession, _local_messages, _typed_args)
from .svc_google import API, _h, price as google_price
from .services import http

RETRIES = 4  # a busy service (429 / 503) is asked again after a pause


class GeminiBrainSession(LocalBrainSession):
    MAX_CALLS = 80

    def start(self, first_message=None, resume=""):
        os.makedirs(self.work, exist_ok=True)
        self.brain = self.node.brain()
        _provider, model, settings, entry = online.resolve(self.brain)
        prompt_file = settings.get("instructions") or PROMPT_FILE
        try:
            with open(prompt_file, encoding="utf-8") as f:
                self.system = f.read()
        except OSError as e:
            return self._fail(f"can't read the instructions {prompt_file}: {e}")
        self.service = services.entry(self.brain.params.get("provider"))
        self.key = services.key(self.service) if self.service else ""
        if not self.key:
            return self._fail("no Google key - set GEMINI_API_KEY")
        self.model = model
        self.price = google_price(model, self.service)
        self.decls = [{"name": t["name"], "description": t["description"], "parametersJsonSchema": t["inputSchema"]}
                      for t in executor_tools.listing()]
        self.session_id = resume or f"gemini-{uuid.uuid4()}"
        self.node.params["session_id"] = self.session_id
        self.path = os.path.join(self.work, self.session_id + ".jsonl")
        self.state_path = os.path.join(self.work, self.session_id + ".gemini.json")
        self.contents = self._load(resume)
        self.calls, self.turns, self._wrapping, self._stopped = 0, 0, False, False
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.job = None  # (LocalBrainSession's engine job: none here)
        self.node.add_log(f"{'Continued' if resume else 'Build started'} · brain: {self.brain.name} (Gemini "
                          f"{model} on Google) · no web search (yet) for a Gemini brain")
        self._lock(True)
        self.node.set_phase(WORKING)
        self._user(first_message or "Start with get_task.")

    def _load(self, resume):
        if not resume:
            return []
        try:
            with open(self.state_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):  # no saved Gemini turns: the transcript as plain text turns
            out = []
            for m in _local_messages(self.path):
                text = m.get("content") or ""
                if m.get("tool_calls"):
                    text += "\n" + "\n".join(f"[called {c['function']['name']} "
                                             f"{json.dumps(c['function']['arguments'], ensure_ascii=False)[:300]}]"
                                             for c in m["tool_calls"])
                role = "model" if m["role"] == "assistant" else "user"
                if m["role"] == "tool":
                    text = "[tool answer] " + text[:2000]
                if text.strip():
                    out.append({"role": role, "parts": [{"text": text}]})
            return out

    def _save_turns(self):
        """The conversation for a later resume - pictures left out (they are large; their captions stay)."""
        def light(c):
            parts = [p if "inlineData" not in p else {"text": "[a picture was shown here]"} for p in c.get("parts", [])]
            return {**c, "parts": parts}
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump([light(c) for c in self.contents], f, ensure_ascii=False)
        except OSError:
            pass

    def stop(self, why):
        self._stopped = True
        for n in self.created:
            if n.scene() is not None and getattr(n, "running", False) and hasattr(n, "action"):
                n.action("stop")
        self._close(STOPPED, why)

    def _close(self, phase, why):
        self._save_turns()
        ExecutorSession._close(self, phase, why)

    def user_says(self, text, news=""):
        if self.node.phase != WAIT_ANSWER:
            return
        self.node.add_log("You: " + text)
        self._lock(True)
        self.node.set_phase(WORKING)
        self._user(f"{news}The user writes:\n{text}" if news else text)

    # ---- the conversation ------------------------------------------------------------------------------------
    def _user(self, text):
        self.contents.append({"role": "user", "parts": [{"text": text}]})
        self._write("user", [{"type": "text", "text": text}])
        self.calls = 0
        self._ask()

    def _ask(self, tools=True, attempt=0):
        level = self.brain.params.get("in:thinking_level") or "high"
        body = {"systemInstruction": {"parts": [{"text": self.system}]}, "contents": self.contents,
                "generationConfig": {"thinkingConfig": {"thinkingLevel": level}}}
        if tools:
            body["tools"] = [{"functionDeclarations": self.decls}]
        t0 = time.time()
        url = f"{API}/models/{self.model}:generateContent"
        services.in_background(lambda: http("POST", url, _h(self.key), body=body, timeout=600),
                               done=lambda d, err: self._answered_http(d, err, time.time() - t0, tools, attempt))

    def _answered_http(self, d, err, seconds, tools, attempt):
        if self._stopped or self.node.phase != WORKING:
            return
        if err is not None:
            status = getattr(err, "status", 0)
            if status in (429, 500, 503) and attempt < RETRIES:
                self.node.add_log(f"(Gemini busy - {err}; asking again in {10 * (attempt + 1)} s)")
                QTimer.singleShot(10000 * (attempt + 1), lambda: self._ask(tools, attempt + 1))
                return
            return self._close(FAILED, f"The brain stopped: Gemini: {err}")
        self.turns += 1
        cand = (d.get("candidates") or [{}])[0]
        content = cand.get("content") or {"role": "model", "parts": []}
        content.setdefault("role", "model")
        usage = d.get("usageMetadata") or {}
        self._count(usage, seconds)
        self.contents.append(content)
        parts = content.get("parts") or []
        text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought")).strip()
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if self._wrapping:
            self._wrapping, calls = False, []
        reason = cand.get("finishReason") or ""
        if not parts and reason:
            self.node.add_log(f"(Gemini gave no answer: {reason})")
        if reason == "MAX_TOKENS":
            self.node.add_log("(the brain's answer was cut at its output limit)")
        calls = [{"name": c["name"], "arguments": c.get("args") or {}, "gemini_id": c.get("id"),
                  "id": f"call_{uuid.uuid4().hex[:12]}"} for c in calls]
        blocks = [{"type": "text", "text": text}] if text else []
        blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]} for c in calls]
        self._write("assistant", blocks, id=f"msg_{uuid.uuid4().hex[:16]}")
        if text:
            self.node.add_log(f"{self.brain.name}: {text}")
        if not calls:
            self._save_turns()
            return self._end_turn()
        self._pending, self._responses = calls, []
        self._next_call()

    def _count(self, usage, seconds):
        """Tokens and what they cost (cached input at a tenth, as Google bills its context cache)."""
        cached = usage.get("cachedContentTokenCount", 0)
        prompt = usage.get("promptTokenCount", 0)
        out = usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)
        self.tokens_in += prompt
        self.tokens_out += out
        turn = 0.0
        if self.price:
            est = costs.estimate(self.price, {}, {"input_tokens": prompt - cached * 0.9, "output_tokens": out})
            turn = est["high"] if est and est["high"] is not None else 0.0
        self.cost += turn
        if turn:
            costs.record(service=self.service["name"], model=self.model, node=self.node.name, kind="brain",
                         cost=round(turn, 6), outcome="turn",
                         metrics={"input_tokens": prompt, "cached": cached, "output_tokens": out})
        self.stats = (f"{self.turns} turns · {seconds:.0f} s this turn ({prompt:,} tokens in, {out:,} out) · "
                      f"{time.time() - self.t0:.0f} s in all · brain ${self.cost:.3f}")
        self.node.stats = self.stats

    def _next_call(self):
        if self._stopped or self.node.phase != WORKING:
            return
        if not self._pending:
            self.contents.append({"role": "user", "parts": self._responses})
            self._save_turns()
            return self._ask()
        self.calls += 1
        if self.calls > self.MAX_CALLS:
            self.node.add_log(f"The brain made {self.MAX_CALLS} tool calls in this turn - asked it to stop and say "
                              "where it is")
            for call in self._pending:
                self._responses.append(self._response(call, "(not run - the call limit of this turn)", True))
            self._pending = []
            self._responses.append({"text": (
                f"(The app: that was {self.MAX_CALLS} tool calls in this turn - stop here. Tell the user in a few "
                "lines what you have done, what works, what doesn't, and what you would try next.)")})
            self.contents.append({"role": "user", "parts": self._responses})
            self._wrapping = True
            return self._ask(tools=False)
        call = self._pending.pop(0)
        args = _typed_args(call["name"], call["arguments"])
        shown = json.dumps(args, ensure_ascii=False)
        self.node.add_log(f"→ {call['name']} {shown[:160] + '…' if len(shown) > 160 else shown}")
        content, is_error = executor_tools.call(self, call["name"], args)
        if isinstance(content, executor_tools.Later):
            later, t0 = content, time.time()
            timer = QTimer(self)

            def poll():
                out = later.check()
                if out is None and time.time() - t0 < later.timeout:
                    return
                timer.stop()
                timer.deleteLater()
                if out is None:
                    self._call_done(call, f"no result after {later.timeout // 60} minutes - still running", True)
                else:
                    self._call_done(call, out if isinstance(out, str) else json.dumps(out, ensure_ascii=False,
                                                                                      indent=1), False)
            timer.timeout.connect(poll)
            timer.start(1000)
            return
        self._call_done(call, content, is_error)

    def _response(self, call, text, is_error, images=()):
        fr = {"name": call["name"], "response": {"error" if is_error else "result": text}}
        if call.get("gemini_id"):
            fr["id"] = call["gemini_id"]
        return {"functionResponse": fr}

    def _call_done(self, call, content, is_error):
        images = []
        if isinstance(content, list):  # MCP-style blocks: the text, and pictures Gemini can see
            images = [{"inlineData": {"mimeType": b.get("mimeType", "image/jpeg"), "data": b["data"]}}
                      for b in content if b.get("type") == "image" and b.get("data")]
            text = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
        else:
            text = str(content)
        if is_error:
            self.node.add_log("  ✗ " + text[:240])
        self._responses.append(self._response(call, text, is_error))
        self._responses += images  # the picture follows its answer in the same message
        self._write("user", [{"type": "tool_result", "tool_use_id": call["id"], "content": text,
                              "is_error": bool(is_error)}])
        QTimer.singleShot(0, self._next_call)

    def _end_turn(self):
        self._end_macros()
        if self.finished is not None:
            self._close(DONE, "Build finished: " + self.finished)
            return
        self._lock(False)
        self.node.set_phase(WAIT_ANSWER)
        self._save_state()
