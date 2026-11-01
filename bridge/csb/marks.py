"""User marks from the web board: seen, snooze, note. One small JSON file
in the data dir, applied to Session objects each step so both consumers
(serial packet `ur`, board lanes) read the same answer.

    seen    {sid: reply_ts}   the reply the user acknowledged. A newer
                              reply has a newer ts, so an ack expires by
                              itself; undo is deleting the entry.
    snooze  {sid: until_ts}   a Needs-you card muted until then (board
                              counts and sort only; the LCD still breathes)
    notes   {sid: text}       one free-text line, "#tags" inside it feed
                              the board's tag filter

Entries older than PRUNE_AFTER_S are dropped on save: session ids change
on /clear and processes exit, so the file never grows past the fleet.
"""

import json
import math
import os
import time

PRUNE_AFTER_S = 30 * 86400
NOTE_MAX = 200
SNOOZE_MAX_S = 24 * 3600


class Marks:
    def __init__(self, path):
        self.path = path
        self.seen = {}        # sid -> reply_ts
        self.snooze = {}      # sid -> until_ts
        self.notes = {}       # sid -> text
        self._touched = {}    # sid -> last write ts (for pruning)
        self._load()

    # ---- persistence ----
    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(d, dict):
            return
        for key, store, typ in (("seen", self.seen, float),
                                ("snooze", self.snooze, float),
                                ("notes", self.notes, str)):
            v = d.get(key)
            if isinstance(v, dict):
                for sid, val in v.items():
                    try:
                        val = typ(val)
                    except (TypeError, ValueError):
                        continue
                    # a non-finite stamp would reach the board as
                    # `Infinity`, which JSON.parse rejects: the page dies
                    if typ is float and not math.isfinite(val):
                        continue
                    store[str(sid)] = val
        t = d.get("touched")
        if isinstance(t, dict):
            for sid, ts in t.items():
                try:
                    self._touched[str(sid)] = float(ts)
                except (TypeError, ValueError):
                    continue

    def _save(self, now=None):
        now = time.time() if now is None else now
        cutoff = now - PRUNE_AFTER_S
        for sid, ts in list(self._touched.items()):
            if ts < cutoff:
                for store in (self.seen, self.snooze, self.notes,
                              self._touched):
                    store.pop(sid, None)
        doc = {"seen": self.seen, "snooze": self.snooze,
               "notes": self.notes, "touched": self._touched}
        tmp = f"{self.path}.{os.getpid()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, self.path)
        except OSError:
            pass                      # the board must not depend on it

    def _touch(self, sid, now):
        self._touched[sid] = now

    # ---- seen ----
    def mark_seen(self, sid, reply_ts, now=None):
        """Seen is monotonic: a late Seen for an OLDER reply (second tab,
        slow request) must not un-ack a newer one. Only unsee() clears."""
        now = time.time() if now is None else now
        ts = float(reply_ts or now)
        if not math.isfinite(ts):
            return
        self.seen[sid] = max(self.seen.get(sid, 0.0), ts)
        self._touch(sid, now)
        self._save(now)

    def unsee(self, sid, now=None):
        if self.seen.pop(sid, None) is not None:
            self._save(now)

    # ---- snooze ----
    def snooze_until(self, sid, until, now=None):
        now = time.time() if now is None else now
        until = min(float(until), now + SNOOZE_MAX_S)
        self.snooze[sid] = until
        self._touch(sid, now)
        self._save(now)

    def unsnooze(self, sid, now=None):
        if self.snooze.pop(sid, None) is not None:
            self._save(now)

    # ---- notes ----
    def set_note(self, sid, text, now=None):
        now = time.time() if now is None else now
        text = " ".join(str(text or "").split())[:NOTE_MAX]
        if text:
            self.notes[sid] = text
        else:
            self.notes.pop(sid, None)
        self._touch(sid, now)
        self._save(now)

    # ---- apply ----
    def apply(self, session, now=None):
        """Copy this session's marks onto it (cheap dict lookups; called
        every step so a mark set from any path is visible next derive)."""
        now = time.time() if now is None else now
        sid = session.session_id
        session.seen_ts = self.seen.get(sid, 0.0)
        until = self.snooze.get(sid, 0.0)
        session.snooze_until = until if until > now else 0.0
        session.note = self.notes.get(sid, "")
