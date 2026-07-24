"""Baseline state-machine table: pins TODAY's derivation behavior at exact
threshold boundaries so later waves (approval flip rework, hook tiers,
limits) can't regress it silently.

Defaults under test: idle_after_s=120, wait_tool_s=20, done_after_s=30,
question_after_s=12. All clocks injected — no sleeping.
"""

import tempfile
import unittest

from csb.engine import derive

from tests.helpers import (T0, assistant_text, assistant_tool_use, base_cfg,
                           make_session, tool_result, user)


class EngineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = base_cfg()

    def make(self, records, mtime, **kw):
        return make_session(self.tmp.name, records, mtime=mtime, **kw)

    def at(self, session, now):
        return derive(session, self.cfg, now)


class TestPendingTool(EngineCase):
    """Fresh session with an unanswered non-long tool_use."""

    def _pending(self, tool_ts=T0 + 5):
        return self.make([user(T0), assistant_tool_use(tool_ts, "Bash", "tu_1")],
                         mtime=tool_ts)

    def test_young_pending_is_tool(self):
        s = self._pending()
        r = self.at(s, T0 + 5 + 19.9)
        self.assertEqual((r.st, r.tl, r.td), ("tool", "Bash", "npm test"))

    def test_pending_at_exact_threshold_is_tool(self):
        # both guards are strict '>' — 20.0 on the dot stays tool
        s = self._pending()
        self.assertEqual(self.at(s, T0 + 5 + 20.0).st, "tool")

    def test_old_pending_flips_to_wait(self):
        s = self._pending()
        r = self.at(s, T0 + 5 + 20.1)
        self.assertEqual((r.st, r.tl, r.td), ("wait", "Bash", "npm test"))

    def test_wait_needs_mtime_quiet_too(self):
        # tool_use is old but the file was written recently -> still tool
        s = self._pending()
        s.mclock.value = T0 + 5 + 15         # a later write bumped mtime
        self.assertEqual(self.at(s, T0 + 5 + 21).st, "tool")

    def test_answered_tool_runs(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1"),
                       tool_result(T0 + 24, "tu_1")], mtime=T0 + 24)
        self.assertEqual(self.at(s, T0 + 26).st, "run")

    def test_newest_pending_wins(self):
        s = self.make([user(T0),
                       assistant_tool_use(T0 + 1, "Read", "tu_1",
                                          {"file_path": "/a/b.py"}),
                       assistant_tool_use(T0 + 5, "Bash", "tu_2")],
                      mtime=T0 + 5)
        r = self.at(s, T0 + 6)
        self.assertEqual((r.st, r.tl), ("tool", "Bash"))


class TestSpecialTools(EngineCase):
    def test_ask_user_question_waits_instantly(self):
        s = self.make([user(T0),
                       assistant_tool_use(T0 + 1, "AskUserQuestion", "tu_q",
                                          {"questions": [{"question": "Ship it?"}]})],
                      mtime=T0 + 1)
        r = self.at(s, T0 + 1.1)             # no threshold at all
        self.assertEqual((r.st, r.tl, r.td), ("wait", "Question", "Ship it?"))

    def test_long_tools_never_wait_even_stale(self):
        for name in ("Task", "Agent", "Workflow", "TaskOutput", "Monitor"):
            s = self.make([user(T0), assistant_tool_use(T0 + 1, name, "tu_t", {})],
                          mtime=T0 + 1, filename=f"long-{name}.jsonl")
            r = self.at(s, T0 + 500)         # way past idle_after_s
            self.assertEqual((r.st, r.tl), ("tool", name), name)


