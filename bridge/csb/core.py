"""BridgeCore: the ONE bridge loop body (rescan / poll / build), shared by
the CLI (claude_bar_bridge.main) and the desktop app (BridgeThread).
Transport (serial vs stdout) stays with the caller."""

import json
import os
import queue
import time

from . import attention, statusline
from .chime import Chimer
from .config import data_dir, log
from .engine import LONG_TOOLS, derive
from .fmt import fmt_countdown
from .limits import AlertLog, is_expired
from .registry import Registry, transcript_path
from .session import Session, find_transcripts
from .slots import SlotAllocator
from .usage import UsageTracker

RESCAN_INTERVAL_S = 15
DISCOVERY_WINDOW_S = 7 * 86400   # ignore transcripts older than a week


def build_packet(sessions, cfg, usage, now=None, slots=None, chimer=None,
                 registry_mode=False, board=None):
    """-> the serial status packet. registry_mode: the live set is exactly
    the sessions the Claude Code registry reports alive (Session.reg),
    however long they have been quiet — parked, not hidden. `board`, if
    given, is called with (live, states, entries) for the WHOLE live set
    before the display's max_sessions curation (the web board shows
    everything)."""
    if now is None:
        now = time.time()
    if slots is None:
        # transient allocator: sl is still emitted (and stable within this
        # allocator's life); BridgeCore passes its persistent one
        slots = SlotAllocator(num_slots=max(8, cfg.get("max_sessions", 8)))
    # hook edges count as liveness/content too: an eagerly-created session
    # (UserPromptSubmit before the transcript file exists) must be visible
    # during its first turn (§c)
    ignore_ep = set(cfg.get("ignore_entrypoints", ()))
    if registry_mode:
        live = [s for s in sessions.values()
                if s.reg is not None and s.entrypoint not in ignore_ep]
    else:
        live = [s for s in sessions.values()
                if now - max(s.mtime(), s.hook_last)
                < cfg["active_window_min"] * 60
                and (s.model or s.turn_start or s.turn_started_at)
                and s.entrypoint not in ignore_ep]
    live.sort(key=lambda s: s.first_seen)
    for s in live:
        try:                       # statusline captures feed derive + packet
            statusline.refresh(s, cfg, now=now)
        except Exception:
            pass
    states = [derive(s, cfg, now) for s in live]   # one derive per session
    if chimer is not None:
        # transitions are judged on the full qualifying set, pre-curation:
        # a collapsed done session finishing must not re-chime on reappear.
        # chime_state is the stingy view: a parent still delegating reads
        # as working, so it chimes once, when its last worker finishes.
        chimer.observe({s.session_id: attention.chime_state(s, r, cfg, now)
                        for s, r in zip(live, states)}, now=now)
    full_live, full_states = live, states
    live, states, hid = _curate(live, states, cfg["max_sessions"])
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
        # additive: finished and not yet seen (attention.is_unread) — the
        # firmware draws unread-done distinctly from seen-done
        e["ur"] = int(attention.is_unread(s, r, cfg, now))
        ses.append(e)
    if board is not None:
        # every live session, curated-out ones included (slot None: not
        # on the display right now)
        shown = {s.session_id: e for s, e in zip(live, ses)}
        entries = []
        for s, r in zip(full_live, full_states):
            e = shown.get(s.session_id)
            if e is None:
                e = s.to_packet(cfg, now=now, state=r)
                e["sl"] = None
            entries.append(e)
        board(full_live, full_states, entries)
    return {
        "t": "s",
        "ses": ses,
        "act": act,
        "hid": hid,     # additive: hidden idle/done sessions beyond capacity
        "us": usage.snapshot(now=now),
    }


# capacity by curation (BACKLOG minimap decision): when more sessions
# qualify than max_sessions, needs-attention sessions always get a cell —
# wait (incl. limited/error, which are wait on the wire) first, then
# run/tool, then done, then idle. Among equals today's rule stands: the
# newest first_seen stay. Only the collapsed idle/done overflow is
# reported in "hid"; a wait/run session is never *silently* hidden (it
# can only fall off if waits+runs alone exceed max_sessions).
_ST_PRIORITY = {"wait": 0, "run": 1, "tool": 1, "done": 2, "idle": 3}


def _curate(live, states, max_sessions):
    """-> (live, states, hid) with ses[] order (first_seen) preserved."""
    if len(live) <= max_sessions:
        return live, states, 0
    ranked = sorted(range(len(live)),
                    key=lambda i: (_ST_PRIORITY.get(states[i].st, 3), -i))
    keep = sorted(ranked[:max_sessions])           # back to first_seen order
    hid = sum(1 for i in ranked[max_sessions:]
              if states[i].st in ("idle", "done"))
    return [live[i] for i in keep], [states[i] for i in keep], hid


