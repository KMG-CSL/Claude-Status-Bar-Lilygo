"""Attention model: the browser board's lanes and cards, derived at read
time from the same Session + StateResult the serial packet uses (one
engine, two consumers — no second copy of session state).

Lanes, in priority order, assigned automatically (no manual curation):

  needs    an actionable wait: permission / plan / question / error
  limited  rate-limited (waiting, but nothing for the user to do)
  review   finished, and the user has not prompted it since ("unread")
  running  working — including "done" turns still delegating to agents
  done     finished and already seen
  parked   quiet for parked_after_s (keeps its unread badge)

"Unread" is durable without any store: a settled turn whose last reply
is newer than the user's last HUMAN prompt (origin.kind, so peer
messages and task notifications don't count as the user looking). It
survives bridge restarts because both timestamps come from the
transcript.
"""

import socket
import time

LANES = ("needs", "limited", "review", "running", "done", "parked")

DEFAULT_PARKED_AFTER_S = 4 * 3600

# delegated Task ids with no live worker are trusted this long after the
# turn ended (a worker's first transcript write can lag its launch); past
# it, an id whose tool_result never came (interrupted, crashed worker) no
# longer pins the session in Running
DELEGATION_GRACE_S = 120


def is_settled(session, r, cfg, now):
    """Done AND nothing still working on its behalf: no live subagents,
    and no delegated Task-family calls still within their grace. A parent
    whose workers are running is not done yet — no chime, no review."""
    if r.st != "done" or session.subagent_count(cfg, now) > 0:
        return False
    for tid in list(session.task_ids):
        since = session.task_since.get(tid)
        if since is None:
            since = session.task_since[tid] = now
        if now - since < DELEGATION_GRACE_S:
            return False
        # no worker alive past its grace: retire it for good, so a later
        # turn's Stop cannot restart its clock
        del session.task_ids[tid]
        del session.task_since[tid]
    return True


def is_unread(session, r, cfg, now):
    if not is_settled(session, r, cfg, now) or r.fin == "cancel":
        return False              # Escape is the user acting: seen
    reply = session.last_reply_ts or session.last_event_ts or 0
    return reply > (session.last_prompt_ts or 0)


def activity_ts(session):
    return max(session.last_event_ts or 0, session.hook_last or 0) or \
        session.mtime()


def need_kind(session, r):
    if r.err:
        return "error"
    if r.tl in ("ExitPlanMode",):
        return "plan"
    if r.tl in ("Question", "AskUserQuestion") or (
            not r.tl and session.last_assistant_text.rstrip().endswith("?")
            and session.perm_prompt_at is None):
        return "question"
    return "permission"


def lane_of(session, r, cfg, now):
    if r.st == "wait":
        return "limited" if r.lim else "needs"
    if r.st in ("run", "tool"):
        return "running"
    if r.st == "done" and not is_settled(session, r, cfg, now):
        return "running"          # delegating: the turn's work continues
    quiet = now - activity_ts(session)
    if quiet > cfg.get("parked_after_s", DEFAULT_PARKED_AFTER_S):
        return "parked"
    return "review" if is_unread(session, r, cfg, now) else "done"


def chime_state(session, r, cfg, now):
    """What the chimer should see: a done that is not settled is still
    working, and a done the user already saw (cancel) is not news. So a
    parent with subagents chimes once — when the last worker finishes."""
    if r.st != "done":
        return r.st
    if not is_settled(session, r, cfg, now):
        return "tool"
    return "done" if is_unread(session, r, cfg, now) else "idle"


def card(session, r, entry, lane, cfg, now):
    reg = session.reg or {}
    subs = [{"type": t or "agent", "age": int(now - t0)}
            for t, t0 in session.subagents.values()]
    return {
        "sid": session.session_id,
        "slot": entry.get("sl"),
        "lane": lane,
        "need": need_kind(session, r) if lane == "needs" else "",
        "unread": is_unread(session, r, cfg, now),
        "title": session.title(),
        "ai_title": session.ai_title,
        "project": session.project,
        "cwd": session.cwd or reg.get("cwd") or "",
        "model": entry.get("md", ""),
        "ctx": entry.get("cx", 0),
        "effort": entry.get("ef", ""),
        "pm": entry.get("pm", ""),
        "st": r.st,
        "tool": entry.get("tl", ""),
        "detail": r.td,
        "el": r.el,
        "fin": r.fin,
        "lim": int(r.lim),
        "err": r.err,
        "src": r.src,
        "task": session.current_task,
        "prompt": session.last_prompt,
        "prompt_ts": session.last_prompt_ts,
        "reply": session.last_assistant_text,
        "reply_ts": session.last_reply_ts,
        "activity": activity_ts(session),
        "subagents": subs,
        "sa": entry.get("sa", 0),
        "delegating": len(session.task_ids),
        "peers": [{"dir": d, "name": n, "ts": t, "gist": g}
                  for d, n, t, g in reversed(session.peers)],
        "pid": reg.get("pid") or session.claude_pid or 0,
        "kind": reg.get("kind") or "",
        "started": (reg.get("startedAt") or 0) / 1000.0,
    }


_SORT = {
    "needs": lambda c: -c["el"],                  # longest waiting first
    "limited": lambda c: c["lim"],                # soonest reset first
    "review": lambda c: -(c["reply_ts"] or 0),    # newest finish first
    "running": lambda c: -c["el"],
    "done": lambda c: -c["activity"],
    "parked": lambda c: -c["activity"],
}


def build_board(live, states, entries, cfg, now=None, focus=False):
    """-> board document for the web view. `entries` are the per-session
    wire dicts (slot letters, pretty model, ctx %) so the two consumers
    agree on every number they share."""
    if now is None:
        now = time.time()
    lanes = {k: [] for k in LANES}
    for s, r, e in zip(live, states, entries):
        ln = lane_of(s, r, cfg, now)
        lanes[ln].append(card(s, r, e, ln, cfg, now))
    for k in LANES:
        lanes[k].sort(key=_SORT[k])
    return {
        "ts": now,
        "host": socket.gethostname().split(".")[0],
        "focus": bool(focus),
        "lanes": lanes,
        "counts": {k: len(v) for k, v in lanes.items()},
    }
