"""BridgeCore: the ONE bridge loop body (rescan / poll / build), shared by
the CLI (claude_bar_bridge.main) and the desktop app (BridgeThread).
Transport (serial vs stdout) stays with the caller."""

import os
import time

from .engine import LONG_TOOLS, derive
from .session import Session, find_transcripts
from .usage import UsageTracker

RESCAN_INTERVAL_S = 15
DISCOVERY_WINDOW_S = 7 * 86400   # ignore transcripts older than a week


def build_packet(sessions, cfg, usage, now=None):
    if now is None:
        now = time.time()
    live = [s for s in sessions.values()
            if now - s.mtime() < cfg["active_window_min"] * 60
            and (s.model or s.turn_start)]
    live.sort(key=lambda s: s.first_seen)
    live = live[-cfg["max_sessions"]:]
    states = [derive(s, cfg, now) for s in live]   # one derive per session
    act = 0
    if live:
        # auto-follow: prefer a waiting session, else most recently active
        waiting = [i for i, r in enumerate(states) if r.st == "wait"]
        if waiting:
            act = waiting[0]
        else:
            act = max(range(len(live)), key=lambda i: live[i].mtime())
    return {
        "t": "s",
        "ses": [s.to_packet(cfg, now=now, state=r)
                for s, r in zip(live, states)],
        "act": act,
        "us": usage.snapshot(),
    }


class BridgeCore:
    def __init__(self, cfg, usage=None, session_factory=Session):
        self.cfg = cfg
        self.usage = usage if usage is not None else UsageTracker(cfg)
        self.sessions = {}          # transcript path -> Session
        self._session_factory = session_factory
        self._last_rescan = 0.0

    def rescan(self, now=None):
        """Discover new transcripts, drop deleted ones."""
        if now is None:
            now = time.time()
        self._last_rescan = now
        cutoff = now - DISCOVERY_WINDOW_S
        for path, mtime in find_transcripts(self.cfg["roots"]).items():
            if mtime > cutoff and path not in self.sessions:
                self.sessions[path] = self._session_factory(path)
        for path in list(self.sessions):
            if not os.path.exists(path):
                del self.sessions[path]

    def step(self, now=None):
        """One loop iteration: rescan when due, poll every session, feed the
        usage tracker, return the status packet dict."""
        if now is None:
            now = time.time()
        if now - self._last_rescan > RESCAN_INTERVAL_S:
            self.rescan(now)
        new_usage = []
        for s in self.sessions.values():
            s.poll(new_usage)
        self.usage.add_events(new_usage)
        return build_packet(self.sessions, self.cfg, self.usage, now=now)

    def next_interval(self, now=None):
        """Sleep hint for the caller loop: tighten to approval_confirm_s
        while any session is within 1s of the write-silence approval flip
        (Item 2's confirm cadence), else the normal send interval."""
        if now is None:
            now = time.time()
        base = self.cfg.get("send_interval_s", 1.0)
        confirm = self.cfg.get("approval_confirm_s", 0.5)
        thr = self.cfg.get("approval_silence_s",
                           self.cfg.get("wait_tool_s", 20))
        for s in self.sessions.values():
            for name, pts, _detail in s.pending_ids.values():
                if name in LONG_TOOLS or name == "AskUserQuestion":
                    continue
                silence = now - max(pts, s.last_event_ts or pts)
                if abs(silence - thr) <= 1.0:
                    return min(base, confirm)
        return base
