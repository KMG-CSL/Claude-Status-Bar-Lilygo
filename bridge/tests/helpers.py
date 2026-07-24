"""Shared fixtures: synthetic JSONL transcript builders, fake clocks,
network-free UsageTracker. No serial ports, no ~/.claude, no sleeping."""

import json
import os
import tempfile
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


def system(ts, text="compact boundary"):
    return {"type": "system", "timestamp": iso(ts), "content": text}


def api_error(ts, error=None, text="API Error"):
    """Synthetic isApiErrorMessage assistant record (limit banner or
    API/auth failure)."""
    rec = {"type": "assistant", "timestamp": iso(ts), "isApiErrorMessage": True,
           "message": {"role": "assistant", "model": "<synthetic>",
                       "content": [{"type": "text", "text": text}]}}
    if error is not None:
        rec["error"] = error
    return rec


def sidechain(rec):
    rec = dict(rec)
    rec["isSidechain"] = True
    return rec


# ---- hook builders ---------------------------------------------------------

def hook_payload(name, transcript_path, session_id="sid-1",
                 cwd="/home/u/projects/widget", tool_name=None, **extra):
    """Recorded-style hook payload as Claude Code POSTs it on stdin —
    includes the fields the bridge ignores, to prove tolerant parsing."""
    p = {"session_id": session_id, "transcript_path": transcript_path,
         "cwd": cwd, "hook_event_name": name,
         "permission_mode": "default"}
    if tool_name is not None:
        p["tool_name"] = tool_name
    p.update(extra)
    return p


def hook_event(name, ts, transcript_path="", session_id="sid-1",
               cwd="/home/u/projects/widget", tool_name=None, message=None):
    """Parsed listener event (what the queue carries) with an injected
    arrival ts, for driving Session.apply_hook on a fake clock."""
    return {"hook_event_name": name, "session_id": session_id,
            "transcript_path": transcript_path, "cwd": cwd,
            "tool_name": tool_name, "message": message, "ts": ts}


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
    # keep tests away from any real capture dir (data_dir()/statusline):
    # a nonexistent path means "no captures" unless a test overrides it
    cfg["statusline_dir"] = os.path.join(
        tempfile.gettempdir(), "csb-tests-no-captures")
    # likewise keep slot persistence out of the real data dir: the parent
    # dir does not exist, so the allocator's writes no-op (in-memory)
    cfg["slots_file"] = os.path.join(
        tempfile.gettempdir(), "csb-tests-no-slots", "slots.json")
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
