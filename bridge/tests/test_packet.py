"""Backward-compat tripwire for the serial packet: every firmware-consumed
field present with the right type, plus display-window filtering, session
ordering, and act auto-follow selection. The firmware in
firmware/claude_statusbar/claude_statusbar.ino parses exactly these keys —
renames/removals here brick the display."""

import tempfile
import unittest

from csb.core import build_packet
from csb.engine import derive

from tests.helpers import (MODEL, T0, api_error, assistant_text,
                           assistant_tool_use, base_cfg, make_session, noise,
                           seed_model_cache, stub_usage, tool_result, user)

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
        self.assertIsInstance(pkt["hid"], int)     # additive overflow count
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

    def test_pm_badge_on_the_wire(self):
        for mode, wire in (("plan", "plan"), ("acceptEdits", "acceptEdits"),
                           ("bypassPermissions", "bypassPermissions"),
                           ("default", ""), ("superYolo", ""), (None, "")):
            rec = user(T0)
            if mode is not None:
                rec["permissionMode"] = mode
            s = self.make([rec, assistant_text(T0 + 1)],
                          mtime=T0 + 1, first_seen=T0)
            e = self.build([s], T0 + 5)["ses"][0]
            self.assertEqual(e["pm"], wire, mode)

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


class TestCuration(PacketCase):
    """Capacity by curation: wait first, then run/tool, then done, then
    idle; only collapsed idle/done overflow is counted in hid."""

    def wait_s(self, first_seen):     # pending Bash 39s -> wait at T0+40
        return self.make([user(T0), assistant_tool_use(T0 + 1, "Bash",
                                                       f"tu_{first_seen}")],
                         mtime=T0 + 1, first_seen=first_seen)

    def run_s(self, first_seen):      # fresh assistant, quiet < done_after_s
        return self.make([user(T0), assistant_text(T0 + 35)],
                         mtime=T0 + 35, first_seen=first_seen)

    def done_s(self, first_seen):     # quiet > done_after_s
        return self.make([user(T0), assistant_text(T0 + 1)],
                         mtime=T0 + 1, first_seen=first_seen)

    def idle_s(self, first_seen):     # stale, last=user
        return self.make([user(T0 - 130)], mtime=T0 - 130,
                         first_seen=first_seen)

    def test_hid_zero_without_overflow(self):
        pkt = self.build([self.run_s(T0)], T0 + 40)
        self.assertEqual(pkt["hid"], 0)
        self.assertEqual(self.build([], T0)["hid"], 0)

    def test_wait_and_run_survive_idle_done_collapse(self):
        self.cfg["max_sessions"] = 3
        now = T0 + 40
        ses = [self.idle_s(T0), self.done_s(T0 + 2), self.wait_s(T0 + 3),
               self.run_s(T0 + 4), self.done_s(T0 + 5), self.wait_s(T0 + 6)]
        pkt = self.build(ses, now)
        # waits + run kept, first_seen order preserved
        self.assertEqual([e["st"] for e in pkt["ses"]],
                         ["wait", "run", "wait"])
        self.assertEqual(pkt["hid"], 3)          # 2 done + 1 idle collapsed

    def test_among_equals_newest_first_seen_stay(self):
        self.cfg["max_sessions"] = 3
        ses = [self.run_s(T0 + i) for i in range(5)]
        for i, s in enumerate(ses):
            s.name = f"r{i}"
        pkt = self.build(ses, T0 + 40)
        self.assertEqual([e["nm"] for e in pkt["ses"]], ["r2", "r3", "r4"])
        self.assertEqual(pkt["hid"], 0)          # hidden runs are not idle/done

    def test_done_outranks_idle(self):
        self.cfg["max_sessions"] = 1
        pkt = self.build([self.idle_s(T0), self.done_s(T0 + 1)], T0 + 40)
        self.assertEqual([e["st"] for e in pkt["ses"]], ["done"])
        self.assertEqual(pkt["hid"], 1)

    def test_limited_wait_gets_a_cell_over_done(self):
        self.cfg["max_sessions"] = 1
        reset = int(T0 + 3600)
        limited = self.make([user(T0),
                             tool_result(T0 + 2, "tu_x",
                                         content=f"limit reached|{reset}")],
                            mtime=T0 + 2, first_seen=T0)
        pkt = self.build([limited, self.done_s(T0 + 1)], T0 + 40)
        self.assertEqual(pkt["ses"][0]["lim"], reset)
        self.assertEqual(pkt["hid"], 1)


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

    def test_rate_limited_wire_mapping(self):
        # §e: st=wait but tl="" (no "approve:" prefix), at=false (banner
        # dark), countdown in td, additive lim carries the reset epoch
        reset = int(T0 + 3600)
        s = self.make([user(T0), assistant_text(T0 + 1),
                       tool_result(T0 + 2, "tu_x",
                                   content=f"limit reached|{reset}")],
                      mtime=T0 + 2, first_seen=T0)
        pkt = self.build([s], T0 + 10)
        e = pkt["ses"][0]
        self.assertEqual(e["st"], "wait")
        self.assertEqual(e["tl"], "")
        self.assertEqual(e["td"], "rate limit · 59m")
        self.assertFalse(e["at"])
        self.assertEqual(e["lim"], reset)
        self.assertIn(e["st"], ST_VALUES)          # no new st value on wire

    def test_error_wire_mapping_keeps_at_true(self):
        s = self.make([user(T0), api_error(T0 + 1, error="authentication_failed")],
                      mtime=T0 + 1, first_seen=T0)
        e = self.build([s], T0 + 5)["ses"][0]
        self.assertEqual((e["st"], e["tl"], e["td"]),
                         ("wait", "", "error · auth failed"))
        self.assertTrue(e["at"])                   # an auth failure is actionable
        self.assertEqual(e["lim"], 0)

    def test_lim_zero_on_ordinary_sessions(self):
        s = self.make([user(T0), assistant_text(T0 + 1)],
                      mtime=T0 + 1, first_seen=T0)
        self.assertEqual(self.build([s], T0 + 5)["ses"][0]["lim"], 0)

    def test_limited_and_error_do_not_hijack_act(self):
        # a limited session must not pin auto-follow for its countdown —
        # the genuine approval-wait appearing later wins act
        reset = int(T0 + 3600)
        limited = self.make([user(T0),
                             tool_result(T0 + 2, "tu_x",
                                         content=f"limit reached|{reset}")],
                            mtime=T0 + 2, first_seen=T0)
        errored = self.make([user(T0), api_error(T0 + 1, error="authentication_failed")],
                            mtime=T0 + 1, first_seen=T0 + 1)
        approval = self.make([user(T0), assistant_tool_use(T0 + 1, "Bash", "tu_1")],
                             mtime=T0 + 1, first_seen=T0 + 2)
        pkt = self.build([limited, errored, approval], T0 + 25)
        self.assertEqual([e["st"] for e in pkt["ses"]],
                         ["wait", "wait", "wait"])
        self.assertEqual(pkt["act"], 2)            # the genuine approval wins

    def test_limited_alone_can_be_act_via_mtime_fallback(self):
        reset = int(T0 + 3600)
        limited = self.make([user(T0),
                             tool_result(T0 + 2, "tu_x",
                                         content=f"limit reached|{reset}")],
                            mtime=T0 + 20, first_seen=T0)
        running = self.make([user(T0), assistant_text(T0 + 1)],
                            mtime=T0 + 1, first_seen=T0 + 1)
        pkt = self.build([limited, running], T0 + 25)
        self.assertEqual(pkt["act"], 0)            # ordinary recency fallback

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
