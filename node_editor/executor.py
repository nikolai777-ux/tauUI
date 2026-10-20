"""Executor mode of the Modality node: a brain (Claude Code on the Max plan) builds a graph that does what the
prompts ask, tests it, and runs it.

The Modality's Executor tick turns it on; the connected model is then the brain (a Model node running on Claude
Code). Inputs: prompt(s), pictures, videos, sounds, data (shots / chunks) - node names say who / what each one is.
The node's own Prompt box is the conversation: Enter sends (Shift+Enter: new line). The first message starts the
build - your words come first, before the connected prompt; later ones are your replies (an ordinary conversation:
whatever the brain proposes or asks is in its own words, and it reads yours in context - no buttons, no fixed
phrases), also after the build (the same Claude Code session is resumed: the brain gets its nodes back as its own
and is told what happened since, e.g. what its renders made). The pane has one Stop (the brain, and any run of what
it built); rendering what was built and freeing memory are things you ask the brain for (its render_built /
free_memory tools: by stage, what already has its result kept unless Overwrite results is ticked). The reset icon
starts a new conversation (the built nodes stay).
The brain reads the task and the inputs, looks at the pictures, reads the library and the node types
(executor_tools.py), talks with you as it sees fit (prompts/executor.txt: propose before building), builds (one undo
step per stretch of building), tests and finishes; the app checks what it built. While the brain works, the graph
is locked (pan / zoom only); when it ends its turn the graph is free - e.g. to draw on a picture before answering.

The brain is a Model node running online (Claude Code) or a language model running here (LocalBrainSession: the
app runs the conversation and the same tools itself). Other models wired in with it (e.g. a LoRA of the local base)
are the brain's to use: it wires them into the nodes it builds.

Claude Code runs headless: `claude -p` with stream-json in and out, only this app's tools (MCP server
mcp_server.py, bridged over a local TCP port with a token), no built-in tools, in a work folder of its own, with
API keys kept out of its environment (online.py) so it runs on the signed-in account. Its instructions are
prompts/executor.txt, as written.
"""

import json
import os
import secrets
import sys
import time
import uuid

from PySide6.QtCore import QObject, QProcess, QTimer
from PySide6.QtNetwork import QHostAddress, QTcpServer

from . import executor_tools, online
from . import model_info as mi

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROMPT_FILE = os.path.join(PROJECT_ROOT, "prompts", "executor.txt")
WORK_ROOT = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "NodeEditor", "executor")
NO_SESSION = "-"  # session_id after a reset: no conversation to resume (not even the one in the state file)

IDLE, WORKING, WAIT_ANSWER, DONE, STOPPED, FAILED = "idle", "working", "your turn", "done", "stopped", "failed"
PHASE_TEXT = {IDLE: "Ready", WORKING: "working…", WAIT_ANSWER: "your turn - write in Prompt", DONE: "built",
              STOPPED: "stopped", FAILED: "failed"}
BUSY = (WORKING, WAIT_ANSWER)  # the brain's process is there (a run of what it built is `rendering`, beside it)
HANDOVER_HINT_K = 500  # thousand tokens of context: from here tool answers suggest a handover (user, 2026-10-04:
                       # fewer, later handovers at stage ends - a fresh session hasn't seen the pictures or the method)
BUDGET_PREFIX = "budget:"  # a param per service: what the brain may spend there without asking ("budget:replicate")


def _waiting(n):
    return n.phase == WAIT_ANSWER


def _can_continue(n):
    """The build has ended and its Claude Code session can be resumed."""
    return n.phase in (IDLE, DONE, STOPPED, FAILED) and bool(n.resume_id())


