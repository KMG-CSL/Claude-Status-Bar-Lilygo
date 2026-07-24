"""Stable slot identity (BACKLOG "LHS fleet minimap", decided 2026-07-24).

A session is assigned a slot index (rendered as letter A..H) the first
time it is displayed and keeps that slot for its lifetime — observed
live, the hiring session drifted B->A between packets, and "long-press B"
must never mean someone else. The bridge assigns; displays render what
they are told (additive ses[] field "sl").

Assignment rule, in order:

  1. Fresh slot first: the lowest index not held by ANY remembered
     session (pruned entries make their index fresh again).
  2. Only when no fresh slot exists, reuse a freed slot — one whose
     holder is not in the current display set. Freed slots are reused
     stalest-holder first (the letter that has been off-screen the
     longest, judged by the holder's last-seen time), lowest index on
     ties; the displaced holder's entry is evicted. A slot held by a
     currently-displayed session is never taken.

Assignments persist across bridge restarts in data_dir()/slots.json,
keyed by session_id; entries unseen for PRUNE_AFTER_S (7 days) are
pruned. The file is written on structural changes (new assignment,
eviction, prune) and at most every SAVE_EVERY_S otherwise, so the 1 Hz
packet loop does not grind the disk just to refresh last-seen times.
"""

import json
import os
import time

PRUNE_AFTER_S = 7 * 86400
SAVE_EVERY_S = 60.0


class SlotAllocator:
    def __init__(self, path=None, num_slots=8):
        self.path = path                # None = in-memory only (tests, one-offs)
        self.num_slots = num_slots
        self.entries = {}               # session_id -> {"slot": int, "ts": last-seen}
        self._last_save = 0.0
        self._load()

    def assign(self, session_ids, now=None):
        """-> {session_id: slot} for the display set, in one pass:
        prune stale entries, keep existing assignments, allocate for
        newcomers per the module rule, persist if anything changed."""
        if now is None:
            now = time.time()
        current = set(session_ids)
        changed = self._prune(current, now)
        out = {}
        for sid in session_ids:
            e = self.entries.get(sid)
            if e is None:
                e = self.entries[sid] = {"slot": self._new_slot(current),
                                         "ts": now}
                changed = True
            else:
                e["ts"] = now
            out[sid] = e["slot"]
        if changed or now - self._last_save > SAVE_EVERY_S:
            self._save(now)
        return out

    def _prune(self, current, now):
        cutoff = now - PRUNE_AFTER_S
        changed = False
        for sid in list(self.entries):
            if self.entries[sid]["ts"] < cutoff and sid not in current:
                del self.entries[sid]
                changed = True
        return changed

    def _new_slot(self, current):
        held = {e["slot"] for e in self.entries.values()}
        for i in range(self.num_slots):
            if i not in held:
                return i                       # fresh: never (still) held
        # no fresh slot: reuse the freed slot whose holder has been
        # off-screen the longest (lowest index on ties), evicting it.
        # Holders in the current display set are untouchable.
        freed = sorted((e["ts"], e["slot"], sid)
                       for sid, e in self.entries.items()
                       if sid not in current)
        if not freed:
            # more displayed sessions than slots (misconfigured
            # max_sessions > num_slots): overflow shares the last slot
            return self.num_slots - 1
        _ts, slot, victim = freed[0]
        del self.entries[victim]
        return slot

    # ---- persistence ----
    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for sid, e in data.items():
                slot, ts = int(e.get("slot", -1)), float(e.get("ts", 0))
                if 0 <= slot < self.num_slots:
                    self.entries[str(sid)] = {"slot": slot, "ts": ts}
        except Exception:
            pass                    # unreadable file: start fresh, overwrite

    def _save(self, now):
        self._last_save = now
        if not self.path:
            return
        try:
            tmp = f"{self.path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.entries, f)
            os.replace(tmp, self.path)
        except OSError:
            pass
