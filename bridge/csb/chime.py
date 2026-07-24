"""Opt-in transition chime, bridge-side (the board has no speaker).

Config: {"chime": {"wait": "<sound path>", "done": "<sound path>"}} —
each value a file afplay (macOS) / paplay (Linux) can play; "" = silent
(the default). Fires on a session's transition INTO wait or done,
fire-and-forget via subprocess, rate-limited to one sound per
RATE_LIMIT_S globally, and never on the first packet after startup (a
restart must not replay every already-waiting session as news).
"""

import subprocess
import time

from .config import IS_MAC, IS_WINDOWS, debug

RATE_LIMIT_S = 10.0
PLAYER = "afplay" if IS_MAC else "paplay"   # no Windows player this wave


class Chimer:
    def __init__(self, cfg):
        self.sounds = dict(cfg.get("chime") or {})
        self._prev = None            # None = startup: first observe never plays
        self._last_play = 0.0

    def observe(self, states, now=None):
        """states: {session_id: st} for this packet. Detects edges against
        the previous packet and plays the configured sound, if any."""
        if now is None:
            now = time.time()
        prev, self._prev = self._prev, dict(states)
        if prev is None:
            return
        for sid, st in states.items():
            if st in ("wait", "done") and sid in prev and prev[sid] != st:
                self._play(st, now)

    def _play(self, st, now):
        path = (self.sounds.get(st) or "").strip()
        if not path or IS_WINDOWS:
            return
        if now - self._last_play < RATE_LIMIT_S:
            return                   # one sound per window, globally
        self._last_play = now
        try:
            subprocess.Popen([PLAYER, path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        except Exception as e:       # missing player/file: chime is best-effort
            debug("chime", f"play failed: {e}")