class BridgeCore:
    def __init__(self, cfg, usage=None, session_factory=Session,
                 hook_queue=None, registry=None):
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
        self.chimer = Chimer(cfg)
        # Claude Code's live session registry: exact discovery when the
        # machine has one (registry_mode), transcript walking otherwise
        if registry is None and cfg.get("registry_enabled", True):
            registry = Registry(cfg)
        self.registry = registry
        self.registry_mode = False
        self.board_doc = None       # newest web-board document
        from . import focus          # local: keeps subprocess/AppleScript
        focus.announce(cfg)          # off the import path of every consumer

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

    def handle_device_line(self, ln):
        """Device-initiated commands arriving on the serial console
        (docs/kvm-design.md): {"t":"focus","sl":N} -> focus that terminal."""
        if not ln.startswith("{"):
            return
        try:
            msg = json.loads(ln)
        except ValueError:
            return
        if msg.get("t") == "focus":
            from . import focus
            focus.focus_slot(self, int(msg.get("sl", -1)))

    def rescan(self, now=None):
        """Discover new transcripts, drop deleted ones."""
        if now is None:
            now = time.time()
        self._last_rescan = now
        cutoff = now - DISCOVERY_WINDOW_S
        # registry mode: discovery is exact (_refresh_registry); the
        # directory walk only runs on machines without a registry
        found = {} if self.registry_mode else find_transcripts(
            self.cfg["roots"], self.cfg.get("ignore_projects", ()))
        for path, mtime in found.items():
            if mtime > cutoff and path not in self.sessions:
                self.sessions[path] = self._session_factory(path)
        fresh_cut = now - self.cfg.get("hook_fresh_s", 900)
        for path in list(self.sessions):
            s = self.sessions[path]
            if s.reg is not None:
                continue          # live per the registry, file or not yet
            if self.registry_mode and s.hook_last < now - 60:
                # no longer a live session (process exited, or /clear moved
                # it to a new transcript): release it. The 60s grace keeps
                # an eagerly hook-created session until the registry
                # catches up with a fresh sessionId.
                del self.sessions[path]
                continue
            # keep hook-fresh sessions whose transcript file does not exist
            # yet (eager creation §c: the file may lag the first edges)
            if not os.path.exists(path) and s.hook_last < fresh_cut:
                del self.sessions[path]

    def step(self, now=None):
        """One loop iteration: drain hook events, rescan when due, poll
        every session, apply the edges, feed the usage tracker, return the
        status packet dict."""
        if now is None:
            now = time.time()
        routed = self._drain_hooks(now)
        if self.hooks is not None:
            self.hooks.assert_port_file()
        self._refresh_registry()
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
        pkt = build_packet(
            self.sessions, self.cfg, self.usage, now=now, slots=self.slots,
            chimer=self.chimer, registry_mode=self.registry_mode,
            board=lambda *a: self._publish_board(*a, now=now))
        self._write_fleet(pkt, now)
        return pkt

    def _refresh_registry(self):
        """Bind every live registry entry to its CURRENT transcript's
        Session (sessionId changes on /clear, so a pid's transcript moves)
        and unbind the rest. No registry on this machine -> fallback mode,
        transcript discovery as before."""
        live = self.registry.read() if self.registry is not None else None
        self.registry_mode = live is not None
        if live is None:
            return
        bound = set()
        for sid, e in live.items():
            path = self._sid_map.get(sid) or \
                transcript_path(e, self.cfg["roots"])
            s = self.sessions.get(path)
            if s is None:
                s = self.sessions[path] = self._session_factory(path)
            s.reg = e
            if isinstance(e.get("pid"), int):
                s.claude_pid = e["pid"]          # exact, no hook needed
            cwd = e.get("cwd")
            if cwd and not s.cwd:         # launch dir, before any record
                s.cwd = cwd
                s.project = s.project or \
                    os.path.basename(cwd.rstrip("/\\")) or cwd
            self._sid_map[sid] = path
            bound.add(path)
        for path, s in self.sessions.items():
            if path not in bound:
                s.reg = None

    def _publish_board(self, live, states, entries, now):
        from . import focus
        self.board_doc = attention.build_board(
            live, states, entries, self.cfg, now=now,
            focus=focus.enabled(self.cfg))
        if self.hooks is not None:
            self.hooks.feed.publish(self.board_doc)

    def _write_fleet(self, pkt, now):
        """Snapshot the packet to fleet.json for local readers (csb.board,
        the terminal waiting-on-you row). Atomic (tmp + os.replace) so a
        reader never sees a torn file; any failure is swallowed — the
        display path must not depend on it."""
        path = self.cfg.get("fleet_file") or \
            os.path.join(data_dir(), "fleet.json")
        tmp = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(pkt, ts=now), f, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            pass

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
        if ev.get("_cmd"):
            self._command(ev)
            return None
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

    def _command(self, ev):
        """Board-originated commands, executed on the bridge thread (the
        HTTP thread only enqueues). Today: focus — jump to a session's
        terminal where an adapter exists; nothing depends on it working."""
        if ev["_cmd"] == "focus":
            from . import focus
            for s in self.sessions.values():
                if s.session_id == ev.get("sid"):
                    focus.focus_session(self, s)
                    return

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
