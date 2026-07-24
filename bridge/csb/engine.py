"""State derivation for a session: one function, computed fresh at every
packet build from parsed transcript facts (read-time decay — no stored
mutable state that rots).

Wave 1 pins today's behavior exactly; later waves add evidence tiers
(hook edges, rate limits) on top of this structure.
"""

import time
from collections import namedtuple

from .fmt import fmt_countdown
from .limits import is_expired

# st:  run|tool|wait|idle|done (frozen serial contract)
# tl:  raw tool name ("" when none) — prettified at packet time
# td:  one-line tool detail
# el:  elapsed seconds for the display timer
# lim: unix epoch the rate limit lifts (0 when not limited)
# err: short API-error reason ("" when none)
StateResult = namedtuple("StateResult", "st tl td el lim err",
                         defaults=(0, ""))

# Orchestration tools legitimately run for minutes with no writes to
# the main transcript (subagents write elsewhere) - never read them
# as permission prompts. Active subagent files are proof of work.
LONG_TOOLS = ("Task", "Agent", "Workflow", "TaskOutput", "Monitor")


def derive(session, cfg, now=None):
    """-> StateResult for `session` at time `now` (injected for tests)."""
    if now is None:
        now = time.time()

    # Step 1 (Item 5): explicit rate-limit / API-error facts beat every
    # inference. tl stays "" on purpose — the firmware renders a non-empty
    # tl on st=wait as "approve: <tl>", which would ask the user to approve
    # a rate limit. The countdown/reason lives in td; is_expired guards
    # against ever showing a past-epoch banner.
    if session.limit_reset and not is_expired(session.limit_reset, now):
        td = "rate limit · " + fmt_countdown(session.limit_reset - now,
                                             now=now)
        return StateResult("wait", "", td, _elapsed(session, "wait", now),
                           int(session.limit_reset), "")
    if session.error:
        return StateResult("wait", "", ("error · " + session.error)[:32],
                           _elapsed(session, "wait", now), 0, session.error)

    st, tool, detail = _state(session, cfg, now)
    return StateResult(st, tool, detail, _elapsed(session, st, now))


def _state(session, cfg, now):
    # Real parsed events are the activity clock; mtime is only the fallback
    # for transcripts that have produced no events yet. Noise records
    # (file-history-snapshot & friends, skipped in session.py) bump mtime
    # without meaning anything — Extra A.
    activity = session.last_event_ts or session.mtime()
    fresh = (now - activity) < cfg["idle_after_s"]

    pending = None
    if session.pending_ids:
        pending = sorted(session.pending_ids.values(), key=lambda v: v[1])[-1]

    if pending and pending[0] == "AskUserQuestion":
        # Claude explicitly asked a question - waiting, no threshold
        return "wait", "Question", pending[2]

    if pending and (pending[0] in LONG_TOOLS
                    or session.subagent_count(cfg, now) > 0):
        return "tool", pending[0], pending[2]

    if pending:
        name, pts, detail = pending
        # write-silence approval heuristic (Item 2): Claude Code writes
        # nothing to the main transcript while a tool executes, so silence
        # after a tool_use means execution *or* a permission prompt; the
        # debounce keeps allowlisted long tools from flashing "approval".
        silence = now - max(pts, activity)
        thr = cfg.get("approval_silence_s", cfg.get("wait_tool_s", 20))
        if not fresh or silence > thr:
            return "wait", name, detail   # likely a permission prompt
        return "tool", name, detail

    if fresh:
        # no pending tools: if the last thing was an assistant message and
        # nothing new has been written for a while, the turn is over.
        # A message ending in "?" flips to wait on a shorter fuse.
        if session.last_role == "assistant":
            quiet = now - activity
            asks = session.last_assistant_text.rstrip().endswith("?")
            if asks and quiet > cfg["question_after_s"]:
                return "wait", "", ""
            if quiet > cfg["done_after_s"]:
                return "done", "", ""
        return "run", "", ""
    # stale
    if session.last_assistant_text.rstrip().endswith("?"):
        return "wait", "", ""
    if session.last_role == "assistant":
        return "done", "", ""
    return "idle", "", ""


def _elapsed(session, st, now):
    if st == "wait":
        # show how long it's been waiting on the user, not turn length
        return max(0, int(now - (session.last_event_ts or now)))
    if session.turn_start:
        end = session.last_event_ts or now
        if st in ("run", "tool"):
            end = now
        return max(0, int(end - session.turn_start))
    return 0
