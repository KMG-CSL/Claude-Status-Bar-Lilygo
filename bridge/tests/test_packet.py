"""Backward-compat tripwire for the serial packet: every firmware-consumed
field present with the right type, plus display-window filtering, session
ordering, and act auto-follow selection. The firmware in
firmware/claude_statusbar/claude_statusbar.ino parses exactly these keys —
renames/removals here brick the display."""

import tempfile
import unittest

from csb.core import build_packet
from csb.engine import derive

from tests.helpers import (MODEL, T0, assistant_text, assistant_tool_use,
                           base_cfg, make_session, noise, seed_model_cache,
                           stub_usage, user)

SES_KEYS = ("pj", "nm", "md", "st", "tl", "td", "ef", "tk", "sa", "el",
            "ti", "to", "cx", "at")
US_KEYS = ("p5", "p7", "r5", "r7", "est")
ST_VALUES = {"run", "tool", "wait", "idle", "done"}


class PacketCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = base_cfg()
        self.usage = stub_usage(self.cfg)
        seed_model_cache()
        self._n = 0

    def make(self, records, mtime, first_seen):
        self._n += 1
        s = make_session(self.tmp.name, records, mtime=mtime,
                         filename=f"ses-{self._n}.jsonl")
        s.first_seen = first_seen
        return s

    def build(self, sessions, now):
        return build_packet({s.path: s for s in sessions}, self.cfg,
                            self.usage, now=now)


class TestFieldContract(PacketCase):
    def test_firmware_consumed_fields(self):
        now = T0 + 41                              # quiet 31s -> done
        us = {"input_tokens": 10000, "output_tokens": 500,
              "cache_read_input_tokens": 30000}
        s = self.make([user(T0), assistant_text(T0 + 10, usage=us)],
                      mtime=T0 + 10, first_seen=T0)
        pkt = self.build([s], now)

        self.assertEqual(pkt["t"], "s")
        self.assertIsInstance(pkt["act"], int)
        for k in US_KEYS:
            self.assertIn(k, pkt["us"])
        self.assertIsInstance(pkt["us"]["est"], bool)

        self.assertEqual(len(pkt["ses"]), 1)
        e = pkt["ses"][0]
        for k in SES_KEYS:
            self.assertIn(k, e)
        self.assertIn(e["st"], ST_VALUES)
        for k, typ in (("pj", str), ("nm", str), ("md", str), ("st", str),
                       ("tl", str), ("td", str), ("ef", str), ("tk", str),
                       ("sa", int), ("el", int), ("ti", int), ("to", int),
                       ("cx", int), ("at", bool)):
            self.assertIsInstance(e[k], typ, k)

        self.assertEqual(e["pj"], "widget")
        self.assertEqual(e["md"], "Fable 5")
        self.assertEqual(e["st"], "done")          # quiet > done_after_s
        self.assertEqual(e["el"], 10)              # frozen turn duration
        self.assertEqual(e["at"], False)
        self.assertEqual(e["tk"], "40k")           # ctx 40000 -> "40k"
        self.assertEqual(e["cx"], 20)              # 40000 / 200000 seeded limit
        self.assertEqual(e["ti"], 10000)
        self.assertEqual(e["to"], 500)

    def test_at_mirrors_wait_and_lengths_capped(self):
        s = self.make([user(T0),
                       assistant_tool_use(T0 + 1, "Bash", "tu_1",
                                          {"command": "x" * 100})],
                      mtime=T0 + 1, first_seen=T0)
        pkt = self.build([s], T0 + 30)             # pending 29s -> wait
        e = pkt["ses"][0]
        self.assertEqual(e["st"], "wait")
        self.assertTrue(e["at"])
        self.assertLessEqual(len(e["td"]), 32)
        self.assertLessEqual(len(e["pj"]), 20)
        self.assertLessEqual(len(e["nm"]), 56)

    def test_packet_matches_single_derive(self):
        # to_packet(state=...) and a fresh derive at the same now agree
        s = self.make([user(T0), assistant_tool_use(T0 + 1, "Bash", "tu_1")],
                      mtime=T0 + 1, first_seen=T0)
        now = T0 + 5
        pkt = self.build([s], now)
        r = derive(s, self.cfg, now)
        e = pkt["ses"][0]
        self.assertEqual(e["st"], r.st)
        self.assertEqual(e["el"], r.el)


