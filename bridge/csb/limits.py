"""Rate-limit / API-error detection from JSONL lines (Item 5,
Maciek-roboblog analyzer.py + claude-notifications-go analyzer.go).

Structured/synthetic strings only — never agent prose. Two reset forms:
an exact unix epoch after "limit reached|", and a relative
"wait N minutes" in system messages anchored to the record timestamp.
"""

import json
import os
import re
import time

# "Claude AI usage limit reached|1750010000" (synthetic assistant msg)
# and tool_result variants both end with "limit reached|<epoch>".
LIMIT_EPOCH_RE = re.compile(r"limit reached\|(\d+)")
WAIT_MINUTES_RE = re.compile(r"wait\s+(\d+)\s+minutes?", re.IGNORECASE)
API_CODE_RE = re.compile(r"API Error:?\s*(\d{3})")


def scan_text(text, ts):
    """-> unix reset epoch, or 0 when the text carries no limit signal.
    `ts` (record timestamp) anchors the relative wait-N-minutes form."""
    if not text:
        return 0
    m = LIMIT_EPOCH_RE.search(text)
    if m:
        return int(m.group(1))
    m = WAIT_MINUTES_RE.search(text)
    if m:
        return int(ts + int(m.group(1)) * 60)
    return 0


def is_expired(reset, now):
    """One rollover guard for every reset epoch (limits and, later, the
    statusline resets_at): a past epoch must never show as active."""
    return not reset or now >= reset


def short_reason(err):
    """'authentication_failed' -> 'auth failed'; 'API Error: 401 ...'
    -> 'API 401'. Fits the td line next to the 'error ·' prefix."""
    if isinstance(err, dict):
        err = err.get("message") or err.get("type") or ""
    s = str(err or "")
    m = API_CODE_RE.search(s)
    if m:
        return "API " + m.group(1)
    s = s.replace("authentication_failed", "auth failed").replace("_", " ")
    s = " ".join(s.split())
    return s[:20] or "error"


class AlertLog:
    """Persisted per-alert cooldown ({alert_id: {triggered, ts}}) so a
    bridge restart does not re-fire the same rate-limit alert."""

    def __init__(self, path, cooldown_s=86400):
        self.path = path
        self.cooldown_s = cooldown_s
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except Exception:
            self._data = {}
        if not isinstance(self._data, dict):
            self._data = {}

    def should_fire(self, alert_id, now=None):
        """True exactly once per cooldown window; records the firing."""
        if now is None:
            now = time.time()
        rec = self._data.get(alert_id)
        if isinstance(rec, dict) and now - rec.get("ts", 0) < self.cooldown_s:
            return False
        self._data[alert_id] = {"triggered": True, "ts": now}
        self._save()
        return True

    def _save(self):
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, self.path)
        except OSError:
            pass
