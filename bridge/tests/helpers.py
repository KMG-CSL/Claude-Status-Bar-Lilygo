"""Shared fixtures: synthetic JSONL transcript builders, fake clocks,
network-free UsageTracker. No serial ports, no ~/.claude, no sleeping."""

import json
import os
import time
from datetime import datetime, timezone

from csb.config import DEFAULT_CONFIG
from csb.session import Session
from csb import usage as usage_mod

# Fixed fixture epoch (2025-06-15T15:06:40Z); tests compute offsets from it.
T0 = 1750000000.0

MODEL = "claude-fable-5-20251101"


def iso(ts):
    """Unix seconds -> the ISO-with-Z form Claude Code writes."""
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


# ---- record builders -------------------------------------------------------

def user(ts, text="fix the widget", cwd="/home/u/projects/widget"):
    return {"type": "user", "timestamp": iso(ts), "cwd": cwd, "isSidechain": False,
            "message": {"role": "user",
                        "content": [{"type": "text", "text": text}]}}


def assistant_text(ts, text="on it", model=MODEL, usage=None, **extra):
    rec = {"type": "assistant", "timestamp": iso(ts),
           "message": {"role": "assistant", "model": model,
                       "content": [{"type": "text", "text": text}]}}
    if usage:
        rec["message"]["usage"] = usage
    rec.update(extra)
    return rec


def assistant_tool_use(ts, name="Bash", tool_id="tu_1", tool_input=None,
                       model=MODEL):
    return {"type": "assistant", "timestamp": iso(ts),
            "message": {"role": "assistant", "model": model,
                        "content": [{"type": "tool_use", "id": tool_id,
                                     "name": name,
                                     "input": tool_input if tool_input is not None
                                     else {"command": "npm test"}}]}}


def tool_result(ts, tool_id="tu_1", content="ok"):
    return {"type": "user", "timestamp": iso(ts),
            "message": {"role": "user",
                        "content": [{"type": "tool_result",
                                     "tool_use_id": tool_id,
                                     "content": content}]}}


def summary(ts, text="Widget fixing session"):
    return {"type": "summary", "timestamp": iso(ts), "summary": text}


def noise(ts, ntype="file-history-snapshot"):
    return {"type": ntype, "timestamp": iso(ts)}


def sidechain(rec):
    rec = dict(rec)
    rec["isSidechain"] = True
    return rec


# ---- session / cfg builders ------------------------------------------------

def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def append_jsonl(path, records):
    with open(path, "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


class MtimeClock:
    """Injectable mtime: tests move .value instead of touching files."""

    def __init__(self, value):
        self.value = value

    def __call__(self, path):
        return self.value


def make_session(dirpath, records, mtime=T0, filename="fixture-session.jsonl"):
    """Write records to a transcript, return a polled Session whose mtime is
    the given MtimeClock (or a fixed float)."""
    path = os.path.join(dirpath, filename)
    write_jsonl(path, records)
    clock = mtime if callable(mtime) else MtimeClock(mtime)
    s = Session(path, mtime_fn=clock)
    s.mclock = clock
    s.poll([])
    return s


def base_cfg(**over):
    cfg = dict(DEFAULT_CONFIG)
    cfg["input"] = dict(DEFAULT_CONFIG["input"])
    cfg.update(over)
    return cfg


def stub_usage(cfg):
    """UsageTracker that can never reach the network: _try_api's rate-limit
    gate (now - api_checked < 60) stays permanently true."""
    u = usage_mod.UsageTracker(cfg)
    u.api_checked = 1e15
    u.api_cache = None
    return u


def seed_model_cache(model=MODEL, limit=200000):
    """Make model_context_limit(model) answer from cache — no HTTP, no
    keychain — for the fixture model id."""
    usage_mod._MODEL_CTX_CACHE[model] = (limit, time.time())
