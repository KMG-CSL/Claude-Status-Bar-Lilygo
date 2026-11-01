"""Web board, served by the hook listener (csb.hooks.HookListener): one
static page, a JSON snapshot, and an SSE stream — the browser is a second
consumer of the same engine state the serial link sends, no new process.

    GET  /                 board.html (single file, no build step)
    GET  /api/board        current board document (csb.attention)
    GET  /api/events       text/event-stream: one "board" event per step
    POST /api/focus        {"sid"} -> jump to that terminal, when a
                           focus adapter exists (never required)
    POST /api/mark         {"sid", "op": seen|unseen|snooze|unsnooze|note,
                            "reply_ts"?, "minutes"?, "text"?} -> csb.marks
    POST /api/answer       {"sid", "answer": approve|deny} -> keystroke to
                           the session's terminal, re-checked on the
                           bridge thread (permission dialog only)

Bound to 127.0.0.1 only (the listener). GETs also check the Host header
so a DNS-rebinding page can't read the board; the POST additionally
requires a JSON content type (cross-origin pages can't send that without
a CORS preflight we never answer) and a same-host Origin when present.
"""

import json
import os
import threading
import time
from urllib.parse import urlparse

from .config import debug

PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "board.html")
KEEPALIVE_S = 15
WRITE_TIMEOUT_S = 10      # a tab that stops reading is dropped, not kept


class BoardFeed:
    """Latest board document + a condition SSE handlers wait on. The
    bridge thread publishes; any number of HTTP threads read."""

    def __init__(self):
        self._cond = threading.Condition()
        self._data = None             # bytes: the JSON document
        self._version = 0
        self.closed = False

    def publish(self, doc):
        data = json.dumps(doc, separators=(",", ":")).encode("utf-8")
        with self._cond:
            self._data = data
            self._version += 1
            self._cond.notify_all()

    def snapshot(self):
        with self._cond:
            return self._version, self._data

    def wait(self, after, timeout):
        """-> (version, data) once a version newer than `after` exists,
        or the current pair on timeout/close."""
        with self._cond:
            self._cond.wait_for(
                lambda: self._version != after or self.closed, timeout)
            return self._version, self._data

    def close(self):
        with self._cond:
            self.closed = True
            self._cond.notify_all()


def _host_ok(handler):
    port = handler.server.server_address[1]
    host = (handler.headers.get("Host") or "").lower()
    return host in (f"127.0.0.1:{port}", f"localhost:{port}")


def _origin_ok(handler):
    origin = handler.headers.get("Origin")
    if not origin:
        return True
    port = handler.server.server_address[1]
    u = urlparse(origin)
    return u.hostname in ("127.0.0.1", "localhost") and u.port == port


def _send(handler, code, body=b"", ctype="text/plain; charset=utf-8"):
    handler.send_response(code)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def handle_get(handler, feed):
    """-> True when the path was ours (response sent)."""
    path = urlparse(handler.path).path
    if path not in ("/", "/board", "/api/board", "/api/events"):
        return False
    if not _host_ok(handler):
        _send(handler, 403, b"forbidden host\n")
        return True
    if path in ("/", "/board"):
        try:
            with open(PAGE, "rb") as f:
                body = f.read()
        except OSError:
            _send(handler, 500, b"board.html missing\n")
            return True
        _send(handler, 200, body, "text/html; charset=utf-8")
    elif path == "/api/board":
        _v, data = feed.snapshot()
        _send(handler, 200, data or b"{}", "application/json")
    else:
        _stream(handler, feed)
    return True


def _stream(handler, feed):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    handler.connection.settimeout(WRITE_TIMEOUT_S)
    version = -1
    try:
        while not feed.closed:
            v, data = feed.wait(version, KEEPALIVE_S)
            if v != version:
                version = v       # even with no document yet: no spin
                if data:
                    handler.wfile.write(b"event: board\ndata: " + data
                                        + b"\n\n")
            else:
                handler.wfile.write(b": keepalive\n\n")
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass                       # the tab closed; the thread just ends
    handler.close_connection = True


POSTS = {"/api/focus": "focus", "/api/mark": "mark", "/api/answer": "answer"}
MARK_OPS = ("seen", "unseen", "snooze", "unsnooze", "note")
ANSWERS = ("approve", "deny")
NOTE_MAX = 200
SNOOZE_MAX_MIN = 24 * 60


def _command(cmd, body):
    """-> validated command event, or None. Only known fields pass, in
    their expected types; the bridge thread trusts nothing else."""
    sid = str(body.get("sid") or "")
    if not sid:
        return None
    ev = {"_cmd": cmd, "sid": sid, "ts": time.time()}
    if cmd == "mark":
        op = body.get("op")
        if op not in MARK_OPS:
            return None
        ev["op"] = op
        if op == "seen" and isinstance(body.get("reply_ts"), (int, float)):
            ev["reply_ts"] = float(body["reply_ts"])
        if op == "snooze":
            mins = body.get("minutes", 30)
            if not isinstance(mins, (int, float)) or not 1 <= mins <= SNOOZE_MAX_MIN:
                return None
            ev["minutes"] = int(mins)
        if op == "note":
            ev["text"] = str(body.get("text") or "")[:NOTE_MAX]
    elif cmd == "answer":
        if body.get("answer") not in ANSWERS:
            return None
        ev["answer"] = body["answer"]
    return ev


def handle_post(handler, enqueue):
    """-> True when the path was ours. `enqueue` puts a command event on
    the bridge's hook queue (executed on the bridge thread)."""
    path = urlparse(handler.path).path
    cmd = POSTS.get(path)
    if cmd is None:
        return False
    ctype = (handler.headers.get("Content-Type") or "").split(";")[0]
    if not (_host_ok(handler) and _origin_ok(handler)
            and ctype.strip().lower() == "application/json"):
        _send(handler, 403, b"forbidden\n")
        return True
    ev = None
    try:
        n = int(handler.headers.get("Content-Length") or 0)
        body = json.loads(handler.rfile.read(n) or b"{}")
        if isinstance(body, dict):
            ev = _command(cmd, body)
    except Exception as e:
        debug("web", f"bad {cmd} request: {e}")
    if ev is None:
        _send(handler, 400, b"bad request\n")
        return True
    enqueue(ev)
    _send(handler, 202)
    return True