class ExecutorMode:
    """What a Modality does in Executor mode (mixed into ModalityNode). Uses the node's params: executor, web,
    overwrite, ask (the Prompt box), built, session_id, log."""

    def _init_executor(self):
        self.phase = IDLE
        self.session = None
        self.stats = ""
        self.news = []  # what happened since the brain's last turn (what its renders made) - told with your next words
        self._run = None  # a run of what was built: {node: its last reported state} while it goes on
        self._run_wait = False  # the brain waits for this run's outcome in its tool call (then it isn't news)
        self._ordered = {}  # paid renders the brain ordered: {node: (service, estimated cost)} - counted while they run
        self._budget_params()

    # ---- money: a budget per service -------------------------------------------------------------------------
    def _budget_params(self):
        """A Budget field per service set up (after Stop): what the brain may spend there without asking."""
        from . import services
        from .node import Param
        wanted = [e for e in services.entries() if e["protocol"] != services.CLAUDE_CODE]
        if [BUDGET_PREFIX + e["name"] for e in wanted] == [p.name for p in self.parameters
                                                            if p.name.startswith(BUDGET_PREFIX)]:
            return
        extra = [Param(BUDGET_PREFIX + e["name"], f"Budget {e['label']} $", "float", 0.0,
                       visible=lambda n: n.executor_on, step=0.5, minimum=0.0, maximum=100.0,
                       hint=f"What the brain may spend on {e['label']}; 0 = it asks") for e in wanted]
        base = list(type(self).parameters)
        at = next(i for i, p in enumerate(base) if p.name == "stop") + 1
        self.parameters = base[:at] + extra + base[at:]
        for p in extra:
            self.params.setdefault(p.name, p.default)

    def accepts_param(self, key):
        return key.startswith(BUDGET_PREFIX) or super().accepts_param(key)

    def budget(self, service):
        return float(self.params.get(BUDGET_PREFIX + service) or 0)

    def spent(self, service):
        """What the paid runs this Executor ordered on a service cost in this conversation (logs/online_spend.jsonl):
        lines with its name - deleted or renamed nodes too - and older lines (without a name) of nodes it built."""
        from . import costs
        names = set(((self.params.get("built") or {}).get("nodes")) or [])
        if self.session is not None:
            names |= {n.name for n in self.session.created}
        since = self.params.get("budget_from") or ""
        return sum(float(r.get("cost") or 0) for r in costs.entries()
                   if r.get("service") == service and r.get("when", "") >= since and r.get("kind") != "brain"
                   and (r["executor"] == self.name if "executor" in r else r.get("node") in names))

    def committed(self, service):
        """What its paid runs still going on a service will cost (their estimates; logged once they end)."""
        for m in [m for m in self._ordered if m.scene() is None or not m.running]:
            del self._ordered[m]
        return sum(cost for svc, cost in self._ordered.values() if svc == service)

    def money_rows(self):
        """"Replicate $3.20 of $40 (2 running ≈ $0.76)" per service with a budget or spending."""
        from . import services
        parts = []
        for e in services.entries():
            if e["protocol"] == services.CLAUDE_CODE:
                continue
            budget, spent, going = self.budget(e["name"]), self.spent(e["name"]), self.committed(e["name"])
            if budget or spent or going:
                n = sum(1 for svc, _c in self._ordered.values() if svc == e["name"])
                parts.append(f"{e['label']} ${spent:.2f} of ${budget:.2f}"
                             + (f" ({n} running ≈ ${going:.2f})" if n else ""))
        return [("Spent", " · ".join(parts))] if parts else []

    @property
    def executor_on(self):
        return bool(self.params.get("executor"))

    def watches_graph(self):
        return self.executor_on  # its rows follow the nodes it built, which aren't wired into it

    # ---- what is plugged in -------------------------------------------------------------------------------
    def connected_models(self):
        return [m for m in self.input_values("model") if m is not None]

    def brain(self):
        """The model that thinks: one running online, else the language model among the connected ones (an
        adapter is never the brain - it is a model the brain may use)."""
        models = self.connected_models()
        return (next((m for m in models if getattr(m, "online", False) and m.brain_kind()), None)
                or next((m for m in models if m.role == mi.LANGUAGE_MODEL), None))

    def other_models(self):
        """The connected models besides the brain: the brain may use them in what it builds."""
        brain = self.brain()
        return [m for m in self.connected_models() if m is not brain]

    def connected_prompt(self):
        return "\n\n".join(t.strip() for t in self.input_values("text") if isinstance(t, str) and t.strip())

    def executor_problems(self):
        if not self.connected_models():
            return ["Connect a brain: a language model, or a Model node running online"]
        brain = self.brain()
        if brain is None:
            return ["The brain must be a language model: one here, Claude Code, or a Gemini text model"]
        if brain.problem():
            return [brain.problem()]
        return []

    @property
    def rendering(self):
        return self._run is not None

    def status_text(self):
        text = PHASE_TEXT.get(self.phase, self.phase)
        if self.rendering:
            done = sum(1 for m, state in self._run.items() if not m.running and state)
            text = f"rendering what was built ({done} of {len(self._run)})" + ("" if self.phase in (IDLE, DONE)
                                                                             else " · " + text)
        return text

    def executor_subtitle(self):
        return self.status_text()

    def executor_rows(self):
        rows = []
        brain = self.brain()
        if brain is not None:
            rows.append(("Brain", brain.name + " · " + (brain.subtitle() if brain.online else "runs here")))
        others = self.other_models()
        if others:
            rows.append(("It may use", ", ".join(m.name for m in others)))
        names = [n for n, _ in self.input_named("text")]
        if names:
            rows.append(("Prompt", ", ".join(names)))
        items = self.data_items("data")
        if items:
            rows.append(("Data", ", ".join(f"{n} ({k})" for n, _v, k in items)))
        rows.append(("Status", self.status_text()))
        built = self.built_nodes()
        if built:
            rows.append(("Built", f"{len(built)} nodes"))
        if self.stats:
            rows.append(("Usage", self.stats))
        self._budget_params()  # a service set up since: its Budget field appears
        rows += self.money_rows()
        brain_settings = online.resolve(brain)[2] if brain is not None and brain.online else {}
        rows.append(("Instructions", brain_settings.get("instructions") or PROMPT_FILE))
        return rows

    # ---- the Prompt box, Stop, reset -------------------------------------------------------------------------
    def executor_action(self, name):
        if name == "send":
            self.send_message()
        elif name == "stop":  # everything: a run of what was built, and the brain (its conversation can go on)
            if self.rendering:
                self.stop_run()
            if self.phase in BUSY and self.session is not None:
                self.session.stop("Stopped by you")
        elif name == "reset":
            self.reset_conversation()

    def send_message(self):
        """Enter in the Prompt box. First message: starts the build (your words first, then the connected prompt).
        While the brain waits: your reply, as you write it. After a build: the conversation goes on (resumed).
        While it works: to the brain at once if it can hear mid-turn (Claude Code: its running calls - renders - go
        on), else kept in the box until it's your turn."""
        text = (self.params.get("ask") or "").strip()
        if self.problems:
            self.notify()
            return
        if self.phase == WORKING:
            if text and self.session is not None and self.session.hears_while_working():
                self.set_param("ask", "")
                self.session.user_says(text, self.take_news())
            else:
                self.notify()
            return
        if _waiting(self) and self.session is not None:
            if text:
                self.set_param("ask", "")
                self.session.user_says(text, self.take_news())
            return
        if _can_continue(self):
            if text:
                self.set_param("ask", "")
                self.continue_build(text)
            return
        if text or self.connected_prompt():
            self.set_param("ask", "")
            self.start_build(text)

    def reset_conversation(self):
        """A new conversation next time: the brain forgets this one (the nodes it built stay in the graph)."""
        if self.phase in BUSY or self.rendering:
            return
        self.params["log"] = ""
        self.params["session_id"] = NO_SESSION
        self.params["sessions_before"] = None
        self.params["budget_from"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self.stats, self.news, self.session, self.phase = "", [], None, IDLE
        self.notify()

    # ---- what was built ---------------------------------------------------------------------------------------
    def built_nodes(self):
        """The nodes the last build made (by name, in the tab it built in) - kept in the graph ("built"), else read
        back from the build's state file."""
        built = self.params.get("built") or {}
        if not built.get("nodes") and self.params.get("session_id") != NO_SESSION:
            built = _state_file_built(os.path.join(WORK_ROOT, self.name, "state.json"))
        scene = self.scene()
        views = scene.views() if scene is not None else []
        window = views[0].window() if views else None
        docs = getattr(getattr(window, "tabs", None), "docs", [])
        # a build can span several tabs: each node's own tab when recorded, else its main tab, then any other
        where = built.get("where") or {}
        ordered = sorted(docs, key=lambda d: d.name != built.get("tab"))
        found = []
        for name in built.get("nodes", []):
            tabs = [d for d in docs if d.name == where[name]] if name in where else ordered
            node = next((n for d in tabs for n in [d.scene.node_named(name)] if n is not None), None)
            if node is not None and node is not self:
                found.append(node)
        return found

    def chat_text(self):
        """The conversation so far - your messages and the brain's replies, without tool calls - from Claude Code's
        own transcript of the session, so it survives restarts and crashes."""
        sid = self.resume_id()
        if not sid:
            return ""
        chat = getattr(self, "_chat", None)
        if chat is None or chat.sid != sid:
            chat = self._chat = Transcript(sid)
        # sessions that handed over to a fresh one (handover) come first: one conversation to read
        earlier = getattr(self, "_chat_before", None)
        before = list(self.params.get("sessions_before") or [])
        if earlier is None or earlier[0] != before:
            earlier = self._chat_before = (before, [Transcript(s) for s in before])
        parts = [t.text() for t in earlier[1]]
        return "\n\n── (a fresh session took over) ──\n\n".join([p for p in parts if p] + [chat.text()])

    def resume_id(self):
        """The brain's session to continue: kept in the graph, else the build's state file (graphs saved before)."""
        sid = self.params.get("session_id") or ""
        if sid == NO_SESSION:
            return ""
        if sid:
            return sid
        if not (self.params.get("built") or {}).get("nodes"):
            return ""
        try:
            with open(os.path.join(WORK_ROOT, self.name, "state.json"), encoding="utf-8") as f:
                return json.load(f).get("session_id", "")
        except (OSError, ValueError):
            return ""

    def built_modalities(self):
        return [n for n in self.built_nodes() if type(n) is type(self)]

    # ---- running what was built (the brain's render_built) ----------------------------------------------------
    def render_built(self, mods, again=False, wait=False):
        """Render the Modalities the brain built: those fed by other built ones wait for their NEW results; the
        others start at once - the worker runs them in turn, so one model's jobs run together. Unless again (or
        Overwrite results), a node whose result file exists is kept as it is (a run picks up where the last one
        stopped). The outcome is news for the brain's next turn - unless it waits for it (wait).
        Returns {"rendering": [...], "kept": [...], "picked_up": [...]}."""
        from .modality import upstream_modalities
        kept, adopted = [], []
        if not (again or self.params.get("overwrite")):
            for m in mods:
                if _has_result(m):
                    kept.append(m)
                elif _adopt_output(m):  # rendered before a crash, after the graph was last saved
                    kept.append(m)
                    adopted.append(f"{m.name} ← {os.path.basename(m.value())}")
        if adopted:
            self.add_log("Picked up from the output folder (made after the graph was saved):\n  " + "\n  ".join(adopted))
        mods = [m for m in mods if m not in kept]
        out = {"rendering": [m.name for m in mods], "kept": [f"{m.name} -> {m.value()}" for m in kept],
               "picked_up": adopted}
        if not mods:
            self.add_log(f"Render: all {len(kept)} built renders already have their results - nothing to do")
            return out
        self._run = {m: None for m in mods}  # node -> its last reported state
        self._run_lines = []  # ✓ / ✗ per node, then the total
        self._run_wait = wait
        self._run_t0 = time.time()
        self.add_log(f"Render: {len(mods)} renders" + (f" · {len(kept)} kept (their results exist)" if kept else ""))
        self.notify()
        waiting = []
        for m in mods:
            deps = [d for d in upstream_modalities(m) if d in self._run]
            if deps:
                waiting.append((m, deps))
            else:
                m.render(overwrite=False)
        for m, deps in waiting:
            m.wait_for(deps)
        self._timer = QTimer()
        self._timer.timeout.connect(self._watch_run)
        self._timer.start(1000)
        self._watch_run()
        return out

    def _watch_run(self):
        running = 0
        for m in list(self._run):
            if m.scene() is None:
                self._run.pop(m)
                continue
            state = m.stage if m.running else m.state
            if m.running:
                running += 1
            elif state != self._run[m]:
                ok = bool(m.value()) and str(state).startswith("Done")
                line = ("✓ " if ok else "✗ ") + f"{m.name}: {state or m.problem() or 'not rendered'}"
                self.add_log(line)
                self._run_lines.append(line + (f" -> {m.value()}" if ok else ""))
            self._run[m] = state
        if running == 0:
            self._timer.stop()
            done = sum(1 for m in self._run if m.value() and str(self._run[m]).startswith("Done"))
            line = f"Render finished: {done} of {len(self._run)} rendered in {time.time() - self._run_t0:.0f} s"
            self.add_log(line)
            self._run_lines.append(line)
            if not self._run_wait:  # the brain didn't wait for it: it hears with your next words
                self.news += self._run_lines
            self._run = None
            self.notify()

    def stop_run(self):
        """Stop the run: what waits stops waiting; the workers are stopped (a job already queued in a worker can't
        be taken back otherwise) - which also frees their memory."""
        from .engine.client import existing_engine
        for m in list(self._run):
            if m.running and m._waiting:
                m.action("stop")
        for python in {m.python() for m in self._run}:
            engine = existing_engine(python)
            if engine is not None and engine.busy:
                engine.shutdown()
                engine._on_exit(-1, None)  # the jobs it had end as failed (stopped)
        self.add_log("Render stopped by you")
        self._run_wait = False  # the brain hears it as news (its waiting tool call answers too)

    # ---- the conversation with the brain --------------------------------------------------------------------
    def _window(self):
        window = self.scene().views()[0].window() if self.scene() and self.scene().views() else None
        return window if window is not None and hasattr(window, "tabs") else None

    def start_build(self, first_words=""):
        """A new conversation: your words (if any) come first, then the connected prompt (get_task)."""
        window = self._window()
        if self.problems or self.phase in BUSY or window is None:
            self.notify()
            return
        for key in ("log", "session_id"):
            self.params[key] = ""
        self.params["sessions_before"] = None
        self.params["budget_from"] = time.strftime("%Y-%m-%d %H:%M:%S")  # a new conversation: spending counts anew
        self.stats = ""
        self.news = []
        self.session = session_class(self)(self, window)
        web = bool(self.params.get("web", True)) and self.brain().brain_kind() == "claude_code"
        shell = bool(self.params.get("shell", True)) and self.brain().brain_kind() == "claude_code"
        message = (f"Build request from the Executor '{self.name}'. Web search: "
                   f"{'on (WebSearch, WebFetch)' if web else 'off'}. PowerShell: "
                   f"{'on (commands on the user PC)' if shell else 'off'}. Start with get_task.")
        if first_words:
            message += ("\n\nThe user's own words, typed in the node - they come FIRST, before the connected "
                        f"prompt:\n{first_words}")
            self.add_log("You: " + first_words)
        self.session.start(message)

    def continue_build(self, text):
        """You write after the build ended: resume the brain's session, with its nodes as its own again."""
        window = self._window()
        if self.problems or self.phase in BUSY or window is None:
            self.notify()
            return
        if self.session is not None and self.session.alive():
            # its process is still there: a second one resuming the same conversation would cut off the calls it
            # is waiting for (renders) - it hears you in the one it has
            self.session.user_says(text, self.take_news())
            return
        session = session_class(self)(self, window)
        session.created = self.built_nodes()
        tab = (self.params.get("built") or {}).get("tab")
        session.target = session.doc_named(tab) or session.target
        mine = ", ".join(n.name for n in session.created) or "none left"
        message = (f"{self.take_news()}The user writes:\n{text}\n\n(Your nodes from before - yours to change "
                   f"again, in tab '{session.target.name}': {mine}.)")
        self.add_log("You: " + text)
        self.session = session
        session.start(message, resume=self.resume_id())

    def take_news(self):
        """What happened since the brain's last turn (its renders' outcome), to go before your words - once."""
        news, self.news = self.news, []
        return "Since your last turn:\n" + "\n".join(news) + "\n\n" if news else ""

    def set_phase(self, phase):
        self.phase = phase
        self.notify()

    def add_log(self, line):
        text = self.params["log"]
        lines = (text + "\n" if text else "") + line
        self.params["log"] = "\n".join(lines.splitlines()[-400:])
        self.notify()

    def executor_remove(self):
        if self.rendering:
            self.stop_run()
        if self.session is not None:
            self.session.stop("The node was deleted")


class Transcript:
    """A Claude Code session's transcript (~/.claude/projects/<work folder>/<session id>.jsonl) as a conversation,
    read incrementally (a long build's file grows to many MB). The brain's words count as a reply only in a message
    without a tool call - the notes it writes along with its tool calls are working notes (they stay in the Log)."""

    def __init__(self, sid):
        self.sid, self.path, self.offset, self.entries, self._text = sid, None, 0, [], ""
        self.with_tools = set()  # ids of the brain's messages that call a tool

    def _find(self):
        import glob
        base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
        found = (glob.glob(os.path.join(base, "projects", "*", self.sid + ".jsonl"))  # Claude Code's
                 or glob.glob(os.path.join(WORK_ROOT, "*", self.sid + ".jsonl")))     # a local brain's
        return found[0] if found else None

    def text(self):
        if self.path is None or not os.path.isfile(self.path):
            self.path, self.offset, self.entries, self.with_tools = self._find(), 0, [], set()
        if self.path is None:
            return ""
        size = os.path.getsize(self.path)
        if size < self.offset:  # rewritten: read again
            self.offset, self.entries, self.with_tools = 0, [], set()
        if size > self.offset:
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                data = f.read()
            end = data.rfind(b"\n") + 1  # whole lines only (the last may still be being written)
            for line in data[:end].splitlines():
                try:
                    e = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                mid = (e.get("message") or {}).get("id")  # one message's blocks come as separate lines
                if e.get("type") == "assistant" and any(b.get("type") == "tool_use"
                                                        for b in e["message"].get("content") or []):
                    self.with_tools.add(mid)
                self.entries += [(who, when, text, mid, said) for who, when, text, said in _chat_entries(e)]
            self.offset += end
            self._text = "\n\n".join(f"── {who} · {when}\n{text}" for who, when, text, mid, said in self.entries
                                     if not said or mid not in self.with_tools)
        return self._text


def _chat_entries(e):
    """(who, local time, text, said) for one transcript line: your messages and the brain's words, not tool
    traffic; said = the brain's own text (a reply only if its message calls no tool). Sessions from before
    2026-10-01 had plans as a tool call: those are shown too."""
    import datetime
    kind = e.get("type")
    stamp = e.get("timestamp", "")
    try:
        when = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        when = ""
    content = e.get("message", {}).get("content")
    out = []
    if kind == "user":
        texts = [content] if isinstance(content, str) else [b.get("text", "") for b in content or []
                                                             if isinstance(b, dict) and b.get("type") == "text"]
        for t in texts:
            t = _users_words(t)
            if t:
                out.append(("You", when, t, False))
    elif kind == "attachment" and (e.get("attachment") or {}).get("type") == "queued_command":
        # your words sent while it worked: Claude Code hands them to the brain within its running turn
        prompt = e["attachment"].get("prompt")
        texts = [prompt] if isinstance(prompt, str) else [b.get("text", "") for b in prompt or []
                                                          if isinstance(b, dict) and b.get("type") == "text"]
        for t in texts:
            t = _users_words(t)
            if t:
                out.append(("You", when, t, False))
    elif kind == "assistant":
        for b in content or []:
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append((e.get("agent") or "Claude", when, b["text"].strip(), True))
            elif b.get("type") == "tool_use":
                name, args = b.get("name", "").replace("mcp__nodeeditor__", ""), b.get("input", {})
                if name == "propose_plan":
                    out.append(("Claude · plan", when, str(args.get("summary", "")).strip(), False))
    return out


def _users_words(t):
    """What the user wrote, without what the app wrapped around it (the build request, news, the node list)."""
    t = (t or "").strip()
    if t.startswith("You are a fresh session of the brain"):  # the app's handover message, not the user's words
        return "(the app passed the handover note to a fresh session)"
    if t.startswith("Build request from the Executor"):
        marker = "before the connected prompt:\n"
        return t.split(marker, 1)[1].strip() if marker in t else "(Build)"
    if "The user writes:\n" in t:
        t = t.split("The user writes:\n", 1)[1]
    return t.split("\n\n(Your nodes from before", 1)[0].strip()


def _has_result(node):
    """A Modality's result is there: a file that exists, or a text result."""
    value = node.value()
    if not value:
        return False
    return True if node.produces == "text" else os.path.isfile(value)


RESULT_KEYS = {".png": "result", ".jpg": "result", ".webp": "result", ".mp4": "result_video", ".wav": "result_audio",
               ".json": "result_data", ".shots": "result_data", ".chunks": "result_data"}


def _adopt_output(node):
    """A Modality without a result whose render file is in its output folder anyway (named after its File name or
    the node: <name>_<YYYYMMDD>_<HHMMSS>...): the newest one becomes its result. True if one was found."""
    import glob
    import re
    folder = node.params.get("output_dir") or ""
    if not os.path.isdir(folder):
        return False
    names = {node.name, (node.params.get("file_name") or "").strip()} - {""}
    pats = [re.compile(re.escape(n) + r"_\d{8}_\d{6}") for n in names]
    files = [f for f in glob.glob(os.path.join(folder, "*"))
             if os.path.splitext(f)[1].lower() in RESULT_KEYS and any(p.match(os.path.basename(f)) for p in pats)]
    if not files:
        return False
    newest = max(files, key=os.path.getmtime)
    node.params[RESULT_KEYS[os.path.splitext(newest)[1].lower()]] = newest
    node.analyze()
    node.notify()
    return True


def _state_file_built(path):
    """{"tab", "nodes"} from a build's state.json (tolerant: an early version could leave it cut short)."""
    import re
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}
    try:
        data = json.loads(text)
        return {"tab": data.get("tab"), "nodes": data.get("created", [])}
    except ValueError:
        tab = re.search(r'"tab":\s*"([^"]*)"', text)
        created = re.search(r'"created":\s*\[(.*?)\]', text, re.S)
        return {"tab": tab.group(1) if tab else None,
                "nodes": re.findall(r'"([^"]+)"', created.group(1)) if created else []}


