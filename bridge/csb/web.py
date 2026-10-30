"""Web board, served by the hook listener (csb.hooks.HookListener): one
static page, a JSON snapshot, and an SSE stream — the browser is a second
consumer of the same engine state the serial link sends, no new process.

    GET  /                 board.html (single file, no build step)
    GET  /api/board        current board document (csb.attention)
    GET  /api/events       text/event-stream: one "board" event per step
    POST /api/focus        {"sid": ...} -> jump to that terminal, when a
                           focus adapter exists (never required)

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
    version = -1
    try:
        while not feed.closed:
            v, data = feed.wait(version, KEEPALIVE_S)
            if v != version and data:
                version = v
                handler.wfile.write(b"event: board\ndata: " + data + b"\n\n")
            else:
                handler.wfile.write(b": keepalive\n\n")
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass                       # the tab closed; the thread just ends
    handler.close_connection = True


def handle_post(handler, enqueue):
    """-> True when the path was ours. `enqueue` puts a command event on
    the bridge's hook queue (executed on the bridge thread)."""
    path = urlparse(handler.path).path
    if path != "/api/focus":
        return False
    ctype = (handler.headers.get("Content-Type") or "").split(";")[0]
    if not (_host_ok(handler) and _origin_ok(handler)
            and ctype.strip().lower() == "application/json"):
        _send(handler, 403, b"forbidden\n")
        return True
    try:
        n = int(handler.headers.get("Content-Length") or 0)
        body = json.loads(handler.rfile.read(n) or b"{}")
        sid = str(body.get("sid") or "")
    except Exception as e:
        debug("web", f"bad focus request: {e}")
        sid = ""
    if not sid:
        _send(handler, 400, b"sid required\n")
        return True
    enqueue({"_cmd": "focus", "sid": sid, "ts": time.time()})
    _send(handler, 202)
    return True
