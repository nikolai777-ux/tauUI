"""Console pane (bottom): your PowerShell session - and where an Executor's brain's commands show as they run.

Every shell here starts with HF_HOME = the model store and the default Python env's Scripts folder first on PATH,
so `hf download <repo>` puts models where the Library looks. Your shell is one long session (Up / Down history,
Ctrl+C or Stop ends what runs, a failed command shows its exit code). A brain's command (executor_tools.run_command)
runs in a PowerShell of its own - never in yours - and its lines appear here under the brain's name."""

import base64
import os
import re
import subprocess
import time

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from .settings import default_python_env, hf_store

READY = "##NE-READY##"  # printed after every command (exit code | current folder): marks the shell idle again
BRAIN_COLOR = "#c98a8a"  # a brain's commands and their output (the Executor's dark-red folds, lightened)
NOTE_COLOR = "#c9a36b"
STYLE = """
QWidget#ConsolePane { background: #1e1e1e; }
QWidget#PaneTitle { background: #353535; border-bottom: 1px solid #1f1f1f; }
QLabel#PaneTitleText { color: #bbb; font-weight: bold; }
QLabel#PaneHint { color: #888; }
QPlainTextEdit { background: #1a1a1a; color: #ccc; border: none; }
QLineEdit { background: #1a1a1a; color: #eee; border: none; }
QLabel#Prompt { color: #7fa7cf; background: #1a1a1a; padding-left: 4px; }
QToolButton { color: #ccc; border: 1px solid #444; background: #3a3a3a; padding: 1px 8px; }
QToolButton:hover { background: #4a6a8a; }
"""


def shell_env():
    """The environment of every shell here: the store as HF_HOME (its login too), the default env's tools first."""
    store = hf_store()
    os.makedirs(store, exist_ok=True)
    env = QProcessEnvironment.systemEnvironment()
    env.insert("HF_HOME", store)
    env.insert("PYTHONUNBUFFERED", "1")
    env.insert("PYTHONIOENCODING", "utf-8")
    env.insert("PATH", os.path.join(default_python_env(), "Scripts") + os.pathsep + env.value("PATH"))
    return env


def kill_tree(proc):
    """End a process and everything it started (a download, Blender...)."""
    pid = proc.processId()
    if pid:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    proc.waitForFinished(2000)