class ExecutorSession(QObject):
    """One Build: the brain's process, the bridge its tools come through, and what it has built so far."""

    def __init__(self, node, window):
        super().__init__(window)  # Qt owns it with the window: its process / port / sockets go in a known order
        self.node, self.window = node, window
        self.target = next(d for d in window.tabs.docs if d.scene is node.scene())
        self.created = []           # nodes made in this build (the only ones it may change)
        self.finished = None        # the summary once finish() was called
        self._macros = set()        # scenes with an open undo macro
        self._buf = b""
        self._clients = {}
        self.token = secrets.token_hex(16)
        self.work = os.path.join(WORK_ROOT, node.name)
        self.proc = None
        self.t0 = time.time()
        self.context_tokens = 0     # the brain's context at its last step (usage reported by Claude Code)
        self._handover = None       # its handover note, taken when the turn ends
        self._hinted = 0            # context size (k tokens) at the last "consider a handover" hint

    # ---- start / stop -------------------------------------------------------------------------------------
    def start(self, first_message=None, resume=""):
        """A new build, or (resume = a session id) the same conversation going on with the user's message."""
        os.makedirs(self.work, exist_ok=True)
        self.server = QTcpServer(self)
        if not self.server.listen(QHostAddress.LocalHost, 0):
            return self._fail("couldn't open a local port for the tools")
        self.server.newConnection.connect(self._accept)
        config = {"mcpServers": {"nodeeditor": {
            "type": "stdio", "command": sys.executable,
            # a built .exe has no python to run the script with: the exe itself relays (main.py --mcp-server)
            "args": ["--mcp-server"] if getattr(sys, "frozen", False)
            else [os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_server.py")],
            "env": {"NODE_EDITOR_PORT": str(self.server.serverPort()), "NODE_EDITOR_TOKEN": self.token}}}}
        # one file per session: Executors with the same name in two app windows share the work folder, and a
        # shared mcp.json once connected one window's brain to the other window's tools (2026-10-04)
        self._drop_mcp_config()
        mcp_path = self._mcp_path = os.path.join(self.work, f"mcp_{self.token[:12]}.json")
        with open(mcp_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=1)
        brain = self.node.brain()
        _provider, model, settings, _entry = online.resolve(brain)  # its online model file's settings
        prompt_file = settings.get("instructions") or PROMPT_FILE
        try:
            with open(prompt_file, encoding="utf-8") as f:
                instructions = f.read()
        except OSError as e:
            return self._fail(f"can't read the instructions {prompt_file}: {e}")
        web = bool(self.node.params.get("web", True))
        shell = bool(self.node.params.get("shell", True))
        # built-in tools: none - or, with Web search on, searching and reading pages; with PowerShell on, the
        # PowerShell tool (commands on this PC, without asking: the user ticked it)
        builtin = ",".join((["WebSearch", "WebFetch"] if web else []) + (["PowerShell"] if shell else []))
        self.tool_flags = (web, shell)  # a tick changed later: user_says restarts it with the new tools
        args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                "--mcp-config", mcp_path, "--strict-mcp-config", "--tools", builtin,
                "--allowedTools", "mcp__nodeeditor" + ("," + builtin if builtin else ""),
                "--permission-mode", "dontAsk", "--setting-sources", "local", "--system-prompt", instructions]
        if model.strip():
            args += ["--model", model.strip()]
        if resume:
            args += ["--resume", resume]
        self.proc = QProcess(self)
        env = online.child_env()
        env.insert("MCP_TOOL_TIMEOUT", str(35 * 60 * 1000))  # a test render (render_node) may take many minutes
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(self.work)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.readyReadStandardError.connect(self._read_err)
        self.proc.finished.connect(self._ended)
        self.node.add_log(f"{'Continued' if resume else 'Build started'} · brain: {brain.subtitle()} · "
                          f"web search {'on' if web else 'off'} · PowerShell {'on' if shell else 'off'}")
        self.proc.start(online.claude_exe(), args)
        if not self.proc.waitForStarted(10000):
            return self._fail("Claude Code didn't start")
        self._lock(True)
        self.node.set_phase(WORKING)
        self._send(first_message or f"Build request from the Executor node '{self.node.name}'. Web search: "
                   f"{'on (WebSearch, WebFetch)' if web else 'off'}. Start with get_task.")

    def stop(self, why):
        if self.proc is not None and self.proc.state() != QProcess.NotRunning:
            self.proc.kill()
        for n in self.created:  # a test render it started stops too
            if n.scene() is not None and getattr(n, "running", False) and hasattr(n, "action"):
                n.action("stop")
        self._close(STOPPED, why)

    def _fail(self, why):
        self._close(FAILED, why)

    def _drop_mcp_config(self):
        path, self._mcp_path = getattr(self, "_mcp_path", None), None
        if path:
            try:
                os.remove(path)
            except OSError:
                pass

    def _close(self, phase, why):
        self._drop_mcp_config()
        self._end_macros()
        self._lock(False)
        for sock in list(self._clients):
            sock.abort()
        self._clients.clear()
        if getattr(self, "server", None) is not None:
            self.server.close()
        self.node.add_log(why)
        self.node.set_phase(phase)
        self._save_state()

    def _ended(self, code, _status):
        if self.node.phase in (STOPPED, FAILED, DONE):
            return
        if self.finished is not None:
            self._close(DONE, "Build finished: " + self.finished)
        else:
            self._close(FAILED, f"Claude Code ended (exit code {code}) before the build was finished")

    # ---- talking to the brain ---------------------------------------------------------------------------------
    def _send(self, text):
        msg = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        self.proc.write((json.dumps(msg) + "\n").encode("utf-8"))

    def alive(self):
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def hears_while_working(self):
        """Claude Code takes a message mid-turn: it reaches the brain with its next tool result and the calls still
        running go on (checked 2026-10-03 with three running calls)."""
        return self.alive()

    def user_says(self, text, news=""):
        """The user replied: back to the brain (it reads the reply in context - "sure", "do it", a change...),
        after what happened since its last turn (news). While it works, the words reach it within its turn."""
        if not self.alive():
            return
        message = f"{news}The user writes:\n{text}" if news else text
        working = self.node.phase == WORKING
        flags = (bool(self.node.params.get("web", True)), bool(self.node.params.get("shell", True)))
        # (not mid-turn: restarting would cut off the calls it is waiting for - the new tools come at its next turn)
        if flags != getattr(self, "tool_flags", flags) and getattr(self, "session_id", "") and not working:
            # the user changed Web search / PowerShell since it started: Claude Code's tools are fixed per process,
            # so the same conversation goes on in a new one that has them
            changed = ", ".join(f"{name} {'on' if now else 'off'}" for name, now, was in
                                zip(("web search", "PowerShell"), flags, self.tool_flags) if now != was)
            self.proc.finished.disconnect(self._ended)
            self.proc.kill()
            self.proc.waitForFinished(5000)
            self.server.close()
            self._buf = b""
            self.node.add_log(f"You: {text}\nTools changed ({changed}) - the brain goes on with them")
            self.start(f"(The user changed your tools: {changed}.)\n\n{message}", resume=self.session_id)
            return
        self.node.add_log(("You (while it works): " if working else "You: ") + text)
        self._lock(True)
        self.node.set_phase(WORKING)
        self._send(message)

    def _read(self):
        self._buf += bytes(self.proc.readAllStandardOutput())
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            try:
                event = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            self._event(event)

    def _read_err(self):
        text = bytes(self.proc.readAllStandardError()).decode("utf-8", "replace")
        with open(os.path.join(self.work, "stderr.log"), "a", encoding="utf-8") as f:
            f.write(text)

    def _event(self, e):
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "init":
            servers = {s.get("name"): s.get("status") for s in e.get("mcp_servers", [])}
            self.node.add_log(f"Claude Code {e.get('claude_code_version', '')} · model {e.get('model', '?')} · "
                              f"app tools: {servers.get('nodeeditor', 'not connected').replace('pending', 'connecting')}")
            self.session_id = e.get("session_id", "")
            self.node.params["session_id"] = self.session_id  # saved with the graph: Reply resumes it later
        elif kind == "assistant":
            if self.node.phase == WAIT_ANSWER:  # it went on by itself (a background command it ran finished)
                self._lock(True)
                self.node.set_phase(WORKING)
            usage = e.get("message", {}).get("usage") or {}
            if usage:  # what the brain re-reads each step: its context (grows until a handover / compaction)
                self.context_tokens = sum(usage.get(k, 0) or 0 for k in (
                    "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            for block in e.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.node.add_log("Claude: " + block["text"].strip())
                elif block.get("type") == "tool_use":
                    name = block.get("name", "").replace("mcp__nodeeditor__", "")
                    args = json.dumps(block.get("input", {}), ensure_ascii=False)
                    self.node.add_log(f"→ {name} {args[:160] + '…' if len(args) > 160 else args}")
        elif kind == "user":  # tool results coming back to the brain: show the refusals, after the call
            for block in e.get("message", {}).get("content", []) or []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    body = block.get("content")
                    text = body if isinstance(body, str) else " ".join(
                        c.get("text", "") for c in body or [] if isinstance(c, dict))
                    self.node.add_log("  ✗ " + text[:240])
        elif kind == "result":
            self._turn_ended(e)

    def _turn_ended(self, e):
        self.stats = (f"{e.get('num_turns', '?')} turns · {e.get('duration_ms', 0) / 1000:.0f} s this turn · "
                      f"{time.time() - self.t0:.0f} s in all")
        self.node.stats = self.stats
        self._end_macros()
        if e.get("is_error"):  # the brain couldn't work at all (sign-in, limits, ...): the build ends, saying why
            why = str(e.get("result") or e.get("subtype") or "an error")
            if "authenticat" in why.lower() or "login" in why.lower():
                why += " - sign in again: in a terminal, run  claude auth login  (then Build again)"
                online.status(refresh=True)
            self.proc.kill()
            self._close(FAILED, "Claude Code: " + why)
            return
        if self.finished is not None:
            self.proc.closeWriteChannel()  # done: the process ends, _ended closes the session
            return
        if self._handover is not None:  # it wrote its handover note: a fresh session goes on from it
            return self._take_over()
        self._lock(False)  # its turn ended: your turn (its reply is in the Conversation)
        self.node.set_phase(WAIT_ANSWER)
        self._save_state()

    # ---- handover: a fresh session continues from the brain's own note ------------------------------------------
    def request_handover(self, note):
        """(handover tool) Taken when this turn ends: the note is saved, this process ends, a new one starts with it."""
        self._handover = note.strip()

    def _take_over(self):
        note, self._handover = self._handover, None
        old, old_ctx = getattr(self, "session_id", ""), self.context_tokens
        n = len(self.node.params.get("sessions_before") or []) + 1
        path = os.path.join(self.work, f"handover_{n:02d}_{time.strftime('%Y%m%d_%H%M%S')}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(note + "\n")
        self.proc.finished.disconnect(self._ended)
        self.proc.kill()
        self.proc.waitForFinished(5000)
        self.server.close()
        self._buf, self._clients, self.context_tokens, self._hinted = b"", {}, 0, 0
        if old:
            self.node.params["sessions_before"] = list(self.node.params.get("sessions_before") or []) + [old]
        mine = {}
        for node in self.created:
            if node.scene() is not None:
                mine.setdefault(self.tab_name(node.scene()), []).append(node.name)
        nodes = "; ".join(f"tab '{t}': {', '.join(names)}" for t, names in mine.items()) or "none yet"
        self.node.add_log(f"Handover {n}: the brain's context was ~{old_ctx // 1000}k tokens - a fresh session "
                          f"continues from its note ({path})")
        self.start(f"You are a fresh session of the brain of the Executor '{self.node.name}', continuing a build: "
                   f"the previous session handed over to you (its context had grown to ~{old_ctx // 1000}k tokens). "
                   f"Nothing has stopped - renders it started may still be running.\n\nIts handover note:\n{note}\n\n"
                   f"{self.node.take_news()}Your nodes (yours to change): {nodes}.\nStart with get_task (the request "
                   f"and inputs), read the files the note names - the method / template files you follow too, in "
                   f"full - and look at the approved reference pictures it names before judging anything; then go "
                   f"on with the work.")

    def context_hint(self):
        """A line for a tool answer when the brain's context has grown large (once per +100k tokens), else ""."""
        k = self.context_tokens // 1000
        if k < HANDOVER_HINT_K or k < self._hinted + 100:
            return ""
        self._hinted = k
        return (f"(App: your context is now ~{k}k tokens - every step re-reads it, which costs more and blurs "
                f"details. At the next stage boundary, call handover with a short note so a fresh session of you "
                f"continues.)")

    # ---- what the tools call (executor_tools.py) --------------------------------------------------------------
    def on_finish(self, summary, problems):
        """Done when the nodes report no problems - or when it finishes again after seeing them (it explains)."""
        self._finish_calls = getattr(self, "_finish_calls", 0) + 1
        if problems:
            self.node.add_log("Check: " + "; ".join(f"{k}: {v}" for k, v in problems.items()))
        if not problems or self._finish_calls > 1:
            self.finished = summary
        self._save_state()

    def log(self, line):
        self.node.add_log(line)

    def tab_name(self, scene):
        return next((d.name for d in self.window.tabs.docs if d.scene is scene), "?")

    def doc_named(self, name):
        return next((d for d in self.window.tabs.docs if d.name == name), None)

    def new_tab(self, name):
        doc = self.window._add_doc(name)
        doc = doc or self.doc_named(name)
        self.window._mark_dirty()
        doc.view.locked = self.node.name
        return doc

    def set_target(self, doc):
        self.target = doc

    def output_dir(self):
        from .modality import DEFAULT_OUTPUT  # not at module level: modality imports this module
        return DEFAULT_OUTPUT

    def begin_macro(self, scene):
        if scene not in self._macros:
            scene.undo_stack.beginMacro(f"Executor {self.node.name}: build")
            self._macros.add(scene)

    def _end_macros(self):
        for scene in self._macros:
            scene.undo_stack.endMacro()
        self._macros.clear()

    def keep_executor_selected(self):
        scene = self.node.scene()
        if scene is not None:
            scene.clearSelection()
            self.node.setSelected(True)

    # ---- the graph is locked while the brain works --------------------------------------------------------------
    def _lock(self, on):
        for doc in self.window.tabs.docs:
            doc.view.locked = self.node.name if on else ""
            doc.view.viewport().update()
        for action in self.window.actions():  # undo / redo / cut / copy / paste / duplicate / delete
            action.setEnabled(not on)

    # ---- the bridge: mcp_server.py's requests ----------------------------------------------------------------------
    def _accept(self):
        while self.server.hasPendingConnections():
            sock = self.server.nextPendingConnection()
            self._clients[sock] = b""
            sock.readyRead.connect(lambda s=sock: self._serve(s))
            sock.disconnected.connect(lambda s=sock: (self._clients.pop(s, None), s.deleteLater()))

    def _serve(self, sock):
        self._clients[sock] = self._clients.get(sock, b"") + bytes(sock.readAll())
        while b"\n" in self._clients[sock]:
            line, self._clients[sock] = self._clients[sock].split(b"\n", 1)
            try:
                req = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if req.get("token") != self.token:
                sock.disconnectFromHost()
                return
            if req.get("list"):
                answer = {"tools": executor_tools.listing()}
            else:
                content, is_error = executor_tools.call(self, req.get("tool"), req.get("args") or {})
                if isinstance(content, executor_tools.Later):  # e.g. a test render: answered when it's done
                    self._answer_later(sock, content, req.get("tool"), req.get("args") or {})
                    continue
                hint = self.context_hint()
                if hint and isinstance(content, list):
                    content = content + [{"type": "text", "text": hint}]
                answer = {"content": content, "isError": is_error}
            sock.write((json.dumps(answer) + "\n").encode("utf-8"))
            sock.flush()

    def _answer_later(self, sock, later, tool="", args=None):
        """Keep the app running while a tool's answer is being made; check once a second, answer when ready. If
        the call is gone by then (the brain's process ended - stopped, or it failed), the answer is news for its
        next turn: what a render made isn't lost."""
        t0 = time.time()
        timer = QTimer(self)

        def poll():
            out = later.check()
            if out is None and time.time() - t0 < later.timeout:
                return
            timer.stop()
            timer.deleteLater()
            if out is None:
                out, err = f"no result after {later.timeout // 60} minutes - still running", True
            else:
                err = False
            text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, indent=1)
            if sock in self._clients:
                sock.write((json.dumps({"content": [{"type": "text", "text": text}], "isError": err}) + "\n")
                           .encode("utf-8"))
                sock.flush()
            else:
                what = ", ".join(f"{k}={v}" for k, v in (args or {}).items())
                self.node.news.append(f"{tool}({what}), asked before your last session ended, answered: "
                                      + (text if len(text) < 1500 else text[:1500] + " …"))
                self.node.add_log(f"  {tool}({what}) answered after the session ended - kept for the brain's next turn")
        timer.timeout.connect(poll)
        timer.start(1000)

    def _save_state(self):
        alive = [n for n in self.created if n.scene() is not None]
        if alive:  # kept in the graph: Execute finds them after a restart too (each in its own tab)
            self.node.params["built"] = {"tab": self.target.name, "nodes": [n.name for n in alive],
                                         "where": {n.name: self.tab_name(n.scene()) for n in alive}}
        state = {"phase": self.node.phase, "tab": self.target.name,
                 "created": [n.name for n in self.created if n.scene() is not None],
                 "finished": self.finished, "session_id": getattr(self, "session_id", ""),
                 "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:  # written whole or not at all (a half-written file was seen)
            text = json.dumps(state, indent=1, ensure_ascii=False)
            with open(os.path.join(self.work, "state.json"), "w", encoding="utf-8", errors="replace") as f:
                f.write(text)
        except (OSError, ValueError):
            pass


def session_class(node):
    """Claude Code for a brain running online, else the app's own conversation with a language model here."""
    brain = node.brain()
    kind = brain.brain_kind() if brain is not None and brain.online else ""
    if kind == "gemini":
        from .gemini_brain import GeminiBrainSession  # not at module level: it imports this module
        return GeminiBrainSession
    return ExecutorSession if kind == "claude_code" else LocalBrainSession


class LocalBrainSession(ExecutorSession):
    """The same Executor work with a language model running here as the brain. The app holds the conversation: one
    engine job per turn of the model (engine runners.run_chat: the model's own chat template, the same tools Claude
    Code gets, its tool calls read the way its template writes them), the tools run here, their answers go back.
    The transcript is kept in the work folder in Claude Code's layout, so the Conversation, Log and resume work the
    same way. Pictures in tool answers are described in words (this brain reads text only, so far)."""

    MAX_CALLS = 80  # tool calls in one of your turns - a model going round in circles stops there

    def alive(self):
        return False  # its conversation runs in the app: nothing goes on between its turns

    def hears_while_working(self):
        return False  # your words wait in the box until its turn ends

    def request_handover(self, note):
        raise executor_tools.ToolError("handover works with a Claude Code brain only, so far")

    def context_hint(self):
        return ""

    def start(self, first_message=None, resume=""):
        from .engine.client import get_engine
        os.makedirs(self.work, exist_ok=True)
        try:
            with open(PROMPT_FILE, encoding="utf-8") as f:
                instructions = f.read()
        except OSError as e:
            return self._fail(f"can't read the instructions {PROMPT_FILE}: {e}")
        self.brain = self.node.brain()
        python = self.brain.python()
        if not python:
            return self._fail("the brain's Python env has no python.exe")
        self.engine = get_engine(python)
        self.engine.finished.connect(self._answered)
        self.engine.failed.connect(self._engine_failed)
        self.engine.cancelled.connect(self._engine_cancelled)
        self.engine.status.connect(self._engine_status)
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["inputSchema"]}}
                      for t in executor_tools.listing()]
        self.session_id = resume or f"local-{uuid.uuid4()}"
        self.node.params["session_id"] = self.session_id
        self.path = os.path.join(self.work, self.session_id + ".jsonl")
        self.messages = [{"role": "system", "content": instructions}] + (_local_messages(self.path) if resume else [])
        self.job, self.calls, self.turns, self._last_status, self._wrapping = None, 0, 0, "", False
        self.node.add_log(f"{'Continued' if resume else 'Build started'} · brain: {self.brain.name} (runs here, "
                          f"{python}) · no web search for a local brain")
        self._lock(True)
        self.node.set_phase(WORKING)
        self._user(first_message or "Start with get_task.")

    def stop(self, why):
        if self.job is not None:
            self.engine.cancel()
            self.job = None
        for n in self.created:
            if n.scene() is not None and getattr(n, "running", False) and hasattr(n, "action"):
                n.action("stop")
        self._close(STOPPED, why)

    def _close(self, phase, why):
        for signal, slot in ((self.engine.finished, self._answered), (self.engine.failed, self._engine_failed),
                             (self.engine.cancelled, self._engine_cancelled), (self.engine.status, self._engine_status)):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        super()._close(phase, why)

    def user_says(self, text, news=""):
        if self.node.phase != WAIT_ANSWER or self.job is not None:
            return
        self.node.add_log("You: " + text)
        self._lock(True)
        self.node.set_phase(WORKING)
        self._user(f"{news}The user writes:\n{text}" if news else text)

    # ---- the conversation ----------------------------------------------------------------------------------
    def _write(self, kind, content, **extra):
        line = {"type": kind, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                "message": {"role": kind, "content": content, **extra}}
        if kind == "assistant":
            line["agent"] = self.brain.name
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _user(self, text):
        self.messages.append({"role": "user", "content": text})
        self._write("user", [{"type": "text", "text": text}])
        self.calls = 0
        self._ask()

    def _ask(self, tools=True):
        b = self.brain
        job = {"chat": {"messages": self.messages, "tools": self.tools if tools else []},
               "models": [{"name": b.name, "path": b.model_path(), "role": b.role, "params": dict(b.params)}]}
        self.job = self.engine.submit(job)

    def _engine_status(self, job_id, message):
        if job_id == self.job and message != self._last_status:
            self._last_status = message
            if not message.startswith("Thinking"):
                self.node.add_log(message)
            self.node.stats = (self.stats + " · " if self.stats else "") + message

    def _engine_failed(self, job_id, message):
        if job_id == self.job:
            self.job = None
            self._close(FAILED, f"The brain stopped: {message}")

    def _engine_cancelled(self, job_id):
        if job_id == self.job:
            self.job = None

    def _answered(self, job_id, msg):
        if job_id != self.job:
            return
        self.job = None
        self.turns += 1
        self.stats = (f"{self.turns} turns · {msg.get('seconds', 0):.0f} s this turn ({msg.get('prompt_tokens')} "
                      f"tokens in, {msg.get('new_tokens')} out) · {time.time() - self.t0:.0f} s in all")
        self.node.stats = self.stats
        content, calls = (msg.get("content") or "").strip(), msg.get("tool_calls") or []
        if self._wrapping:  # its closing words after the call limit: no more tools this turn
            self._wrapping, calls = False, []
        if msg.get("cut"):
            self.node.add_log(f"(the brain's answer stopped at Max new tokens = {msg.get('new_tokens')} - raise it "
                              f"on {self.brain.name} if its answers get cut)")
        mid = f"msg_{uuid.uuid4().hex[:16]}"
        blocks = [{"type": "text", "text": content}] if content else []
        calls = [{**c, "id": f"call_{uuid.uuid4().hex[:12]}"} for c in calls]
        blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]} for c in calls]
        self._write("assistant", blocks, id=mid)
        if content:
            self.node.add_log(f"{self.brain.name}: {content}")
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = [{"type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                                     for c in calls]
        self.messages.append(message)
        if not calls:
            return self._end_turn()
        self._pending = calls
        self._next_call()

    def _next_call(self):
        if self.node.phase != WORKING:  # stopped meanwhile
            return
        if not self._pending:
            return self._ask()
        self.calls += 1
        if self.calls > self.MAX_CALLS:  # stop it here - and let it say where it is, so you know what to answer
            self.node.add_log(f"The brain made {self.MAX_CALLS} tool calls in this turn - asked it to stop and say "
                              "where it is")
            for call in self._pending:
                self.messages.append({"role": "tool", "content": "(not run - the call limit of this turn)"})
            self._pending = []
            self.messages.append({"role": "user", "content": (
                f"(The app: that was {self.MAX_CALLS} tool calls in this turn - stop here. Tell the user in a few "
                "lines what you have done, what works, what doesn't, and what you would try next.)")})
            self._wrapping = True
            return self._ask(tools=False)
        call = self._pending.pop(0)
        args = _typed_args(call["name"], call["arguments"])
        text = json.dumps(args, ensure_ascii=False)
        self.node.add_log(f"→ {call['name']} {text[:160] + '…' if len(text) > 160 else text}")
        content, is_error = executor_tools.call(self, call["name"], args)
        if isinstance(content, executor_tools.Later):  # e.g. a test render: answered when it's done
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

    def _call_done(self, call, content, is_error):
        if isinstance(content, list):  # MCP-style blocks: text as it is, a picture described
            content = "\n".join(b.get("text", "") if b.get("type") == "text" else
                                "[a picture - this brain reads text only, so far]" for b in content)
        if is_error:
            self.node.add_log("  ✗ " + str(content)[:240])
        self.messages.append({"role": "tool", "content": str(content)})
        self._write("user", [{"type": "tool_result", "tool_use_id": call["id"], "content": str(content),
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


def _typed_args(name, args):
    """A local model's tool arguments as the tool declares them (tag-style calls give text: numbers, true / false
    and lists are read from it)."""
    tool = next((t for t in executor_tools.TOOLS if t["name"] == name), None)
    props = tool["inputSchema"]["properties"] if tool else {}
    out = {}
    for key, value in (args or {}).items():
        want = props.get(key, {}).get("type")
        if want == "string" and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        elif want in ("number", "integer", "boolean", "array", "object") and isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                if want == "boolean":
                    value = value.strip().lower() in ("true", "yes", "1")
        out[key] = value
    return out


def _local_messages(path):
    """A local brain's conversation read back from its transcript (resume): the chat messages its model saw."""
    messages, open_calls = [], []
    try:
        with open(path, encoding="utf-8") as f:
            lines = [json.loads(l) for l in f if l.strip()]
    except (OSError, ValueError):
        return []
    for e in lines:
        blocks = e.get("message", {}).get("content") or []
        if e.get("type") == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [b for b in blocks if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = [{"type": "function", "function": {"name": b["name"], "arguments": b["input"]}}
                                     for b in calls]
            messages.append(msg)
            open_calls = [b["id"] for b in calls]
        else:
            for b in blocks:
                if b.get("type") == "tool_result":
                    messages.append({"role": "tool", "content": str(b.get("content", ""))})
                    if b.get("tool_use_id") in open_calls:
                        open_calls.remove(b["tool_use_id"])
                elif b.get("type") == "text":
                    messages += [{"role": "tool", "content": "(not run - the conversation was stopped)"}
                                 for _ in open_calls]
                    open_calls = []
                    messages.append({"role": "user", "content": b["text"]})
    return messages
