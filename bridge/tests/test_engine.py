"""Baseline state-machine table: pins TODAY's derivation behavior at exact
threshold boundaries so later waves (approval flip rework, hook tiers,
limits) can't regress it silently.

Defaults under test: idle_after_s=120, wait_tool_s=20, done_after_s=30,
question_after_s=12. All clocks injected — no sleeping.
"""

import tempfile
import unittest

from csb.engine import derive

from tests.helpers import (T0, append_jsonl, assistant_text,
                           assistant_tool_use, base_cfg, make_session, noise,
                           tool_result, user)


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

    def test_real_event_resets_the_silence_clock(self):
        # a real record after the tool_use restarts the debounce
        s = self._pending()
        append_jsonl(s.path, [assistant_text(T0 + 15, text="progress note")])
        s.poll([])
        self.assertEqual(self.at(s, T0 + 15 + 19.9).st, "tool")
        self.assertEqual(self.at(s, T0 + 15 + 20.1).st, "wait")

    def test_noise_write_does_not_reset_the_silence_clock(self):
        # noise records bump mtime and file size but are not activity —
        # the approval flip must still happen on time (Extra A)
        s = self._pending()
        append_jsonl(s.path, [noise(T0 + 20, "file-history-snapshot")])
        s.poll([])
        s.mclock.value = T0 + 20              # the write also bumped mtime
        r = self.at(s, T0 + 5 + 20.1)
        self.assertEqual((r.st, r.tl), ("wait", "Bash"))

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


class TestApprovalDebounceTunable(EngineCase):
    """Item 2: the write-silence debounce is a config knob."""

    def _pending(self):
        return self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                         mtime=T0 + 5)

    def test_shorter_debounce_flips_sooner(self):
        self.cfg["approval_silence_s"] = 3.5
        s = self._pending()
        self.assertEqual(self.at(s, T0 + 5 + 3.4).st, "tool")
        r = self.at(s, T0 + 5 + 3.6)
        self.assertEqual((r.st, r.tl, r.td), ("wait", "Bash", "npm test"))

    def test_wait_tool_s_is_the_fallback_knob(self):
        del self.cfg["approval_silence_s"]
        self.cfg["wait_tool_s"] = 5
        s = self._pending()
        self.assertEqual(self.at(s, T0 + 5 + 4.9).st, "tool")
        self.assertEqual(self.at(s, T0 + 5 + 5.1).st, "wait")

    def test_fast_tool_result_never_flickers(self):
        # result lands well inside the debounce -> never shows wait
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1"),
                       tool_result(T0 + 6, "tu_1")], mtime=T0 + 6)
        for dt in (0.1, 5, 19, 25):
            self.assertEqual(self.at(s, T0 + 6 + dt).st, "run", dt)

    def test_wait_clears_within_a_packet_of_the_result(self):
        # flip happened; the approval's tool_result clears it on next derive
        s = self._pending()
        self.assertEqual(self.at(s, T0 + 26).st, "wait")
        append_jsonl(s.path, [tool_result(T0 + 26.5, "tu_1")])
        s.poll([])
        self.assertEqual(self.at(s, T0 + 27).st, "run")

    def test_confirm_cadence_near_the_flip(self):
        from csb.core import BridgeCore
        from tests.helpers import stub_usage
        cfg = base_cfg(roots=[self.tmp.name])
        core = BridgeCore(cfg, usage=stub_usage(cfg))
        s = self._pending()
        core.sessions[s.path] = s
        pts = T0 + 5
        self.assertEqual(core.next_interval(pts + 5), 1.0)     # far from flip
        self.assertEqual(core.next_interval(pts + 19.2), 0.5)  # within 1s
        self.assertEqual(core.next_interval(pts + 20.9), 0.5)  # just past
        self.assertEqual(core.next_interval(pts + 30), 1.0)    # flip settled


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


class TestNoiseSkip(EngineCase):
    """Extra A at the engine level: noise-kept-alive transcripts must not
    read as fresh — only real events count as activity."""

    def test_noise_only_freshness_uses_mtime_fallback(self):
        # a transcript with no real events falls back to mtime
        s = self.make([], mtime=T0)
        self.assertEqual(self.at(s, T0 + 300).st, "idle")

    def test_noise_cannot_keep_a_session_fresh(self):
        # real events ended at T0+1; noise keeps bumping mtime — the
        # session still goes stale on the real-activity clock
        for i, ntype in enumerate(("file-history-snapshot", "queue-operation",
                                   "last-prompt", "attachment",
                                   "bridge-session")):
            s = self.make([user(T0), noise(T0 + 100, ntype)],
                          mtime=T0 + 100, filename=f"noise-{i}.jsonl")
            self.assertEqual(self.at(s, T0 + 130).st, "idle", ntype)

    def test_noise_does_not_defer_done(self):
        s = self.make([user(T0), assistant_text(T0 + 10, text="all done."),
                       noise(T0 + 35, "file-history-snapshot")],
                      mtime=T0 + 35)
        self.assertEqual(self.at(s, T0 + 45).st, "done")   # quiet = 35 > 30


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
