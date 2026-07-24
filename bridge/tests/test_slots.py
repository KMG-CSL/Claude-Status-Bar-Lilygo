"""Stable slot identity (BACKLOG minimap decision): a session keeps its
letter for its lifetime; fresh slots first, freed slots reused stalest
first; assignments persist in slots.json and prune after 7 days."""

import json
import os
import tempfile
import unittest

from csb.slots import PRUNE_AFTER_S, SlotAllocator

from tests.helpers import (T0, assistant_text, base_cfg, make_session,
                           seed_model_cache, stub_usage, user)

DAY = 86400


class AllocCase(unittest.TestCase):
    def setUp(self):
        self.a = SlotAllocator()          # in-memory


class TestAssignment(AllocCase):
    def test_fresh_slots_lowest_index_first(self):
        self.assertEqual(self.a.assign(["s1", "s2", "s3"], now=T0),
                         {"s1": 0, "s2": 1, "s3": 2})

    def test_session_keeps_its_slot_for_its_lifetime(self):
        self.a.assign(["s1", "s2"], now=T0)
        # s1 vanishes, s3 arrives, s1 returns: everyone keeps their letter
        self.assertEqual(self.a.assign(["s2", "s3"], now=T0 + 10),
                         {"s2": 1, "s3": 2})
        self.assertEqual(self.a.assign(["s2", "s3", "s1"], now=T0 + 20),
                         {"s2": 1, "s3": 2, "s1": 0})

    def test_slot_survives_display_order_changes(self):
        # the observed live bug: hiring session drifted B->A between
        # packets. Input order must never move an assigned slot.
        self.a.assign(["x", "y"], now=T0)
        self.assertEqual(self.a.assign(["y", "x"], now=T0 + 1),
                         {"x": 0, "y": 1})

    def test_freed_slot_reused_only_when_no_fresh_slot_left(self):
        sids = [f"s{i}" for i in range(8)]
        self.a.assign(sids, now=T0)
        # s0 leaves; a newcomer still gets nothing until slots run out —
        # there are none fresh, so it reuses s0's freed slot 0
        got = self.a.assign(sids[1:] + ["new"], now=T0 + 10)
        self.assertEqual(got["new"], 0)
        # the displaced holder was evicted: s0 is a stranger now
        self.assertNotIn("s0", self.a.entries)

    def test_freed_reuse_prefers_stalest_holder(self):
        sids = [f"s{i}" for i in range(8)]
        self.a.assign(sids, now=T0)
        # s3 last seen T0+5, s6 last seen T0+9: s3 has been off-screen longer
        self.a.assign([s for s in sids if s != "s6"], now=T0 + 5)
        remaining = [s for s in sids if s not in ("s3", "s6")]
        self.a.assign(remaining + ["s6"], now=T0 + 9)
        got = self.a.assign(remaining + ["n1", "n2"], now=T0 + 20)
        self.assertEqual(got["n1"], 3)     # s3's slot: stalest holder first
        self.assertEqual(got["n2"], 6)     # then s6's

    def test_live_sessions_slot_is_never_stolen(self):
        sids = [f"s{i}" for i in range(8)]
        self.a.assign(sids, now=T0)
        got = self.a.assign(sids + ["extra"], now=T0 + 1)
        for sid in sids:
            self.assertEqual(got[sid], int(sid[1:]))
        # nothing to evict: overflow shares the last slot rather than
        # displacing a displayed session
        self.assertEqual(got["extra"], 7)


class TestPersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "slots.json")

    def test_assignments_survive_a_restart(self):
        a = SlotAllocator(self.path)
        a.assign(["s1", "s2"], now=T0)
        b = SlotAllocator(self.path)               # "bridge restart"
        self.assertEqual(b.assign(["s2", "s3", "s1"], now=T0 + 30),
                         {"s2": 1, "s3": 2, "s1": 0})

    def test_entries_prune_after_seven_days(self):
        a = SlotAllocator(self.path)
        a.assign(["old"], now=T0)
        a.assign(["young"], now=T0 + 2 * DAY)     # old off-screen, ts stays T0
        b = SlotAllocator(self.path)
        got = b.assign(["new"], now=T0 + PRUNE_AFTER_S + 1)
        # "old" (last seen 7d+ ago) pruned -> slot 0 fresh again;
        # "young" survives the prune window
        self.assertEqual(got, {"new": 0})
        self.assertIn("young", b.entries)
        self.assertNotIn("old", b.entries)

    def test_currently_displayed_session_is_never_pruned(self):
        a = SlotAllocator(self.path)
        a.assign(["s1"], now=T0)
        got = a.assign(["s1"], now=T0 + PRUNE_AFTER_S + DAY)
        self.assertEqual(got, {"s1": 0})

    def test_corrupt_file_starts_fresh(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not json")
        a = SlotAllocator(self.path)
        self.assertEqual(a.assign(["s1"], now=T0), {"s1": 0})

    def test_file_is_valid_json_keyed_by_session_id(self):
        a = SlotAllocator(self.path)
        a.assign(["sid-abc"], now=T0)
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["sid-abc"]["slot"], 0)
        self.assertEqual(data["sid-abc"]["ts"], T0)


class TestPacketField(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = base_cfg()
        self.usage = stub_usage(self.cfg)
        seed_model_cache()

    def make(self, n):
        out = []
        for i in range(n):
            s = make_session(self.tmp.name,
                             [user(T0, text=f"task {i}"), assistant_text(T0 + 1)],
                             mtime=T0 + 1, filename=f"sl-{i}.jsonl")
            s.first_seen = T0 + i
            out.append(s)
        return out

    def test_sl_is_additive_int_and_stable_across_packets(self):
        from csb.core import build_packet
        allocator = SlotAllocator()
        a, b = self.make(2)
        smap = {s.path: s for s in (a, b)}
        p1 = build_packet(smap, self.cfg, self.usage, now=T0 + 5,
                          slots=allocator)
        self.assertEqual([e["sl"] for e in p1["ses"]], [0, 1])
        # first session ages out of the window; b keeps slot 1
        a.mclock.value = T0 - 40 * 60
        p2 = build_packet(smap, self.cfg, self.usage, now=T0 + 6,
                          slots=allocator)
        self.assertEqual([e["sl"] for e in p2["ses"]], [1])
        self.assertIsInstance(p2["ses"][0]["sl"], int)

    def test_sl_present_without_an_explicit_allocator(self):
        from csb.core import build_packet
        (a,) = self.make(1)
        pkt = build_packet({a.path: a}, self.cfg, self.usage, now=T0 + 5)
        self.assertEqual(pkt["ses"][0]["sl"], 0)


if __name__ == "__main__":
    unittest.main()
