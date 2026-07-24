"""Wave 4 (Item 4) — the two-timer contract with read-time decay.

WORKING timer = UserPromptSubmit -> Stop; WAITING timer = Stop -> next
prompt; done freezes the turn duration; idle is 0. Every el below comes
from derive() at an injected now — the "where every pixel comes from"
table of the design, pinned at its transition boundaries, plus the fin
terminal-outcome classification and bridge-restart recovery (edges are
memory-only; the transcript tier must take over without a jump).
"""

import tempfile
import unittest

from csb.core import build_packet
from csb.engine import derive
from csb.session import Session

from tests.helpers import (T0, api_error, append_jsonl, assistant_text,
                           assistant_tool_use, base_cfg, hook_event,
                           make_session, seed_model_cache, stub_usage,
                           tool_result, user)


class TimerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = base_cfg()

    def make(self, records, mtime, **kw):
        return make_session(self.tmp.name, records, mtime=mtime, **kw)

    def hook(self, s, name, ts, **kw):
        s.apply_hook(hook_event(name, ts, transcript_path=s.path, **kw))

    def at(self, s, now):
        return derive(s, self.cfg, now)


class TestWorkingTimer(TimerCase):
    def test_run_ticks_from_the_prompt_edge(self):
        # transcript user record at T0+2 but the edge fired at T0 — the
        # edge is the turn start (hooks > transcript on disagreement)
        s = self.make([user(T0 + 2)], mtime=T0 + 2)
        self.hook(s, "UserPromptSubmit", T0)
        for dt in (1, 5, 30):
            r = self.at(s, T0 + dt)
            self.assertEqual((r.st, r.el), ("run", dt), dt)

    def test_tool_ticks_from_the_prompt_edge_not_the_tool(self):
        # working time is turn time: a tool starting at +10 does not reset
        # the clock
        s = self.make([user(T0), assistant_tool_use(T0 + 10, "Task", "tu_t",
                                                    {})],
                      mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        r = self.at(s, T0 + 15)
        self.assertEqual((r.st, r.el), ("tool", 15))

    def test_transcript_fallback_counts_from_last_user_message(self):
        # no edges at all: the working timer anchors on the transcript's
        # last real user record — today's behavior, zero regression
        s = self.make([user(T0), assistant_text(T0 + 10)], mtime=T0 + 10)
        self.assertEqual(self.at(s, T0 + 25).el, 25)


class TestWaitingTimer(TimerCase):
    def test_perm_prompt_wait_ticks_from_the_prompt(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Notification", T0 + 5.5,
                  message="Claude needs your permission to use Bash")
        r = self.at(s, T0 + 6.5)
        self.assertEqual((r.st, r.el), ("wait", 1))
        self.assertEqual(self.at(s, T0 + 65.5).el, 60)   # keeps ticking

    def test_stop_question_wait_ticks_from_the_stop_edge(self):
        s = self.make([user(T0), assistant_text(T0 + 10,
                                                text="should I deploy?")],
                      mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 11)
        r = self.at(s, T0 + 24)                # quiet 14 > question_after_s
        self.assertEqual(r.st, "wait")
        self.assertEqual(r.el, 13)             # now - Stop, not now - text

    def test_transcript_wait_falls_back_to_last_event(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        r = self.at(s, T0 + 30)                # approval flip at 20s silence
        self.assertEqual((r.st, r.el), ("wait", 25))

    def test_wait_never_shows_turn_length(self):
        # a long turn that ends in a question: the waiting timer starts
        # near zero, it does not inherit the 600s working time
        s = self.make([user(T0), assistant_text(T0 + 600,
                                                text="should I deploy?")],
                      mtime=T0 + 600)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 601)
        r = self.at(s, T0 + 614)
        self.assertEqual((r.st, r.el), ("wait", 13))


class TestFrozenDone(TimerCase):
    def _stopped(self):
        s = self.make([user(T0), assistant_text(T0 + 10, text="all done.")],
                      mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 11)
        return s

    def test_done_freezes_the_edge_duration(self):
        s = self._stopped()
        for now in (T0 + 12, T0 + 60, T0 + 600):
            r = self.at(s, now)
            self.assertEqual((r.st, r.el), ("done", 11), now)

    def test_no_discontinuity_at_the_stop_edge(self):
        # the working timer's last reading equals the frozen duration
        s = self.make([user(T0), assistant_text(T0 + 10)], mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        self.assertEqual(self.at(s, T0 + 11).el, 11)   # still running
        self.hook(s, "Stop", T0 + 11)
        self.assertEqual(self.at(s, T0 + 11).el, 11)   # frozen at the same value

    def test_frozen_el_survives_late_flushes(self):
        s = self._stopped()
        self.assertEqual(self.at(s, T0 + 12).el, 11)   # latch armed
        append_jsonl(s.path, [tool_result(T0 + 50, "tu_stale")])
        s.poll([])
        r = self.at(s, T0 + 51)
        self.assertEqual((r.st, r.el), ("done", 11))

    def test_done_to_run_restarts_the_clock(self):
        s = self._stopped()
        self.assertEqual(self.at(s, T0 + 12).st, "done")
        append_jsonl(s.path, [user(T0 + 30, text="next task")])
        s.poll([])
        self.hook(s, "UserPromptSubmit", T0 + 30)
        r = self.at(s, T0 + 31)
        self.assertEqual((r.st, r.el), ("run", 1))     # fresh WORKING timer

    def test_transcript_done_freezes_event_span(self):
        # no edges: frozen duration is last event - turn start (fallback)
        s = self.make([user(T0), assistant_text(T0 + 10, text="done.")],
                      mtime=T0 + 10)
        for now in (T0 + 45, T0 + 500):
            self.assertEqual(self.at(s, now).el, 10, now)

    def test_idle_is_zero(self):
        s = self.make([user(T0)], mtime=T0)
        r = self.at(s, T0 + 300)
        self.assertEqual((r.st, r.el), ("idle", 0))


class TestFin(TimerCase):
    def test_clean_stop_is_ok(self):
        s = self.make([user(T0), assistant_text(T0 + 10, text="all done.")],
                      mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 11)
        r = self.at(s, T0 + 12)
        self.assertEqual((r.st, r.fin), ("done", "ok"))

    def test_stop_with_unmatched_tool_use_is_cancel(self):
        # Escape mid-tool: Stop arrives while a tool_use is still pending
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 8)
        r = self.at(s, T0 + 9)
        self.assertEqual((r.st, r.el, r.fin), ("done", 8, "cancel"))

    def test_cancel_survives_late_assistant_flush(self):
        # ordinary poll-vs-hook ordering: the Stop edge arrives over HTTP
        # instantly, but an assistant record written just before it lands
        # on the next poll tick. That flush must not release the latch
        # and reclassify the hook-decided cancel to ok.
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 20)
        r = self.at(s, T0 + 21)
        self.assertEqual((r.st, r.el, r.fin), ("done", 20, "cancel"))
        append_jsonl(s.path, [assistant_text(T0 + 19, text="partial")])
        s.poll([])
        r = self.at(s, T0 + 22)
        self.assertEqual((r.st, r.el, r.fin), ("done", 20, "cancel"))

    def test_cancel_survives_late_flush_after_session_end(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "SessionEnd", T0 + 30)
        r = self.at(s, T0 + 31)
        self.assertEqual((r.st, r.el, r.fin), ("done", 30, "cancel"))
        append_jsonl(s.path, [assistant_text(T0 + 29, text="partial")])
        s.poll([])
        r = self.at(s, T0 + 32)
        self.assertEqual((r.st, r.el, r.fin), ("done", 30, "cancel"))

    def test_mid_turn_quiet_resumption_still_releases_the_latch(self):
        # the guard is scoped to PRE-edge flushes: with no Stop edge
        # (transcript-inferred done during mid-turn quiet) new assistant
        # output is genuine resumption and must still break the latch
        s = self.make([user(T0), assistant_text(T0 + 10, text="thinking")],
                      mtime=T0 + 10)
        self.assertEqual(self.at(s, T0 + 45).st, "done")   # latch armed
        append_jsonl(s.path, [assistant_text(T0 + 50, text="one more thing")])
        s.poll([])
        self.assertEqual(self.at(s, T0 + 51).st, "run")

    def test_api_error_in_the_turn_is_fail(self):
        # transient error, turn recovers, then stops: the window still
        # carries the error record -> fail. Edges applied in real order
        # (UserPromptSubmit precedes the turn's records).
        s = self.make([user(T0)], mtime=T0)
        self.hook(s, "UserPromptSubmit", T0)
        us = {"input_tokens": 10, "output_tokens": 5}
        append_jsonl(s.path, [api_error(T0 + 3, error="API Error: 500"),
                              assistant_text(T0 + 6, text="retried ok.",
                                             usage=us)])
        s.poll([])
        self.hook(s, "Stop", T0 + 7)
        r = self.at(s, T0 + 8)
        self.assertEqual((r.st, r.fin), ("done", "fail"))

    def test_session_end_mid_turn_is_cancel_and_freezes(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "SessionEnd", T0 + 20)
        r = self.at(s, T0 + 60)
        self.assertEqual((r.st, r.el, r.fin), ("done", 20, "cancel"))
        self.assertEqual(self.at(s, T0 + 600).el, 20)  # frozen

    def _turn_with_error(self, follow_ups):
        us = {"input_tokens": 1, "output_tokens": 1}
        s = self.make([user(T0)], mtime=T0)
        self.hook(s, "UserPromptSubmit", T0)
        recs = [api_error(T0 + 1, error="API Error: 500")]
        recs += [assistant_text(T0 + 2 + i, text=f"step {i}", usage=us)
                 for i in range(follow_ups)]
        append_jsonl(s.path, recs)
        s.poll([])
        self.hook(s, "Stop", T0 + 2 + follow_ups)
        return s

    def test_window_is_capped_at_last_15_messages(self):
        # claude-notifications-go's turn-scoped cap: an error followed by
        # 15 later messages has scrolled out of the window -> ok
        s = self._turn_with_error(follow_ups=15)
        self.assertEqual(self.at(s, T0 + 18).fin, "ok")

    def test_error_within_the_window_is_still_fail(self):
        s = self._turn_with_error(follow_ups=14)   # error is msg 15 of 15
        self.assertEqual(self.at(s, T0 + 17).fin, "fail")

    def test_fin_empty_unless_done(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        self.hook(s, "UserPromptSubmit", T0)
        self.assertEqual(self.at(s, T0 + 6).fin, "")           # tool
        self.hook(s, "Notification", T0 + 7, message=None)
        self.assertEqual(self.at(s, T0 + 8).fin, "")           # wait

    def test_new_prompt_clears_fin(self):
        s = self.make([user(T0), assistant_text(T0 + 10, text="all done.")],
                      mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 11)
        self.assertEqual(self.at(s, T0 + 12).fin, "ok")
        append_jsonl(s.path, [user(T0 + 30, text="next")])
        s.poll([])
        self.hook(s, "UserPromptSubmit", T0 + 30)
        r = self.at(s, T0 + 31)
        self.assertEqual((r.st, r.fin), ("run", ""))
        self.assertEqual(s.fin, "")

    def test_transcript_inferred_done_classifies_too(self):
        # no hooks at all: quiet-based done still reports ok/fail
        s = self.make([user(T0), assistant_text(T0 + 10, text="all done.")],
                      mtime=T0 + 10)
        self.assertEqual(self.at(s, T0 + 45).fin, "ok")
        us = {"input_tokens": 10, "output_tokens": 5}
        s2 = self.make([user(T0), api_error(T0 + 3, error="API Error: 500"),
                        assistant_text(T0 + 6, text="recovered.", usage=us)],
                       mtime=T0 + 6, filename="failed.jsonl")
        self.assertEqual(self.at(s2, T0 + 45).fin, "fail")

    def test_session_start_clears_fin(self):
        s = self.make([user(T0), assistant_text(T0 + 10, text="all done.")],
                      mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 11)
        self.assertEqual(self.at(s, T0 + 12).fin, "ok")
        self.hook(s, "SessionStart", T0 + 20)
        self.assertEqual(s.fin, "")


class TestPacketFin(TimerCase):
    def test_fin_on_the_wire_only_when_done(self):
        seed_model_cache()
        cfg = base_cfg()
        usage = stub_usage(cfg)
        done = self.make([user(T0), assistant_text(T0 + 10, text="done.")],
                         mtime=T0 + 10, filename="d.jsonl")
        done.first_seen = T0
        running = self.make([user(T0), assistant_text(T0 + 40)],
                            mtime=T0 + 40, filename="r.jsonl")
        running.first_seen = T0 + 1
        pkt = build_packet({s.path: s for s in (done, running)}, cfg, usage,
                           now=T0 + 45)
        self.assertEqual([e["st"] for e in pkt["ses"]], ["done", "run"])
        self.assertEqual([e["fin"] for e in pkt["ses"]], ["ok", ""])


class TestBridgeRestartRecovery(TimerCase):
    """Hook edges live only in bridge memory. After a restart the engine
    must fall back to the transcript tier for the same session without
    lying about state and without a large timer jump."""

    def _restart(self, s):
        s2 = Session(s.path, mtime_fn=s.mclock)
        s2.poll([])
        return s2

    def test_done_survives_a_restart(self):
        s = self.make([user(T0), assistant_text(T0 + 10, text="all done.")],
                      mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 11)
        before = self.at(s, T0 + 45)
        self.assertEqual((before.st, before.el, before.src),
                         ("done", 11, "h"))
        s2 = self._restart(s)
        after = derive(s2, self.cfg, T0 + 45)
        self.assertEqual((after.st, after.src), ("done", "t"))
        self.assertEqual(after.fin, "ok")          # re-inferred, same call
        self.assertLessEqual(abs(after.el - before.el), 1)   # 10 vs 11

    def test_mid_turn_restart_keeps_running(self):
        s = self.make([user(T0), assistant_text(T0 + 10)], mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        before = self.at(s, T0 + 20)
        self.assertEqual((before.st, before.el), ("run", 20))
        s2 = self._restart(s)
        after = derive(s2, self.cfg, T0 + 20)
        self.assertEqual((after.st, after.el), ("run", 20))  # same anchor

    def test_waiting_restart_keeps_waiting(self):
        # question asked, bridge restarts during the wait: the transcript
        # tier reproduces wait with the same last-event anchor
        s = self.make([user(T0), assistant_text(T0 + 10,
                                                text="should I deploy?")],
                      mtime=T0 + 10)
        self.hook(s, "UserPromptSubmit", T0)
        self.hook(s, "Stop", T0 + 11)
        before = self.at(s, T0 + 30)
        self.assertEqual((before.st, before.el), ("wait", 19))
        s2 = self._restart(s)
        after = derive(s2, self.cfg, T0 + 30)
        self.assertEqual(after.st, "wait")
        self.assertLessEqual(abs(after.el - before.el), 1)   # 20 vs 19


if __name__ == "__main__":
    unittest.main()
