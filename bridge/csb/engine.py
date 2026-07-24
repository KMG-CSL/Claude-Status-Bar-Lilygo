"""State derivation for a session: one function, computed fresh at every
packet build from parsed transcript facts (read-time decay — no stored
mutable state that rots).

Three evidence tiers, strict precedence (§b): hook edges (only while the
session is hook-fresh) > transcript inference > mtime heuristics. A
session that never produced hook traffic behaves exactly as before —
per-session freshness, so one instrumented session cannot make the
engine trust missing edges on an uninstrumented one.
"""

import time
from collections import namedtuple

from .fmt import fmt_countdown
from .limits import is_expired

# st:  run|tool|wait|idle|done (frozen serial contract)
# tl:  raw tool name ("" when none) — prettified at packet time
# td:  one-line tool detail
# el:  elapsed seconds for the display timer (two-timer contract, Item 4:
#      run/tool tick from turn start, wait ticks from turn end, done is
#      the frozen turn duration, idle is 0 — all computed at read time)
# lim: unix epoch the rate limit lifts (0 when not limited)
# err: short API-error reason ("" when none)
# src: evidence tier that decided st — "h" hook / "t" transcript / "m" mtime
# fin: terminal outcome ok/fail/cancel, "" unless st=done
StateResult = namedtuple("StateResult", "st tl td el lim err src fin",
                         defaults=(0, "", "t", ""))

# Orchestration tools legitimately run for minutes with no writes to
# the main transcript (subagents write elsewhere) - never read them
# as permission prompts. Active subagent files are proof of work.
LONG_TOOLS = ("Task", "Agent", "Workflow", "TaskOutput", "Monitor")


def derive(session, cfg, now=None):
    """-> StateResult for `session` at time `now` (injected for tests)."""
    if now is None:
        now = time.time()
    hooky = _hook_fresh(session, cfg, now)

    # Step 1 (Item 5): explicit rate-limit / API-error facts beat every
    # inference. tl stays "" on purpose — the firmware renders a non-empty
    # tl on st=wait as "approve: <tl>", which would ask the user to approve
    # a rate limit. The countdown/reason lives in td; is_expired guards
    # against ever showing a past-epoch banner.
    if session.limit_reset and not is_expired(session.limit_reset, now):
        td = "rate limit · " + fmt_countdown(session.limit_reset - now,
                                             now=now)
        return StateResult("wait", "", td,
                           _elapsed(session, "wait", now, hooky),
                           int(session.limit_reset), "")
    if session.error:
        return StateResult("wait", "", ("error · " + session.error)[:32],
                           _elapsed(session, "wait", now, hooky),
                           0, session.error)

    st, tool, detail, src = _state(session, cfg, now, hooky)
    return _apply_sticky_done(session, st, tool, detail, src, now, hooky)


def _hook_fresh(session, cfg, now):
    """Per-session hook freshness: edges are trusted only while at least
    one hook event arrived for THIS session within hook_fresh_s."""
    hl = session.hook_last
    return bool(hl) and (now - hl) < cfg.get("hook_fresh_s", 900)


def _armed_perm_prompt(session):
    """Only an ARMED latch may produce wait (§b): any real transcript
    activity after the prompt — assistant record, tool_result, new
    tool_use (noise excluded in session.py) — is proof it was answered
    or approved, because approving a dialog/plan/question emits no
    UserPromptSubmit. The hook-side clears (PostToolUse / Stop /
    UserPromptSubmit / SessionStart) happen in Session.apply_hook."""
    p = session.perm_prompt_at
    if p is None:
        return False
    if session.last_event_ts and session.last_event_ts > p:
        session.perm_prompt_at = None
        return False
    return True