class BrainCommand(QObject):
    """One command of a brain: its own PowerShell process, shown in the Console as it runs.
    done / code / output / seconds are read by the tool waiting for it."""
    MAX_KEEP = 2_000_000  # characters kept for the answer (the Console shows everything)

    def __init__(self, pane, who, command, cwd, timeout):
        super().__init__(pane)
        self.pane, self.who, self.timeout = pane, who, timeout
        self.output, self.done, self.code, self.why = "", False, None, ""
        self.t0 = time.time()
        script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'\n"
                  + command + "\n$__ok = $?; $__code = $LASTEXITCODE\n"
                  "if (-not $__ok) { if ($__code) { exit $__code } else { exit 1 } }")
        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(shell_env())
        self.proc.setWorkingDirectory(cwd)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.proc.errorOccurred.connect(self._error)
        pane.brain_line(f"[{who}] PS {cwd}> {command}\n")
        self.proc.start("powershell.exe", ["-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                           base64.b64encode(script.encode("utf-16-le")).decode("ascii")])

    @property
    def seconds(self):
        return round(time.time() - self.t0, 1)

    def check_timeout(self):
        if not self.done and time.time() - self.t0 > self.timeout:
            self.kill(f"stopped after {self.timeout} s (its timeout)")

    def kill(self, why):
        if not self.done and self.proc.state() != QProcess.NotRunning:
            self.why = why
            kill_tree(self.proc)

    def _read(self):
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        if len(self.output) < self.MAX_KEEP:
            self.output += text
        self.pane.brain_line(text, raw=True)

    def _finished(self, code, _status=None):
        if self.done:
            return
        self.done, self.code = True, code
        tail = f" · {self.why}" if self.why else ""
        self.pane.brain_line(f"[{self.who}] exit {code} · {self.seconds} s{tail}\n")
        self.pane.commands.discard(self)

    def _error(self, err):
        if err == QProcess.FailedToStart:
            self.why = "PowerShell didn't start"
            self._finished(-1)


class ConsolePane(QWidget):
    idle = Signal()  # a command finished

    def __init__(self):
        super().__init__(objectName="ConsolePane")
        self.setStyleSheet(STYLE)
        self.proc = None
        self.cwd = hf_store()
        self.busy = False
        self.commands = set()  # brains' commands running now (BrainCommand)
        self.history, self._hist_pos = [], 0
        self._line, self._cr = "", False  # the line being written (progress bars rewrite it with \r)

        title = QWidget(objectName="PaneTitle")
        t = QHBoxLayout(title)
        t.setContentsMargins(10, 3, 6, 3)
        t.addWidget(QLabel("Console", objectName="PaneTitleText"))
        self.hint = QLabel(objectName="PaneHint")
        self.hint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # long text mustn't widen the window
        t.addWidget(self.hint, 1)
        for text, tip, slot in (("Stop", "Stop what runs: your command (Ctrl+C; restarts your shell) and a brain's command",
                                 self.stop),
                                ("Clear", "Clear the output", lambda: self.output.clear())):
            b = QToolButton(text=text, toolTip=tip)
            b.clicked.connect(slot)
            t.addWidget(b)

        mono = QFont("Consolas", 9)
        self.output = QPlainTextEdit(readOnly=True)
        self.output.setFont(mono)
        self.output.setMaximumBlockCount(5000)
        self.prompt = QLabel(objectName="Prompt")
        self.prompt.setFont(mono)
        self.input = QLineEdit()
        self.input.setFont(mono)
        self.input.returnPressed.connect(self._submit)
        self.input.installEventFilter(self)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self.prompt)
        row.addWidget(self.input, 1)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        v.addWidget(title)
        v.addWidget(self.output, 1)
        v.addLayout(row)
        self.start()

    # ---- shell process ---------------------------------------------------
    def start(self):
        store = hf_store()
        env = shell_env()
        self.hint.setText(f"HF_HOME = {store}   ·   tools from {os.path.join(default_python_env(), 'Scripts')}"
                          + ("" if os.path.isfile(os.path.join(store, "token")) else "   ·   not logged in to HF"))
        self.hint.setToolTip(self.hint.text())

        self.proc = QProcess(self)
        self.proc.setProcessEnvironment(env)
        self.proc.setWorkingDirectory(self.cwd if os.path.isdir(self.cwd) else store)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.proc.start("powershell.exe", ["-NoLogo", "-NoProfile", "-Command", "-"])
        self._send("[Console]::OutputEncoding = [Text.Encoding]::UTF8; $ProgressPreference = 'SilentlyContinue'")
        self._set_busy(True)
        self._send_ready()

    def stop(self):
        for c in list(self.commands):
            c.kill("stopped by the user (Console Stop)")
        if self.busy or self.proc is None or self.proc.state() == QProcess.NotRunning:
            self.restart()

    def restart(self):
        self.shutdown(brains=False)
        self._line, self._cr = "", False
        self._write("\n[shell restarted]\n", NOTE_COLOR)
        self.start()

    def shutdown(self, brains=True):
        if brains:
            for c in list(self.commands):
                c.kill("the app is closing")
        if self.proc and self.proc.state() != QProcess.NotRunning:
            self.proc.finished.disconnect(self._finished)
            kill_tree(self.proc)  # the whole tree: a download runs as a child of the shell
        self.proc = None

    def _finished(self):
        self._write("\n[shell exited - Stop restarts it]\n", NOTE_COLOR)
        self.prompt.setText("")

    # ---- a brain's commands ------------------------------------------------
    def brain_run(self, who, command, cwd, timeout):
        """Start a brain's command in a PowerShell of its own (see BrainCommand)."""
        c = BrainCommand(self, who, command, cwd, timeout)
        if not c.done:
            self.commands.add(c)
        return c

    def brain_line(self, text, raw=False):
        """A brain's command / its output, in the brain's colour, on lines of its own (never inside yours)."""
        if self._line:  # your command's line in progress: the brain's text starts below it
            self._append("\n", QTextCharFormat())
            self._line, self._cr = "", False
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(BRAIN_COLOR))
        self._append(text.replace("\r\n", "\n") if raw else text, fmt)
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def _send(self, line):
        if self.proc:
            self.proc.write((line + "\n").encode("utf-8"))

    def _send_ready(self):
        # exit code first ($? / $LASTEXITCODE of the command just run), then the folder
        self._send("$__ok = $?; $__code = $LASTEXITCODE; "
                   f"[Console]::Out.WriteLine('{READY}' + $(if ($__ok) {{ 0 }} elseif ($__code) {{ $__code }} "
                   "else { 1 }) + '|' + $PWD.Path)")

    def run(self, command):
        """Run a command as if typed (used by other parts of the app)."""
        self._write(f"PS {self.cwd}> {command}\n", "#7fa7cf")
        self.history.append(command)
        self._hist_pos = len(self.history)
        self._set_busy(True)
        self._send("$global:LASTEXITCODE = 0")  # an earlier program's code mustn't show for a failed cmdlet
        self._send(command)
        self._send_ready()

    def _submit(self):
        command = self.input.text()
        self.input.clear()
        if command.strip() in ("cls", "clear"):
            self.output.clear()
            return
        if not command.strip():
            return
        self.run(command)

    def _set_busy(self, busy):
        self.busy = busy
        self.prompt.setText("running… " if busy else f"PS {self.cwd}> ")
        if not busy:
            self.idle.emit()

    # ---- output ----------------------------------------------------------
    def _read(self):
        text = bytes(self.proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._write(text)

    def _write(self, text, color=None):
        fmt = QTextCharFormat()
        if color:
            fmt.setForeground(QColor(color))
        for part in re.split(r"(\r\n|\n|\r)", text):
            if part in ("\n", "\r\n"):
                if self._line.startswith(READY):  # the shell is idle again: show the prompt instead
                    code, _, cwd = self._line[len(READY):].partition("|")
                    self.cwd = cwd.strip() or self.cwd
                    self._set_line("", fmt)
                    self._line = ""
                    if code.strip() not in ("", "0") and self.history:  # (not after the shell's own set-up)
                        red = QTextCharFormat()
                        red.setForeground(QColor("#d46a6a"))
                        self._append(f"[exit {code.strip()}]\n", red)
                    self._set_busy(False)
                    continue
                self._append("\n", fmt)
                self._line, self._cr = "", False
            elif part == "\r":
                self._cr = True
            elif part:
                self._line = part if self._cr else self._line + part
                self._cr = False
                if not self._line.startswith(READY[:4]):  # don't flash the marker
                    self._set_line(self._line, fmt)
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def _set_line(self, text, fmt):
        c = QTextCursor(self.output.document().lastBlock())
        c.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        c.insertText(text, fmt)

    def _append(self, text, fmt):
        c = QTextCursor(self.output.document())
        c.movePosition(QTextCursor.End)
        c.insertText(text, fmt)

    # ---- history (Up / Down), Ctrl+C ---------------------------------------
    def eventFilter(self, obj, event):
        if (obj is self.input and event.type() == event.Type.KeyPress and event.key() == Qt.Key_C
                and event.modifiers() & Qt.ControlModifier and not self.input.hasSelectedText()):
            if self.busy:  # like a terminal: Ctrl+C ends what runs (with a selection it copies)
                self._write("^C\n", NOTE_COLOR)
                self.restart()
            return True
        if obj is self.input and event.type() == event.Type.KeyPress and event.key() in (Qt.Key_Up, Qt.Key_Down):
            if self.history:
                step = -1 if event.key() == Qt.Key_Up else 1
                self._hist_pos = max(0, min(len(self.history), self._hist_pos + step))
                self.input.setText(self.history[self._hist_pos] if self._hist_pos < len(self.history) else "")
            return True
        return super().eventFilter(obj, event)
