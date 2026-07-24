"""Usage tracking: OAuth usage API with a local token-count estimate fallback,
plus per-model context-window lookup."""

import json
import os
import subprocess
import time
from collections import deque

from .config import CLAUDE_DIR, IS_MAC
from .fmt import fmt_countdown, parse_ts


def oauth_token():
    """Claude Code login token: .credentials.json on Windows/Linux,
    the login Keychain on macOS. The file is checked first on every
    platform since macOS uses it as an override in SSH/tmux setups."""
    cred_path = os.path.join(CLAUDE_DIR, ".credentials.json")
    blob = None
    if os.path.exists(cred_path):
        with open(cred_path, "r", encoding="utf-8") as f:
            blob = json.load(f)
    elif IS_MAC:
        out = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            blob = json.loads(out.stdout.strip())
    if not blob:
        raise FileNotFoundError("no Claude Code credentials")
    return blob["claudeAiOauth"]["accessToken"]


_MODEL_CTX_CACHE = {}     # model_id -> (context_window, fetched_at)

# offline fallback only - the Models API is the source of truth
_MODEL_CTX_FALLBACK = (
    ("haiku", 200000),
    ("fable", 1000000), ("mythos", 1000000),
    ("sonnet-5", 1000000), ("sonnet-4-6", 1000000),
    ("opus-4-8", 1000000), ("opus-4-7", 1000000), ("opus-4-6", 1000000),
)


def model_context_limit(model_id, default):
    """Per-model context window via the Models API (max_input_tokens),
    cached per model id; falls back to a static table, then `default`."""
    if not model_id:
        return default
    hit = _MODEL_CTX_CACHE.get(model_id)
    if hit and (hit[0] or time.time() - hit[1] < 600):
        return hit[0] or default
    limit = 0
    try:
        token = oauth_token()
        import urllib.request
        req = urllib.request.Request(
            f"https://api.anthropic.com/v1/models/{model_id}",
            headers={"Authorization": f"Bearer {token}",
                     "anthropic-version": "2023-06-01",
                     "anthropic-beta": "oauth-2025-04-20"})
        with urllib.request.urlopen(req, timeout=5) as r:
            limit = int(json.loads(r.read().decode()).get("max_input_tokens") or 0)
    except Exception:
        pass
    if not limit:
        for key, val in _MODEL_CTX_FALLBACK:
            if key in model_id:
                limit = val
                break
    _MODEL_CTX_CACHE[model_id] = (limit, time.time())
    return limit or default


class UsageTracker:
    """Real numbers from the Claude OAuth usage API when available,
    otherwise a local estimate from transcript token counts."""

    # compatibility alias — oauth_token() is the module-level function now
    _oauth_token = staticmethod(oauth_token)

    def __init__(self, cfg):
        self.cfg = cfg
        self.events = deque()      # (ts, tokens)
        self.api_cache = None
        self.api_checked = 0

    def add_events(self, evs):
        cutoff = time.time() - 8 * 86400
        for e in evs:
            if e[0] > cutoff:
                self.events.append(e)
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def _try_api(self):
        if time.time() - self.api_checked < 60:
            return self.api_cache
        self.api_checked = time.time()
        try:
            token = oauth_token()
            import urllib.request
            req = urllib.request.Request(
                "https://api.anthropic.com/api/oauth/usage",
                headers={"Authorization": f"Bearer {token}",
                         "anthropic-beta": "oauth-2025-04-20"})
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode())
            out = {}
            for key, tag in (("five_hour", "5"), ("seven_day", "7")):
                blk = data.get(key) or {}
                util = blk.get("utilization")
                resets = parse_ts(blk.get("resets_at"))
                out["p" + tag] = int(round(util)) if util is not None else -1
                out["r" + tag] = fmt_countdown(resets - time.time()) if resets else ""
            out["est"] = False
            self.api_cache = out
        except Exception:
            self.api_cache = None
        return self.api_cache

    def snapshot(self):
        api = self._try_api()
        if api:
            return api
        now = time.time()
        tok5 = sum(t for ts, t in self.events if ts > now - 5 * 3600)
        tok7 = sum(t for ts, t in self.events)
        first5 = min((ts for ts, _ in self.events if ts > now - 5 * 3600), default=None)
        r5 = fmt_countdown(first5 + 5 * 3600 - now) if first5 else ""
        return {
            "p5": min(100, int(100 * tok5 / self.cfg["est_cap_5h_tokens"])),
            "p7": min(100, int(100 * tok7 / self.cfg["est_cap_7d_tokens"])),
            "r5": r5, "r7": "",
            "est": True,
        }
