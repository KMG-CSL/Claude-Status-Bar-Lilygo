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
transcript. The one user-side input is the board's Seen mark
(csb.marks): a reply acknowledged there counts as seen until a newer
reply arrives.
"""

import re
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
    seen = max(session.last_prompt_ts or 0, getattr(session, "seen_ts", 0) or 0)
    return reply > seen


def is_snoozed(session, now):
    return (getattr(session, "snooze_until", 0) or 0) > now


def _newest_pending(session):
    """The pending_text entry of the newest unresolved tool_use: what the
    Needs-you card shows in full (command, path, question + options)."""
    ids = session.pending_ids
    if not ids:
        return None
    tid = max(ids, key=lambda k: ids[k][1])
    return getattr(session, "pending_text", {}).get(tid)


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


_REF = re.compile(r"\s*\[[^\]]*\]\s*$")


def split_traffic(session, live):
    """-> (peers, traffic). Peers are messages to/from OTHER live sessions
    in the registry (matched by pid, else by name with any " [ref]"
    suffix dropped); everything else — subagent handbacks, SendMessage to
    raw agent ids or "main", senders that have since exited — is the
    session's own worker traffic and goes with its subagents."""
    reg = session.reg or {}
    own_pid = reg.get("pid") or session.claude_pid or 0
    own_name = reg.get("name") or ""
    peers, traffic = [], []
    for d, name, ts, gist, pid in reversed(session.peers):
        bare = _REF.sub("", name)
        hit = False
        shown = name
        if live is not None:
            if pid:
                hit = pid in live["pids"] and pid != own_pid
                # registry name, else the board's own title for that pid
                # (an unnamed session must not surface as a socket path)
                shown = (live["pids"].get(pid)
                         or live.get("titles", {}).get(pid) or bare)
            else:
                hit = bare in live["names"] and bare != own_name
                shown = bare
                pid = live.get("by_name", {}).get(bare, 0)
        item = {"dir": d, "name": shown if hit else name, "ts": ts,
                "gist": gist, "pid": pid if hit else 0}
        (peers if hit else traffic).append(item)
    return peers, traffic


def card(session, r, entry, lane, cfg, now, live=None, jump=False):
    reg = session.reg or {}
    subs = [{"type": t or "agent", "age": int(now - t0)}
            for t, t0 in session.subagents.values()]
    peers, traffic = split_traffic(session, live)
    pend = _newest_pending(session) if lane == "needs" else None
    todos = getattr(session, "todos", [])
    done_n = sum(1 for _c, st in todos if st == "completed")
    seen_ts = getattr(session, "seen_ts", 0) or 0
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
        "peers": peers,
        "traffic": traffic,
        "pid": reg.get("pid") or session.claude_pid or 0,
        "jump": bool(jump),
        "kind": reg.get("kind") or "",
        "started": (reg.get("startedAt") or 0) / 1000.0,
        # detail (drawer) facts and user marks
        "branch": getattr(session, "git_branch", ""),
        "files": list(getattr(session, "turn_files", [])),
        "todos": [{"c": c, "s": st} for c, st in todos],
        "progress": f"{done_n}/{len(todos)}" if todos else "",
        "exchanges": [{"who": w, "ts": t, "text": x}
                      for w, t, x in getattr(session, "exchanges", [])],
        "pending": pend["text"] if pend else "",
        "pending_tool": pend["tool"] if pend else "",
        "options": list(pend["options"]) if pend else [],
        "snooze": int(session.snooze_until) if (
            lane == "needs" and is_snoozed(session, now)) else 0,
        "note": getattr(session, "note", ""),
        "seen_ts": seen_ts,
        # acked: the user marked THIS reply seen (so Done offers "unread")
        "acked": bool(seen_ts and session.last_reply_ts
                      and seen_ts >= session.last_reply_ts),
    }


_SORT = {
    "needs": lambda c: (1 if c.get("snooze") else 0, -c["el"]),  # snoozed last
    "limited": lambda c: c["lim"],                # soonest reset first
    "review": lambda c: -(c["reply_ts"] or 0),    # newest finish first
    "running": lambda c: -c["el"],
    "done": lambda c: -c["activity"],
    "parked": lambda c: -c["activity"],
}


def build_board(live, states, entries, cfg, now=None, focus=False,
                registry=None, can_jump=None):
    """-> board document for the web view. `entries` are the per-session
    wire dicts (slot letters, pretty model, ctx %) so the two consumers
    agree on every number they share. `registry` is the live registry
    ({sessionId: entry}, None without one) for peer matching; can_jump(s)
    says whether a focus adapter can actually act on that session."""
    peers_live = None
    if registry is not None:
        peers_live = {
            "pids": {e["pid"]: str(e.get("name") or "")
                     for e in registry.values()
                     if isinstance(e.get("pid"), int)},
            "names": {str(e.get("name")) for e in registry.values()
                      if e.get("name")},
            "by_name": {str(e.get("name")): e["pid"]
                        for e in registry.values()
                        if e.get("name") and isinstance(e.get("pid"), int)},
            # the board's own titles by pid: the chip for an unnamed
            # peer reads like its card, never like a socket path
            "titles": {(s.reg or {}).get("pid"): s.title()
                       for s in live if (s.reg or {}).get("pid")},
        }
    if now is None:
        now = time.time()
    lanes = {k: [] for k in LANES}
    for s, r, e in zip(live, states, entries):
        ln = lane_of(s, r, cfg, now)
        lanes[ln].append(card(s, r, e, ln, cfg, now, live=peers_live,
                              jump=bool(can_jump and can_jump(s))))
    for k in LANES:
        lanes[k].sort(key=_SORT[k])
    counts = {k: len(v) for k, v in lanes.items()}
    # a snoozed Needs-you stays in its lane (and on the LCD) but is out
    # of the headline count, the tab title and the favicon
    counts["snoozed"] = sum(1 for c in lanes["needs"] if c["snooze"])
    counts["needs"] -= counts["snoozed"]
    return {
        "ts": now,
        "host": socket.gethostname().split(".")[0],
        "focus": bool(focus),
        "lanes": lanes,
        "counts": counts,
    }