def _apply_sticky_done(session, st, tool, detail, src, now, hooky=False):
    """Sticky-done (Extra B, ccm file-store.ts:129-136): once a session
    reports done, late PostToolUse flushes, summary records and noise
    events must not cause done->run flicker. The latch keys on turn_start
    (which only a real, non-tool_result, non-noise user record moves) and
    freezes el so the finished-turn duration can't drift under late
    writes.

    "done" is a heuristic (done_after_s of transcript quiet), and mid-turn
    quiet is routine — extended thinking, long generations, compaction —
    so the latch can arm during a live turn. It therefore releases on
    EITHER kind of genuine resumption:
      * a moved turn_start (real new user prompt), or
      * new assistant output after arming (tool_use, questions, text) —
        the model only writes assistant records while actually working,
        so this is proof the turn never ended.
    A lone late tool_result flush sets last_role to "user" without moving
    turn_start or producing assistant output, so it stays masked — that
    is the flicker this latch exists to kill. Likewise an assistant
    record TIMESTAMPED BEFORE the Stop/SessionEnd edge is a late flush
    of pre-stop work (hook edges arrive over HTTP instantly; transcript
    records land on the next poll tick), not resumption — releasing on
    it would rearm via _classify_fin(), which cannot reproduce a
    hook-classified 'cancel' (pending_ids were cleared at the edge), so
    it stays masked too. Rate-limit/error states
    bypass the latch by returning earlier in derive() — both are new
    information worth showing on a done tile. A computed WAIT also always
    releases the latch: an armed perm-prompt edge or a post-Stop trailing
    "?" (engine step 5) is news the user must see, and no stale-flush
    path can produce wait. UserPromptSubmit and SessionStart edges clear
    the latch directly in Session.apply_hook."""
    latch = session.done_latch
    if latch is not None and (
            st == "wait"                           # waits are always news
            or latch[0] != session.turn_start      # real new prompt
            or (session.last_role == "assistant"
                and session.last_event_ts != latch[2]      # turn resumed…
                and not (session.turn_stopped_at           # …unless it is a
                         and session.last_event_ts         # pre-edge flush
                         <= session.turn_stopped_at))):
        session.done_latch = latch = None
        session.fin = ""      # released: the turn is not finished after all
    if latch is not None:
        return StateResult("done", "", "", latch[1], 0, "", latch[3],
                           session.fin)
    el = _elapsed(session, st, now, hooky)
    if st == "done":
        session.done_latch = (session.turn_start, el,
                              session.last_event_ts, src)
        if not session.fin:
            # transcript-inferred ending (no Stop edge): classify from the
            # same turn-scoped window the edge path uses. Cancel is a
            # hook-only signal — an inferred done never has pending ids
            # (those derive as wait/tool), so ok/fail is the whole space.
            session.fin = session._classify_fin()
    return StateResult(st, tool, detail, el, 0, "", src,
                       session.fin if st == "done" else "")