class TestDisplayWindow(PacketCase):
    def test_active_window_filter(self):
        now = T0 + 30 * 60                       # active_window_min = 30
        inside = self.make([user(T0), assistant_text(T0 + 1)],
                           mtime=now - 1799, first_seen=T0)
        outside = self.make([user(T0), assistant_text(T0 + 1)],
                            mtime=now - 1801, first_seen=T0 + 1)
        pkt = self.build([inside, outside], now)
        self.assertEqual(len(pkt["ses"]), 1)
        self.assertEqual(pkt["ses"][0]["pj"], "widget")
        # boundary is strict '<': exactly 1800s old is out
        edge = self.make([user(T0), assistant_text(T0 + 1)],
                         mtime=now - 1800, first_seen=T0 + 2)
        self.assertEqual(len(self.build([edge], now)["ses"]), 0)

    def test_contentless_sessions_hidden(self):
        # no model and no real user turn (e.g. only noise records) -> not shown
        s = self.make([noise(T0)], mtime=T0, first_seen=T0)
        self.assertEqual(self.build([s], T0 + 10)["ses"], [])

    def test_max_sessions_keeps_newest_first_seen(self):
        now = T0 + 100
        sessions = [self.make([user(T0, text=f"task {i}"),
                               assistant_text(T0 + 1)],
                              mtime=T0 + 1, first_seen=T0 + i)
                    for i in range(9)]            # max_sessions = 8
        pkt = self.build(sessions, now)
        self.assertEqual(len(pkt["ses"]), 8)
        # oldest first_seen fell off; order is first_seen ascending
        self.assertEqual(pkt["ses"][0]["nm"], "task 1")
        self.assertEqual(pkt["ses"][-1]["nm"], "task 8")


class TestOrderingAndAct(PacketCase):
    def test_sessions_ordered_by_first_seen(self):
        now = T0 + 50
        a = self.make([user(T0, text="alpha task"), assistant_text(T0 + 1)],
                      mtime=T0 + 1, first_seen=T0 + 2)
        b = self.make([user(T0, text="beta task"), assistant_text(T0 + 1)],
                      mtime=T0 + 1, first_seen=T0 + 1)
        pkt = self.build([a, b], now)
        self.assertEqual([e["nm"] for e in pkt["ses"]],
                         ["beta task", "alpha task"])

    def test_act_prefers_first_waiting_session(self):
        now = T0 + 25
        running = self.make([user(T0), assistant_text(T0 + 24)],
                            mtime=T0 + 24, first_seen=T0)
        waiting1 = self.make([user(T0), assistant_tool_use(T0 + 1, "Bash", "tu_1")],
                             mtime=T0 + 1, first_seen=T0 + 1)
        waiting2 = self.make([user(T0), assistant_tool_use(T0 + 1, "Edit", "tu_2")],
                             mtime=T0 + 1, first_seen=T0 + 2)
        pkt = self.build([running, waiting1, waiting2], now)
        # ses[] is first_seen-ordered: running, waiting1, waiting2
        self.assertEqual([e["st"] for e in pkt["ses"]], ["run", "wait", "wait"])
        self.assertEqual(pkt["act"], 1)            # first waiting wins
        self.assertTrue(pkt["ses"][pkt["act"]]["at"])

    def test_act_falls_back_to_most_recent_mtime(self):
        now = T0 + 20
        older = self.make([user(T0), assistant_text(T0 + 1)],
                          mtime=T0 + 5, first_seen=T0)
        newer = self.make([user(T0), assistant_text(T0 + 1)],
                          mtime=T0 + 15, first_seen=T0 + 1)
        pkt = self.build([older, newer], now)
        self.assertEqual(pkt["act"], 1)

    def test_empty_packet_shape(self):
        pkt = self.build([], T0)
        self.assertEqual(pkt["t"], "s")
        self.assertEqual(pkt["ses"], [])
        self.assertEqual(pkt["act"], 0)
        for k in US_KEYS:
            self.assertIn(k, pkt["us"])
        self.assertTrue(pkt["us"]["est"])          # stubbed tracker estimates


if __name__ == "__main__":
    unittest.main()
