"""Item 5: rate-limit / API-error detection — both regexes, the rollover
guard, the live countdown with an injected clock, error-reason mapping,
clearing rules, and alert-cooldown persistence across a simulated restart.
All clocks injected — no sleeping, no network."""

import os
import tempfile
import unittest

from csb.engine import derive
from csb.fmt import fmt_countdown
from csb.limits import AlertLog, is_expired, scan_text, short_reason

from tests.helpers import (T0, api_error, append_jsonl, assistant_text,
                           base_cfg, make_session, system, tool_result, user)


class TestScanText(unittest.TestCase):
    def test_tool_result_epoch_form(self):
        self.assertEqual(scan_text("limit reached|1750010000", T0), 1750010000)

    def test_usage_limit_literal(self):
        self.assertEqual(
            scan_text("Claude AI usage limit reached|1750010000", T0),
            1750010000)

    def test_wait_minutes_anchored_to_record_ts(self):
        self.assertEqual(scan_text("please wait 30 minutes", T0, relative=True),
                         int(T0 + 1800))
        self.assertEqual(scan_text("Wait 1 minute.", T0, relative=True),
                         int(T0 + 60))

    def test_wait_minutes_requires_relative_opt_in(self):
        # default scan is structured-epoch only: agent-visible prose like
        # a deploy log's "wait 45 minutes" must not read as a rate limit
        self.assertEqual(scan_text("please wait 30 minutes", T0), 0)
        self.assertEqual(
            scan_text("Cluster busy - please wait 45 minutes.", T0), 0)

    def test_no_signal(self):
        for text in ("", "all tests passed", "the limit was reached today",
                     "wait for me"):
            self.assertEqual(scan_text(text, T0), 0, repr(text))
            self.assertEqual(scan_text(text, T0, relative=True), 0, repr(text))


class TestIsExpired(unittest.TestCase):
    def test_boundaries(self):
        self.assertTrue(is_expired(0, T0))            # no limit at all
        self.assertTrue(is_expired(T0, T0))           # exactly at reset
        self.assertTrue(is_expired(T0 - 1, T0))       # past epoch: ghost
        self.assertFalse(is_expired(T0 + 1, T0))


