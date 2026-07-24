"""BridgeCore: the ONE bridge loop body (rescan / poll / build), shared by
the CLI (claude_bar_bridge.main) and the desktop app (BridgeThread).
Transport (serial vs stdout) stays with the caller."""

import os
import queue
import time

from . import statusline
from .config import data_dir, log
from .engine import LONG_TOOLS, derive
from .fmt import fmt_countdown
from .limits import AlertLog, is_expired
from .session import Session, find_transcripts
from .slots import SlotAllocator
from .usage import UsageTracker

RESCAN_INTERVAL_S = 15
DISCOVERY_WINDOW_S = 7 * 86400   # ignore transcripts older than a week


def build_packet(sessions, cfg, usage, now=None, slots=None):
    if now is None:
        now = time.time()
    if slots is None:
        # transient allocator: sl is still emitted (and stable within this
        # allocator's life); BridgeCore passes its persistent one
        slots = SlotAllocator(num_slots=max(8, cfg.get("max_sessions", 8)))
    # hook edges count as liveness/content too: an eagerly-created session
    # (UserPromptSubmit before the transcript file exists) must be visible
    # during its first turn (§c)
    live = [s for s in sessions.values()
            if now - max(s.mtime(), s.hook_last) < cfg["active_window_min"] * 60
            and (s.model or s.turn_start or s.turn_started_at)]
    live.sort(key=lambda s: s.first_seen)
    live = live[-cfg["max_sessions"]:]
    for s in live:
        try:                       # statusline captures feed derive + packet
            statusline.refresh(s, cfg, now=now)
        except Exception:
            pass
    states = [derive(s, cfg, now) for s in live]   # one derive per session
    act = 0
    if live:
        # auto-follow: prefer a waiting session, else most recently active.
        # Rate-limited / error waits are excluded from the preference (§e):
        # a limited session would otherwise pin act for its whole countdown
        # and starve a genuine approval prompt appearing later.
        waiting = [i for i, r in enumerate(states)
                   if r.st == "wait" and not r.lim and not r.err]
        if waiting:
            act = waiting[0]
        else:
            act = max(range(len(live)), key=lambda i: live[i].mtime())
    # stable slot identity (additive "sl"): assigned at first display,
    # kept for the session's lifetime — ses[] order stays first_seen as
    # today, sl is metadata for displays that want stable letters
    slot_map = slots.assign([s.session_id for s in live], now=now)
    ses = []
    for s, r in zip(live, states):
        e = s.to_packet(cfg, now=now, state=r)
        e["sl"] = slot_map[s.session_id]
        ses.append(e)
    return {
        "t": "s",
        "ses": ses,
        "act": act,
        "us": usage.snapshot(now=now),
    }