class TestQuietAssistant(EngineCase):
    """Fresh session, no pending tools, assistant spoke last."""

    def _quiet(self, text="all done."):
        return self.make([user(T0), assistant_text(T0 + 10, text=text)],
                         mtime=T0 + 10)

    def test_recent_reply_is_run(self):
        s = self._quiet()
        self.assertEqual(self.at(s, T0 + 10 + 29.9).st, "run")

    def test_quiet_reply_is_done(self):
        s = self._quiet()
        self.assertEqual(self.at(s, T0 + 10 + 30.1).st, "done")

    def test_question_flips_to_wait_on_short_fuse(self):
        s = self._quiet(text="should I deploy?")
        self.assertEqual(self.at(s, T0 + 10 + 11.9).st, "run")
        r = self.at(s, T0 + 10 + 12.1)
        self.assertEqual((r.st, r.tl, r.td), ("wait", "", ""))

    def test_question_needs_the_fuse(self):
        # between done_after_s and question_after_s nothing odd happens:
        # a "?" text past done_after_s stays wait, not done
        s = self._quiet(text="should I deploy?")
        self.assertEqual(self.at(s, T0 + 10 + 31).st, "wait")

    def test_user_spoke_last_is_run(self):
        s = self.make([user(T0)], mtime=T0)
        self.assertEqual(self.at(s, T0 + 60).st, "run")


class TestStale(EngineCase):
    """mtime older than idle_after_s."""

    def test_freshness_boundary_is_strict(self):
        s = self.make([user(T0), assistant_text(T0 + 1, text="done.")],
                      mtime=T0)
        self.assertEqual(self.at(s, T0 + 119.9).st, "done")   # fresh, quiet>30
        self.assertEqual(self.at(s, T0 + 120.0).st, "done")   # stale, same answer
        # the run window closes exactly at idle_after_s:
        s2 = self.make([user(T0)], mtime=T0, filename="s2.jsonl")
        self.assertEqual(self.at(s2, T0 + 119.9).st, "run")
        self.assertEqual(self.at(s2, T0 + 120.0).st, "idle")

    def test_stale_pending_is_wait(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 1, "Edit", "tu_1",
                                                    {"file_path": "/a/b.py"})],
                      mtime=T0 + 1)
        r = self.at(s, T0 + 300)
        self.assertEqual((r.st, r.tl, r.td), ("wait", "Edit", "b.py"))

    def test_stale_question_is_wait(self):
        s = self.make([user(T0), assistant_text(T0 + 1, text="deploy?")],
                      mtime=T0 + 1)
        self.assertEqual(self.at(s, T0 + 300).st, "wait")

    def test_stale_assistant_is_done(self):
        s = self.make([user(T0), assistant_text(T0 + 1, text="done.")],
                      mtime=T0 + 1)
        self.assertEqual(self.at(s, T0 + 300).st, "done")

    def test_stale_user_is_idle(self):
        s = self.make([user(T0)], mtime=T0)
        r = self.at(s, T0 + 300)
        self.assertEqual((r.st, r.tl, r.td), ("idle", "", ""))


class TestElapsed(EngineCase):
    """el semantics per state — the display timer must not change meaning."""

    def test_run_counts_from_turn_start(self):
        s = self.make([user(T0), assistant_text(T0 + 10)], mtime=T0 + 10)
        self.assertEqual(self.at(s, T0 + 25).el, 25)

    def test_tool_counts_from_turn_start(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        self.assertEqual(self.at(s, T0 + 15).el, 15)

    def test_wait_counts_from_last_event(self):
        s = self.make([user(T0), assistant_text(T0 + 10, text="deploy?")],
                      mtime=T0 + 10)
        r = self.at(s, T0 + 10 + 13)
        self.assertEqual(r.st, "wait")
        self.assertEqual(r.el, 13)               # waiting time, not turn time

    def test_done_freezes_turn_duration(self):
        s = self.make([user(T0), assistant_text(T0 + 10, text="done.")],
                      mtime=T0 + 10)
        r1 = self.at(s, T0 + 45)
        r2 = self.at(s, T0 + 90)
        self.assertEqual(r1.st, "done")
        self.assertEqual(r1.el, 10)              # last_event_ts - turn_start
        self.assertEqual(r2.el, 10)              # frozen: does not tick

    def test_idle_with_lone_user_message_is_zero(self):
        s = self.make([user(T0)], mtime=T0)
        r = self.at(s, T0 + 300)
        self.assertEqual(r.st, "idle")
        self.assertEqual(r.el, 0)                # last_event_ts == turn_start

    def test_empty_session_is_zero(self):
        s = self.make([], mtime=T0)
        self.assertEqual(self.at(s, T0 + 300).el, 0)


if __name__ == "__main__":
    unittest.main()