class TestShortReason(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(short_reason("authentication_failed"), "auth failed")
        self.assertEqual(short_reason({"type": "authentication_failed"}),
                         "auth failed")
        self.assertEqual(short_reason("API Error: 401 unauthorized"),
                         "API 401")
        self.assertEqual(short_reason(""), "error")
        self.assertLessEqual(len(short_reason("x" * 100)), 20)


class LimitsSessionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = base_cfg()

    def make(self, records, mtime, **kw):
        return make_session(self.tmp.name, records, mtime=mtime, **kw)


class TestSessionLimitFacts(LimitsSessionCase):
    RESET = int(T0 + 2 * 3600)

    def _limited(self):
        return self.make(
            [user(T0), tool_result(T0 + 2, "tu_x",
                                   content=f"limit reached|{self.RESET}")],
            mtime=T0 + 2)

    def test_tool_result_sets_reset_epoch(self):
        self.assertEqual(self._limited().limit_reset, self.RESET)

    def test_tool_result_prose_wait_minutes_is_not_a_limit(self):
        # a deploy log mentioning "wait 45 minutes" is ordinary tool
        # output, not a throttle notice — no banner, session stays run
        s = self.make(
            [user(T0),
             tool_result(T0 + 2, "tu_x",
                         content="Deploy queued. Cluster busy - please "
                                 "wait 45 minutes before retrying.")],
            mtime=T0 + 2)
        self.assertEqual(s.limit_reset, 0.0)
        self.assertEqual(derive(s, self.cfg, T0 + 10).st, "run")

    def test_system_wait_minutes_sets_relative_reset(self):
        s = self.make([user(T0), system(T0 + 5, "please wait 15 minutes")],
                      mtime=T0 + 5)
        self.assertEqual(s.limit_reset, int(T0 + 5 + 900))

    def test_api_error_limit_literal(self):
        s = self.make([user(T0),
                       api_error(T0 + 1,
                                 text=f"Claude AI usage limit reached|{self.RESET}")],
                      mtime=T0 + 1)
        self.assertEqual(s.limit_reset, self.RESET)
        self.assertEqual(s.error, "")               # a limit is not an error

    def test_api_error_sets_error_flag(self):
        s = self.make([user(T0), api_error(T0 + 1, error="authentication_failed")],
                      mtime=T0 + 1)
        self.assertEqual(s.error, "auth failed")

    def test_error_cleared_by_new_user_prompt(self):
        s = self.make([user(T0), api_error(T0 + 1, error="authentication_failed")],
                      mtime=T0 + 1)
        append_jsonl(s.path, [user(T0 + 10, text="try again")])
        s.poll([])
        self.assertEqual(s.error, "")

    def test_limit_and_error_cleared_by_successful_call(self):
        s = self._limited()
        append_jsonl(s.path, [api_error(T0 + 3, error="overloaded"),
                              assistant_text(T0 + 60, usage={"input_tokens": 5,
                                                             "output_tokens": 5})])
        s.poll([])
        self.assertEqual(s.limit_reset, 0.0)
        self.assertEqual(s.error, "")


class TestEngineLimited(LimitsSessionCase):
    RESET = int(T0 + 2 * 3600)

    def _limited(self):
        return self.make(
            [user(T0), tool_result(T0 + 2, "tu_x",
                                   content=f"limit reached|{self.RESET}")],
            mtime=T0 + 2)

    def test_limited_state_and_wire_shape(self):
        r = derive(self._limited(), self.cfg, T0 + 10)
        self.assertEqual(r.st, "wait")
        self.assertEqual(r.tl, "")                 # never "approve: RateLimit"
        self.assertEqual(r.td, "rate limit · 1h59m")
        self.assertEqual(r.lim, self.RESET)
        self.assertEqual(r.err, "")

    def test_countdown_ticks_at_read_time(self):
        s = self._limited()
        self.assertEqual(derive(s, self.cfg, self.RESET - 300).td,
                         "rate limit · 5m")
        self.assertEqual(derive(s, self.cfg, self.RESET - 60).td,
                         "rate limit · 1m")

    def test_limited_beats_every_other_inference(self):
        # even a stale session with a trailing "?" shows the limit banner
        s = self.make([user(T0), assistant_text(T0 + 1, text="deploy?"),
                       tool_result(T0 + 2, "tu_x",
                                   content=f"limit reached|{self.RESET}")],
                      mtime=T0 + 2)
        self.assertEqual(derive(s, self.cfg, T0 + 500).lim, self.RESET)

    def test_expired_epoch_never_shows(self):
        s = self._limited()
        r = derive(s, self.cfg, self.RESET)          # now == reset: cleared
        self.assertEqual(r.lim, 0)
        self.assertNotIn("rate limit", r.td)
        r = derive(s, self.cfg, self.RESET + 10)
        self.assertEqual(r.lim, 0)

    def test_error_state_wire_shape(self):
        s = self.make([user(T0), api_error(T0 + 1, error="authentication_failed")],
                      mtime=T0 + 1)
        r = derive(s, self.cfg, T0 + 5)
        self.assertEqual((r.st, r.tl, r.td), ("wait", "", "error · auth failed"))
        self.assertEqual(r.lim, 0)
        self.assertEqual(r.err, "auth failed")


class TestCountdownAbsoluteForm(unittest.TestCase):
    def test_relative_below_24h(self):
        self.assertEqual(fmt_countdown(3599, now=T0), "59m")
        self.assertEqual(fmt_countdown(2 * 3600 + 840, now=T0), "2h14m")

    def test_absolute_beyond_24h_with_now(self):
        out = fmt_countdown(2 * 86400 + 3600, now=T0)
        self.assertRegex(out, r"^[A-Z][a-z]{2} \d{1,2} \d{2}:\d{2}$")

    def test_legacy_relative_form_without_now(self):
        # us.r5/r7 keep their compact "2d1h" form
        self.assertEqual(fmt_countdown(2 * 86400 + 3600), "2d1h")


class TestAlertCooldown(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "alerts.json")

    def test_fires_once_per_window(self):
        log = AlertLog(self.path, cooldown_s=86400)
        self.assertTrue(log.should_fire("limit:a:123", now=T0))
        self.assertFalse(log.should_fire("limit:a:123", now=T0 + 100))
        self.assertTrue(log.should_fire("limit:b:456", now=T0 + 100))

    def test_cooldown_survives_a_restart(self):
        AlertLog(self.path, cooldown_s=86400).should_fire("limit:a:123", now=T0)
        fresh = AlertLog(self.path, cooldown_s=86400)   # simulated restart
        self.assertFalse(fresh.should_fire("limit:a:123", now=T0 + 3600))

    def test_refires_after_cooldown(self):
        log = AlertLog(self.path, cooldown_s=86400)
        self.assertTrue(log.should_fire("limit:a:123", now=T0))
        self.assertTrue(log.should_fire("limit:a:123", now=T0 + 86401))

    def test_corrupt_file_tolerated(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertTrue(AlertLog(self.path).should_fire("x", now=T0))


if __name__ == "__main__":
    unittest.main()
