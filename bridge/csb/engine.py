"""State derivation for a session: one function, computed fresh at every
packet build from parsed transcript facts (read-time decay — no stored
mutable state that rots).

Wave 1 pins today's behavior exactly; later waves add evidence tiers
(hook edges, rate limits) on top of this structure.
"""

import time
from collections import namedtuple

# st: run|tool|wait|idle|done (frozen serial contract)
# tl: raw tool name ("" when none) — prettified at packet time
# td: one-line tool detail
# el: elapsed seconds for the display timer
StateResult = namedtuple("StateResult", "st tl td el")

# Orchestration tools legitimately run for minutes with no writes to
# the main transcript (subagents write elsewhere) - never read them
# as permission prompts. Active subagent files are proof of work.
LONG_TOOLS = ("Task", "Agent", "Workflow", "TaskOutput", "Monitor")


def derive(session, cfg, now=None):
    """-> StateResult for `session` at time `now` (injected for tests)."""
    if now is None:
        now = time.time()
    st, tool, detail = _state(session, cfg, now)
    return StateResult(st, tool, detail, _elapsed(session, st, now))


def _state(session, cfg, now):
    mtime = session.mtime()
    fresh = (now - mtime) < cfg["idle_after_s"]

    pending = None
    if session.pending_ids:
        pending = sorted(session.pending_ids.values(), key=lambda v: v[1])[-1]

    if pending and pending[0] == "AskUserQuestion":
        # Claude explicitly asked a question - waiting, no threshold
        return "wait", "Question", pending[2]

    if pending and (pending[0] in LONG_TOOLS
                    or session.subagent_count(cfg, now) > 0):
        return "tool", pending[0], pending[2]

    if fresh:
        if pending:
            name, pts, detail = pending
            if now - pts > cfg["wait_tool_s"] and now - mtime > cfg["wait_tool_s"]:
                return "wait", name, detail   # likely a permission prompt
            return "tool", name, detail
        # no pending tools: if the last thing was an assistant message and
        # nothing new has been written for a while, the turn is over.
        # A message ending in "?" flips to wait on a shorter fuse.
        if session.last_role == "assistant":
            quiet = now - mtime
            asks = session.last_assistant_text.rstrip().endswith("?")
            if asks and quiet > cfg["question_after_s"]:
                return "wait", "", ""
            if quiet > cfg["done_after_s"]:
                return "done", "", ""
        return "run", "", ""
    # stale
    if pending:
        return "wait", pending[0], pending[2]
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