def _state(session, cfg, now, hooky):
    # Real parsed events are the activity clock; mtime is only the fallback
    # for transcripts that have produced no events yet. Noise records
    # (file-history-snapshot & friends, skipped in session.py) bump mtime
    # without meaning anything — Extra A. For hook-fresh sessions the clock
    # also counts hook edges (§b: max(last_event_ts, hook edge times)) so
    # an eagerly-created session with an empty transcript reads as active.
    activity = session.last_event_ts or 0
    if hooky:
        activity = max(activity, session.hook_last)
    activity = activity or session.mtime()
    # A statusline capture proves the Claude Code process is alive (Item 3
    # liveness signal) — it holds off the stale tier, but it is NOT
    # transcript activity: the quiet/silence clocks below stay on
    # `activity`, or captures would defer done/approval flips forever.
    fresh = (now - max(activity, session.sl_ts)) < cfg["idle_after_s"]

    pending = None
    if session.pending_ids:
        pending = sorted(session.pending_ids.values(), key=lambda v: v[1])[-1]

    if pending and pending[0] == "AskUserQuestion":
        # Claude explicitly asked a question - waiting, no threshold
        return "wait", "Question", pending[2], "t"

    # §b step 2: an armed perm-prompt latch (Notification permission_prompt
    # or PreToolUse ExitPlanMode|AskUserQuestion) -> instant wait. The
    # transcript still supplies the "what needs approval" line when it has
    # the pending tool_use.
    if hooky and _armed_perm_prompt(session):
        if pending and pending[0] not in LONG_TOOLS:
            return "wait", pending[0], pending[2], "h"
        return "wait", "", "", "h"

    if pending and (pending[0] in LONG_TOOLS
                    or session.subagent_count(cfg, now) > 0):
        return "tool", pending[0], pending[2], "t"

    if pending:
        name, pts, detail = pending
        if hooky:
            # §b step 4, hook-fresh: the Notification(permission_prompt)
            # hook is registered, so the ABSENCE of an armed latch is
            # positive evidence the tool was auto-approved/allowlisted and
            # is simply executing. The write-silence heuristic is disabled
            # entirely — a pre-approved 60s Bash never shows "needs
            # approval".
            return "tool", name, detail, "h"
        # write-silence approval heuristic (Item 2): Claude Code writes
        # nothing to the main transcript while a tool executes, so silence
        # after a tool_use means execution *or* a permission prompt; the
        # debounce keeps allowlisted long tools from flashing "approval".
        silence = now - max(pts, activity)
        thr = cfg.get("approval_silence_s", cfg.get("wait_tool_s", 20))
        if not fresh or silence > thr:
            return "wait", name, detail, "t"   # likely a permission prompt
        return "tool", name, detail, "t"

    if hooky:
        # §b step 5: authoritative turn edges. Stop newer than the last
        # UserPromptSubmit -> the turn ended; classify the ending: a
        # trailing "?" still flips to wait after question_after_s (the
        # Stop edge confirms the turn ended, it does not negate the
        # question), else done (sticky). A newer UserPromptSubmit -> the
        # turn is running, whatever the transcript's quiet says.
        started, stopped = session.turn_started_at, session.turn_stopped_at
        if stopped and (started is None or stopped > started):
            quiet = now - activity
            asks = session.last_assistant_text.rstrip().endswith("?")
            if asks and quiet > cfg["question_after_s"]:
                return "wait", "", "", "h"
            return "done", "", "", "h"
        if started:
            return "run", "", "", "h"
        # hook-fresh but no turn edge yet (e.g. SessionStart only):
        # fall through to the transcript/mtime tiers

    if fresh:
        # no pending tools: if the last thing was an assistant message and
        # nothing new has been written for a while, the turn is over.
        # A message ending in "?" flips to wait on a shorter fuse.
        if session.last_role == "assistant":
            quiet = now - activity
            asks = session.last_assistant_text.rstrip().endswith("?")
            if asks and quiet > cfg["question_after_s"]:
                return "wait", "", "", "t"
            if quiet > cfg["done_after_s"]:
                return "done", "", "", "t"
        return "run", "", "", "t"
    # stale
    if session.last_assistant_text.rstrip().endswith("?"):
        return "wait", "", "", "m"
    if session.last_role == "assistant":
        return "done", "", "", "m"
    return "idle", "", "", "m"


def _elapsed(session, st, now, hooky=False):
    """The two-timer contract (Item 4), computed at read time — el never
    jumps except at the defined edges:

      run/tool  now − turn start   (UserPromptSubmit edge / last user msg)
      wait      now − turn end     (Stop or perm-prompt edge / last event)
      done      turn end − turn start, frozen by the sticky-done latch
      idle      0
    """
    if st == "wait":
        # WAITING timer: how long it's been waiting on the user, never
        # turn length. Hook edges pin the exact turn end; the newest
        # applicable timestamp is when the waiting actually began.
        cands = [session.last_event_ts]
        if hooky:
            cands += [session.turn_stopped_at, session.perm_prompt_at]
        cands = [t for t in cands if t]
        return max(0, int(now - max(cands))) if cands else 0
    if st == "idle":
        return 0
    start = (session.turn_started_at if hooky and session.turn_started_at
             else session.turn_start)
    if not start:
        return 0
    if st in ("run", "tool"):
        return max(0, int(now - start))    # WORKING timer, ticking
    # done: frozen turn duration; the Stop edge is the exact end when we
    # have it, the last transcript event otherwise
    end = None
    if hooky and session.turn_stopped_at and session.turn_stopped_at > start:
        end = session.turn_stopped_at
    end = end or session.last_event_ts or now
    return max(0, int(end - start))
