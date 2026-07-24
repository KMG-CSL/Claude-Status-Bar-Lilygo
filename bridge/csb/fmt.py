"""Pure formatting / parsing helpers. No I/O, no state."""

import os
import time
from datetime import datetime

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def parse_ts(s):
    """ISO timestamp -> unix seconds, tolerant."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def pretty_model(model_id):
    if not model_id:
        return "Claude"
    m = model_id.lower().replace("claude-", "")
    # drop trailing date stamp like -20251001
    parts = [p for p in m.split("-") if not (len(p) == 8 and p.isdigit())]
    words, version = [], []
    for p in parts:
        if p.isdigit():
            version.append(p)
        else:
            words.append(p.capitalize())
    name = " ".join(words) if words else "Claude"
    if version:
        name += " " + ".".join(version)
    return name[:20]


def pretty_tool(name):
    """'mcp__workspace__bash' -> 'bash'; keep normal names as-is."""
    if not name:
        return name
    if name.startswith("mcp__"):
        name = name.split("__")[-1]
    name = name.replace("_", " ").strip()
    if len(name) > 1 and name == name.lower():
        name = name[0].upper() + name[1:]
    return name[:16]


def fmt_tokens(v):
    if v >= 1000000:
        return f"{v / 1e6:.1f}M"
    if v >= 1000:
        return f"{v // 1000}k"
    return str(v)


def tool_detail(inp):
    """One-line 'what is this tool doing' from its input, octomux-style:
    first non-empty of a priority list; basenames for path-ish fields."""
    if not isinstance(inp, dict):
        return ""
    qs = inp.get("questions")
    if isinstance(qs, list) and qs and isinstance(qs[0], dict):
        q = qs[0].get("question", "")
        if q:
            return " ".join(q.split())[:36]
    for key in ("command", "file_path", "target_file", "notebook_path",
                "pattern", "url", "query", "description", "path", "prompt"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            if key.endswith("path") or key == "target_file":
                v = os.path.basename(v)
            return v[:36]
    return ""


def fmt_countdown(seconds, now=None):
    """Relative under 24h. Beyond that, pass `now` to get the absolute
    local reset time ("Jul 26 15:04") — a day-granular countdown is
    uselessly vague for a rate-limit banner. Without `now` the legacy
    relative form ("2d1h") is kept (us.r5/r7 layout depends on it)."""
    if seconds is None or seconds < 0:
        return ""
    s = int(seconds)
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = s // 3600, (s % 3600) // 60
        return f"{h}h{m:02d}m" if h < 10 else f"{h}h"
    if now is not None:
        lt = time.localtime(now + s)
        return f"{_MONTHS[lt.tm_mon - 1]} {lt.tm_mday} " \
               f"{lt.tm_hour:02d}:{lt.tm_min:02d}"
    d, h = s // 86400, (s % 86400) // 3600
    return f"{d}d{h}h"
