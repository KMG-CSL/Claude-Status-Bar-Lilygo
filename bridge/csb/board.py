"""Terminal waiting-on-you board: the fleet minimap as one status-line row.

The bridge snapshots every packet to fleet.json (BridgeCore._write_fleet);
this module renders it for a terminal instead of the LCD:

    python -m csb.board line [--dir D]   # statusLine command (stdin payload)
    python -m csb.board                  # full board, one row per session

`line` first runs the statusline collector quietly (captures keep feeding
the bridge's context/rate-limit numbers), then prints one row:

    ctx 34% │ ⏳ waiting on you: brain sync 4m · loaner prep 1m │ 3 running

A snapshot older than STALE_S means the bridge is not running; the row
says so instead of showing a frozen fleet.
"""

import argparse
import contextlib
import json
import os
import re
import sys
import time

from . import statusline
from .config import data_dir, load_config

STALE_S = 10
NAME_MAX = 24

ORANGE = "\033[38;5;173m"
DIM = "\033[2m"
RESET = "\033[0m"

# titles come from transcripts: one line, no terminal control sequences
_CTRL = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
                   r"|[\x00-\x1f\x7f-\x9f]")


def fleet_path(cfg):
    return cfg.get("fleet_file") or os.path.join(data_dir(), "fleet.json")


def read_fleet(path, now, stale_s=STALE_S):
    """-> packet dict, or None when missing / unparseable / stale."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return None
    ts = doc.get("ts") if isinstance(doc, dict) else None
    if not isinstance(ts, (int, float)) or now - ts > stale_s:
        return None
    return doc


def fmt_age(seconds):
    s = max(0, int(seconds or 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}"


def _name(e):
    nm = " ".join(_CTRL.sub(" ", str(e.get("nm") or e.get("pj") or "?"))
                  .split()) or "?"
    return nm if len(nm) <= NAME_MAX else nm[:NAME_MAX - 1] + "…"


def waiting(ses):
    """Actionable waits (at: approval/question/error, never a rate limit),
    longest-waiting first."""
    w = [e for e in ses if e.get("at")]
    return sorted(w, key=lambda e: -(e.get("el") or 0))


def summarize(doc, color=True):
    """-> the fleet half of the row ("" never; always says something)."""
    if doc is None:
        return f"{DIM}board: bridge off{RESET}" if color else \
            "board: bridge off"
    ses = doc.get("ses") or []
    w = waiting(ses)
    running = sum(1 for e in ses if e.get("st") in ("run", "tool"))
    limited = sum(1 for e in ses if e.get("lim"))
    done = sum(1 for e in ses if e.get("st") == "done")
    rest = [f"{running} running"] if running else []
    if limited:
        rest.append(f"{limited} rate-limited")
    if done:
        rest.append(f"{done} done")
    tail = " · ".join(rest)
    if w:
        names = " · ".join(f"{_name(e)} {fmt_age(e.get('el'))}" for e in w)
        head = f"⏳ waiting on you: {names}"
        if color:
            head = f"{ORANGE}{head}{RESET}"
    else:
        head = "✓ nothing waiting"
    return f"{head} │ {tail}" if tail else head


def line(stdin_bytes, cfg, now=None, capture_dir=None):
    """statusLine entry: collect quietly, then one row."""
    if now is None:
        now = time.time()
    statusline.collect(stdin_bytes, capture_dir or
                       statusline.capture_dir(cfg), quiet=True)
    own = ""
    try:
        payload = json.loads(stdin_bytes.decode("utf-8", "replace"))
        pct = (payload.get("context_window") or {}).get("used_percentage")
        if isinstance(pct, (int, float)):
            own = f"ctx {int(round(pct))}% │ "
    except Exception:
        pass
    return own + summarize(read_fleet(fleet_path(cfg), now))


def board(cfg, now=None):
    """Full multi-line board for a terminal."""
    if now is None:
        now = time.time()
    doc = read_fleet(fleet_path(cfg), now)
    if doc is None:
        return "bridge off (no fresh fleet.json)"
    rows = []
    for e in sorted(doc.get("ses") or [],
                    key=lambda e: (not e.get("at"), -(e.get("el") or 0))):
        st = "WAIT" if e.get("at") else (e.get("st") or "?").upper()
        detail = e.get("tl") or ""
        rows.append(f"{st:5} {fmt_age(e.get('el')):>5}  {_name(e):{NAME_MAX}}"
                    f"  {e.get('pj', '')[:20]:20}  {detail}")
    if doc.get("hid"):
        rows.append(f"      (+{doc['hid']} idle/done hidden)")
    return "\n".join(rows) or "no live sessions"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m csb.board")
    ap.add_argument("cmd", nargs="?", default="board", choices=("board", "line"))
    ap.add_argument("--dir", default="", help="statusline capture dir")
    args = ap.parse_args(argv)
    if args.cmd == "line":
        # the row is the ONLY stdout: config/collector logs go to stderr,
        # and nothing (config included) may fail the statusline
        try:
            with contextlib.redirect_stdout(sys.stderr):
                out = line(sys.stdin.buffer.read(), load_config(),
                           capture_dir=args.dir or None)
        except Exception:
            out = "board: error"
        sys.stdout.write(out + "\n")
    else:
        print(board(load_config()))


if __name__ == "__main__":
    main()