class BridgeCore:
    def __init__(self, cfg, usage=None, session_factory=Session,
                 hook_queue=None):
        self.cfg = cfg
        self.usage = usage if usage is not None else UsageTracker(cfg)
        self.sessions = {}          # transcript path -> Session
        self._session_factory = session_factory
        self._last_rescan = 0.0
        self.alerts = None          # lazy AlertLog; created on first limit
        self.hook_queue = hook_queue   # queue.Queue of parsed hook events
        self.hooks = None           # HookListener once start_hooks() ran
        self._sid_map = {}          # session_id -> transcript path fallback
        self.slots = SlotAllocator(
            cfg.get("slots_file") or os.path.join(data_dir(), "slots.json"),
            max(8, cfg.get("max_sessions", 8)))

    def start_hooks(self):
        """Start the localhost hook listener (Item 1). Called by the
        runtime entry points only — tests inject hook_queue instead, so
        no test ever binds the preferred port or writes a port file
        outside its temp data dir."""
        if not self.cfg.get("hooks_enabled", True) or self.hooks is not None:
            return self.hooks
        from .hooks import HookListener
        try:
            self.hooks = HookListener(self.cfg)
        except OSError as e:
            log("hooks", f"listener disabled: {e}")
            return None
        self.hook_queue = self.hooks.queue
        log("hooks", f"listening on 127.0.0.1:{self.hooks.port} "
                     f"(port file {self.hooks.port_file})")
        return self.hooks

    def rescan(self, now=None):
        """Discover new transcripts, drop deleted ones."""
        if now is None:
            now = time.time()
        self._last_rescan = now
        cutoff = now - DISCOVERY_WINDOW_S
        for path, mtime in find_transcripts(self.cfg["roots"]).items():
            if mtime > cutoff and path not in self.sessions:
                self.sessions[path] = self._session_factory(path)
        fresh_cut = now - self.cfg.get("hook_fresh_s", 900)
        for path in list(self.sessions):
            # keep hook-fresh sessions whose transcript file does not exist
            # yet (eager creation §c: the file may lag the first edges)
            if not os.path.exists(path) \
                    and self.sessions[path].hook_last < fresh_cut:
                del self.sessions[path]

    def step(self, now=None):
        """One loop iteration: drain hook events, rescan when due, poll
        every session, apply the edges, feed the usage tracker, return the
        status packet dict."""
        if now is None:
            now = time.time()
        routed = self._drain_hooks(now)
        if now - self._last_rescan > RESCAN_INTERVAL_S:
            self.rescan(now)
        new_usage = []
        for s in self.sessions.values():
            s.poll(new_usage)
        # edges are applied AFTER the poll so the transcript facts they
        # override (pending ids, sticky-done) are current first — a Stop
        # must clear this turn's tool_use ids, not miss ones still
        # buffered in the file
        for session, ev in routed:
            session.apply_hook(ev)
        self.usage.add_events(new_usage)
        self._limit_alerts(now)
        return build_packet(self.sessions, self.cfg, self.usage, now=now,
                            slots=self.slots)

    def _drain_hooks(self, now):
        """-> [(session, event)] for every queued hook event. Events route
        by transcript_path (canonical key) with session_id as fallback;
        an unknown transcript_path eagerly creates the Session and
        triggers an immediate rescan — edges are never dropped (§c)."""
        if self.hook_queue is None:
            return []
        routed = []
        while True:
            try:
                ev = self.hook_queue.get_nowait()
            except queue.Empty:
                break
            s = self._route_hook(ev, now)
            if s is not None:
                routed.append((s, ev))
        return routed

    def _route_hook(self, ev, now):
        path = ev.get("transcript_path") or ""
        sid = ev.get("session_id") or ""
        if not path and sid:
            path = self._sid_map.get(sid, "")
        if not path:
            return None
        if sid:
            self._sid_map[sid] = path
        s = self.sessions.get(path)
        if s is None:
            # eager creation: SessionStart/UserPromptSubmit fire before the
            # 15s rescan notices the transcript (the file may not exist
            # yet — the tailer no-ops until it has bytes). Dropping the
            # edge would leave turn_stopped_at without turn_started_at and
            # report done during the first live turn.
            s = self.sessions[path] = self._session_factory(path)
            s.hook_last = ev.get("ts") or now   # survive the rescan's cleanup
            self.rescan(now)
            s = self.sessions.get(path, s)
        return s

    def _limit_alerts(self, now):
        """Announce a newly-hit rate limit once per cooldown window; the
        persisted AlertLog keeps a bridge restart from re-firing it."""
        for path, s in self.sessions.items():
            if s.limit_reset and not is_expired(s.limit_reset, now):
                if self.alerts is None:
                    self.alerts = AlertLog(
                        os.path.join(data_dir(), "alerts.json"),
                        self.cfg.get("alert_cooldown_s", 86400))
                aid = f"limit:{os.path.basename(path)}:{int(s.limit_reset)}"
                if self.alerts.should_fire(aid, now):
                    log("limit", f"rate limited, resets in "
                        f"{fmt_countdown(s.limit_reset - now, now=now)} "
                        f"({os.path.basename(path)})")

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
