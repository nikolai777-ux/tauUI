"""The app's tools for an Executor's brain, as an MCP server (stdio) - started by Claude Code (`--mcp-config`).

It only relays: every tools/list and tools/call goes over a local TCP connection to the app (executor_bridge in
executor.py), which runs the tool on the live graph and answers. Standard library only, so any Python can run it.
Environment: NODE_EDITOR_PORT, NODE_EDITOR_TOKEN (given in the MCP config the app writes for each session).

MCP over stdio = JSON-RPC 2.0, one message per line: initialize, notifications/initialized, tools/list,
tools/call, ping.

Calls run side by side: each tools/call is answered from its own thread over its own connection to the app, so
calls the brain makes together (several renders) run together, and a ping is answered while a long call waits.
"""

import json
import os
import socket
import sys
import threading

PORT = int(os.environ.get("NODE_EDITOR_PORT", "0"))
TOKEN = os.environ.get("NODE_EDITOR_TOKEN", "")
_out = threading.Lock()


def app(request):
    """One request to the app over a connection of its own, one answer back (a line of JSON each way)."""
    with socket.create_connection(("127.0.0.1", PORT), timeout=3600) as s:  # a test render can take many minutes
        f = s.makefile("rwb")
        f.write((json.dumps({"token": TOKEN, **request}) + "\n").encode("utf-8"))
        f.flush()
        line = f.readline()
    if not line:
        raise ConnectionError("the app closed the connection")
    return json.loads(line)


def reply(msg_id, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    with _out:  # answers come from several threads: one whole line at a time
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def call(msg_id, params):
    try:
        reply(msg_id, app({"tool": params.get("name"), "args": params.get("arguments") or {}}))
    except Exception as e:  # noqa: BLE001 - the brain sees what went wrong
        reply(msg_id, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})


def main():
    # MCP messages are UTF-8; on Windows a pipe would otherwise be read as the ANSI code page (cp1252): "×" became
    # "Ã—" and "←" an unencodable character that broke the app's answer to the call
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:  # a notification (initialized, cancelled...): nothing to answer
            continue
        try:
            if method == "initialize":
                version = (msg.get("params") or {}).get("protocolVersion") or "2025-06-18"
                reply(msg_id, {"protocolVersion": version, "capabilities": {"tools": {}},
                               "serverInfo": {"name": "nodeeditor", "version": "0.1"}})
            elif method == "ping":
                reply(msg_id, {})
            elif method == "tools/list":
                reply(msg_id, {"tools": app({"list": True})["tools"]})
            elif method == "tools/call":
                threading.Thread(target=call, args=(msg_id, msg.get("params") or {}), daemon=True).start()
            else:
                reply(msg_id, error={"code": -32601, "message": f"unknown method {method}"})
        except Exception as e:  # noqa: BLE001 - the brain sees what went wrong
            reply(msg_id, {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})


if __name__ == "__main__":
    main()
