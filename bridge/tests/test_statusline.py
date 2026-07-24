"""Wave 4 (Item 3) — statusline pass-through collector.

Collector: byte-identical echo (even on internal failure), atomic
captures under concurrent writers, subprocess end-to-end with the
committed example payload. Reader: Maciek hardening — TTL, >101% drop,
resets_at rollover, tombstone. Consumption: UsageTracker preference with
clean OAuth/estimate fallback, per-session context/effort, engine
liveness. Installer: statusLine wrap in a temp settings.json only.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from csb import statusline
from csb.engine import derive
from csb.hooks import _read_settings

from tests.helpers import (MODEL, T0, base_cfg, make_session,
                           seed_model_cache, stub_usage, user)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                       "statusline_payload.json")
BRIDGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# the committed example payload resets at these epochs (T0-relative)
FIVE_HOUR_RESET = T0 + 10400        # 2025-06-15T18:00:00Z
SEVEN_DAY_RESET = T0 + 402800       # 2025-06-20T07:00:00Z


def fixture_payload():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        return json.load(f)


class StatuslineCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "statusline")
        self.cfg = base_cfg(statusline_dir=self.dir)

    def write_capture(self, cap, sid=None):
        os.makedirs(self.dir, exist_ok=True)
        sid = sid or cap.get("session_id", "sid")
        path = os.path.join(self.dir, sid + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cap, f)
        return path

    def capture(self, sid="sid-1", ts=T0, rate_limits=None, **over):
        cap = {"ts": ts, "session_id": sid, "rate_limits": rate_limits,
               "context_used_pct": 42.5, "cost": {"total_cost_usd": 1.0},
               "model": {"id": MODEL}, "effort": "high"}
        cap.update(over)
        return cap


# --------------------------------------------------------------- collector

class TestCollector(StatuslineCase):
    def test_echo_is_byte_identical_and_capture_lands(self):
        raw = (json.dumps(fixture_payload()) + "\n").encode()
        out = io.BytesIO()
        statusline.collect(raw, self.dir, out=out)
        self.assertEqual(out.getvalue(), raw)
        cap_path = os.path.join(
            self.dir, "b3c1a5e2-4f6d-4a2b-9c8e-0d7f13a9b2c4.json")
        with open(cap_path, "r", encoding="utf-8") as f:
            cap = json.load(f)
        self.assertEqual(cap["session_id"],
                         "b3c1a5e2-4f6d-4a2b-9c8e-0d7f13a9b2c4")
        self.assertEqual(cap["context_used_pct"], 42.5)
        self.assertEqual(cap["effort"], "high")
        self.assertEqual(cap["model"]["id"], MODEL)
        self.assertEqual(cap["cost"]["total_cost_usd"], 1.2345)
        self.assertEqual(
            cap["rate_limits"]["five_hour"]["used_percentage"], 34.2)
        self.assertIsInstance(cap["ts"], float)

    def test_echo_survives_internal_error(self):
        # capture dir path is an existing FILE -> makedirs fails; the echo
        # already happened and the error is swallowed
        blocker = os.path.join(self.tmp.name, "blocked")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        raw = b'{"session_id": "sid-9"}'
        out = io.BytesIO()
        statusline.collect(raw, blocker, out=out)   # must not raise
        self.assertEqual(out.getvalue(), raw)

    def test_non_json_stdin_is_echoed_untouched(self):
        raw = b"not json at all \xff\x00"
        out = io.BytesIO()
        statusline.collect(raw, self.dir, out=out)
        self.assertEqual(out.getvalue(), raw)
        self.assertFalse(os.path.isdir(self.dir) and os.listdir(self.dir))

    def test_quiet_mode_emits_nothing(self):
        out = io.BytesIO()
        statusline.collect(b'{"session_id": "sid-q"}', self.dir,
                           quiet=True, out=out)
        self.assertEqual(out.getvalue(), b"")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "sid-q.json")))

    def test_session_id_cannot_escape_the_capture_dir(self):
        statusline.snapshot({"session_id": "../../evil"}, self.dir, now=T0)
        self.assertEqual(os.listdir(self.dir), [".._.._evil.json"])
        # an id with no substance at all is refused
        self.assertIsNone(
            statusline.snapshot({"session_id": "../.."}, self.dir, now=T0))
        self.assertIsNone(statusline.snapshot({}, self.dir, now=T0))

    def test_subprocess_end_to_end(self):
        # the real pipeline: fixture payload | python -m csb.statusline
        with open(FIXTURE, "rb") as f:
            raw = f.read()
        proc = subprocess.run(
            [sys.executable, "-m", "csb.statusline", "collect",
             "--dir", self.dir],
            input=raw, capture_output=True, cwd=BRIDGE_DIR, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, raw)         # byte-identical
        self.assertTrue(os.path.exists(os.path.join(
            self.dir, "b3c1a5e2-4f6d-4a2b-9c8e-0d7f13a9b2c4.json")))

    def test_parallel_writers_never_yield_a_partial_read(self):
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, "sid-hammer.json")
        payload = {"session_id": "sid-hammer",
                   "context_window": {"used_percentage": 50.0},
                   "cost": {"filler": "x" * 4096}}
        stop = threading.Event()
        errors = []

        def writer():
            while not stop.is_set():
                statusline.snapshot(payload, self.dir)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        try:
            reads = 0
            while reads < 200:
                if not os.path.exists(path):
                    continue
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        cap = json.load(f)
                    self.assertEqual(cap["session_id"], "sid-hammer")
                    self.assertEqual(cap["context_used_pct"], 50.0)
                    reads += 1
                except (ValueError, KeyError) as e:
                    errors.append(e)
                    break
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=5)
        self.assertEqual(errors, [])
        self.assertEqual(reads, 200)


# ------------------------------------------------------- reader hardening

class TestReaderHardening(StatuslineCase):
    def test_ttl_expiry(self):
        path = self.write_capture(self.capture(ts=T0))
        self.assertIsNotNone(statusline.read_capture(path, T0 + 599.9))
        self.assertIsNone(statusline.read_capture(path, T0 + 600.0))

    def test_over_101_context_pct_dropped(self):
        # leak bug claude-code#52326: a 250% reading is garbage, not data
        path = self.write_capture(self.capture(context_used_pct=250.0))
        cap = statusline.read_capture(path, T0 + 1)
        self.assertIsNone(cap["context_used_pct"])
        path = self.write_capture(self.capture(context_used_pct=101.0))
        self.assertEqual(
            statusline.read_capture(path, T0 + 1)["context_used_pct"], 101.0)

    def test_tombstone_suppresses_the_capture(self):
        path = self.write_capture(self.capture(tombstone=True))
        self.assertIsNone(statusline.read_capture(path, T0 + 1))

    def test_missing_and_garbage_files(self):
        self.assertIsNone(statusline.read_capture(
            os.path.join(self.dir, "nope.json"), T0))
        os.makedirs(self.dir, exist_ok=True)
        bad = os.path.join(self.dir, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{truncated")
        self.assertIsNone(statusline.read_capture(bad, T0))

    def test_rate_limits_over_101_bucket_dropped(self):
        us = statusline.parse_rate_limits(
            {"five_hour": {"used_percentage": 250.0,
                           "resets_at": FIVE_HOUR_RESET},
             "seven_day": {"used_percentage": 61.9,
                           "resets_at": SEVEN_DAY_RESET}}, T0)
        self.assertEqual((us["p5"], us["r5"]), (-1, ""))
        self.assertEqual(us["p7"], 62)

    def test_resets_at_rollover_nulls_the_bucket(self):
        # pct written before the window rolled over is a ghost: absent,
        # never a stale 87%
        us = statusline.parse_rate_limits(
            {"five_hour": {"used_percentage": 87.0, "resets_at": T0 - 10},
             "seven_day": {"used_percentage": 61.9,
                           "resets_at": SEVEN_DAY_RESET}}, T0)
        self.assertEqual((us["p5"], us["r5"]), (-1, ""))
        self.assertEqual((us["p7"], us["r7"]), (62, "4d15h"))

    def test_all_buckets_unusable_is_none(self):
        self.assertIsNone(statusline.parse_rate_limits(
            {"five_hour": {"used_percentage": 87.0, "resets_at": T0 - 10}},
            T0))
        self.assertIsNone(statusline.parse_rate_limits({}, T0))
        self.assertIsNone(statusline.parse_rate_limits(None, T0))

    def test_bucket_tombstone(self):
        us = statusline.parse_rate_limits(
            {"five_hour": {"used_percentage": 30.0, "tombstone": True,
                           "resets_at": FIVE_HOUR_RESET},
             "seven_day": {"used_percentage": 61.9,
                           "resets_at": SEVEN_DAY_RESET}}, T0)
        self.assertEqual(us["p5"], -1)
        self.assertEqual(us["p7"], 62)

    def test_latest_capture_wins(self):
        rl = {"five_hour": {"used_percentage": 10.0,
                            "resets_at": FIVE_HOUR_RESET}}
        rl2 = {"five_hour": {"used_percentage": 20.0,
                             "resets_at": FIVE_HOUR_RESET}}
        self.write_capture(self.capture("sid-old", ts=T0, rate_limits=rl))
        self.write_capture(self.capture("sid-new", ts=T0 + 5,
                                        rate_limits=rl2))
        us = statusline.latest_rate_limits(self.cfg, T0 + 10)
        self.assertEqual(us["p5"], 20)
        self.assertFalse(us["est"])


# ------------------------------------------------------------ consumption

class TestUsagePreference(StatuslineCase):
    def test_snapshot_prefers_statusline_over_estimate(self):
        self.write_capture(self.capture(
            rate_limits=fixture_payload()["rate_limits"]))
        usage = stub_usage(self.cfg)
        us = usage.snapshot(now=T0 + 10)
        self.assertEqual((us["p5"], us["p7"], us["est"]), (34, 62, False))
        self.assertEqual(us["r5"], "2h53m")
        self.assertEqual(us["r7"], "4d15h")

    def test_stale_capture_falls_back_cleanly(self):
        self.write_capture(self.capture(
            rate_limits=fixture_payload()["rate_limits"]))
        usage = stub_usage(self.cfg)
        us = usage.snapshot(now=T0 + 700)          # past statusline_ttl_s
        self.assertTrue(us["est"])                 # estimate path

    def test_enterprise_payload_without_rate_limits_falls_back(self):
        self.write_capture(self.capture(rate_limits=None))
        usage = stub_usage(self.cfg)
        self.assertTrue(usage.snapshot(now=T0 + 10)["est"])

    def test_no_capture_dir_falls_back(self):
        usage = stub_usage(self.cfg)               # dir never created
        self.assertTrue(usage.snapshot(now=T0)["est"])


class TestSessionConsumption(StatuslineCase):
    def setUp(self):
        super().setUp()
        seed_model_cache()

    def _session(self):
        us = {"input_tokens": 10000, "output_tokens": 500,
              "cache_read_input_tokens": 30000}
        from tests.helpers import assistant_text
        return make_session(self.tmp.name,
                            [user(T0), assistant_text(T0 + 5, usage=us)],
                            mtime=T0 + 5)

    def test_refresh_populates_the_session(self):
        s = self._session()
        statusline.snapshot(
            {"session_id": s.session_id,
             "context_window": {"used_percentage": 42.5},
             "effort": {"level": "high"}}, self.dir, now=T0 + 6)
        statusline.refresh(s, self.cfg, now=T0 + 7)
        self.assertEqual(s.sl_ts, T0 + 6)
        self.assertEqual(s.sl_ctx_pct, 42.5)
        self.assertEqual(s.sl_effort, "high")

    def test_packet_prefers_capture_context_and_effort(self):
        s = self._session()
        s.sl_ts, s.sl_ctx_pct, s.sl_effort = T0 + 6, 42.5, "high"
        e = s.to_packet(self.cfg, now=T0 + 10)
        self.assertEqual(e["cx"], 42)              # capture, not 40k/200k=20
        self.assertEqual(e["ef"], "high")

    def test_stale_capture_falls_back_to_token_math(self):
        s = self._session()
        s.sl_ts, s.sl_ctx_pct, s.sl_effort = T0 + 6, 42.5, "high"
        e = s.to_packet(self.cfg, now=T0 + 700)    # capture expired
        self.assertEqual(e["cx"], 20)              # 40000 / 200000
        self.assertEqual(e["ef"], "")

    def test_capture_is_a_liveness_signal_not_activity(self):
        # transcript quiet since T0, capture still arriving: the session
        # is alive (not idle) — but the capture must not defer done
        s = make_session(self.tmp.name, [user(T0)], mtime=T0,
                         filename="live.jsonl")
        self.assertEqual(derive(s, self.cfg, T0 + 300).st, "idle")
        s.sl_ts = T0 + 295
        self.assertEqual(derive(s, self.cfg, T0 + 300).st, "run")

    def test_capture_does_not_defer_done(self):
        from tests.helpers import assistant_text
        s = make_session(self.tmp.name,
                         [user(T0), assistant_text(T0 + 10, text="done.")],
                         mtime=T0 + 10, filename="done.jsonl")
        s.sl_ts = T0 + 40                          # capture just arrived
        self.assertEqual(derive(s, self.cfg, T0 + 41).st, "done")


# -------------------------------------------------------------- installer

class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, "settings.json")
        self.dir = os.path.join(self.tmp.name, "captures")

    def write_settings(self, obj):
        with open(self.settings, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2)

    def read(self):
        with open(self.settings, "r", encoding="utf-8") as f:
            return json.load(f)


class TestInstaller(InstallerCase):
    def test_wraps_existing_command(self):
        self.write_settings({"model": "opus",
                             "statusLine": {"type": "command",
                                            "command": "npx ccstatusline",
                                            "padding": 0}})
        self.assertEqual(statusline.install(self.settings, self.dir), 0)
        got = self.read()
        cmd = got["statusLine"]["command"]
        self.assertIn(statusline.MARKER, cmd)
        self.assertTrue(cmd.endswith("| npx ccstatusline"))
        self.assertIn("csb.statusline collect", cmd)
        self.assertIn(self.dir, cmd)
        self.assertEqual(got["statusLine"]["padding"], 0)   # preserved
        self.assertEqual(got["model"], "opus")              # untouched
        self.assertTrue(os.path.exists(self.settings + ".csb-bak"))

    def test_install_is_idempotent(self):
        self.write_settings({"statusLine": {"type": "command",
                                            "command": "npx ccstatusline"}})
        statusline.install(self.settings, self.dir)
        once = self.read()
        statusline.install(self.settings, self.dir)
        self.assertEqual(self.read(), once)        # no double wrap

    def test_standalone_install_is_quiet(self):
        self.write_settings({"model": "opus"})
        statusline.install(self.settings, self.dir)
        cmd = self.read()["statusLine"]["command"]
        self.assertIn(statusline.MARKER, cmd)
        self.assertTrue(cmd.endswith("--quiet"))
        self.assertNotIn("|", cmd)

    def test_uninstall_restores_the_original(self):
        original = {"statusLine": {"type": "command",
                                   "command": "npx ccstatusline",
                                   "padding": 0},
                    "hooks": {"Stop": []}}
        self.write_settings(original)
        statusline.install(self.settings, self.dir)
        self.assertEqual(statusline.uninstall(self.settings), 0)
        got = self.read()
        self.assertEqual(got["statusLine"]["command"], "npx ccstatusline")
        self.assertEqual(got["statusLine"]["padding"], 0)
        self.assertEqual(got["hooks"], {"Stop": []})

    def test_uninstall_removes_standalone_entirely(self):
        self.write_settings({"model": "opus"})
        statusline.install(self.settings, self.dir)
        statusline.uninstall(self.settings)
        got = self.read()
        self.assertNotIn("statusLine", got)
        self.assertEqual(got["model"], "opus")

    def test_uninstall_leaves_foreign_statusline_alone(self):
        self.write_settings({"statusLine": {"type": "command",
                                            "command": "npx ccstatusline"}})
        before = self.read()
        self.assertEqual(statusline.uninstall(self.settings), 0)
        self.assertEqual(self.read(), before)

    def test_missing_settings_file_installs_fresh(self):
        self.assertEqual(statusline.install(self.settings, self.dir), 0)
        self.assertIn(statusline.MARKER,
                      self.read()["statusLine"]["command"])
        # no backup of a file that never existed
        self.assertFalse(os.path.exists(self.settings + ".csb-bak"))

    def test_unparseable_settings_refused(self):
        with open(self.settings, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(statusline.install(self.settings, self.dir), 1)
        with open(self.settings, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "{not json")

    def test_windows_refused_and_settings_untouched(self):
        self.write_settings({"statusLine": {"type": "command",
                                            "command": "npx ccstatusline"}})
        before = self.read()
        with mock.patch("sys.platform", "win32"):
            self.assertEqual(statusline.install(self.settings, self.dir), 2)
        self.assertEqual(self.read(), before)

    def test_pipeline_survives_settings_roundtrip(self):
        # the wrapped command parses back out of _read_settings intact
        self.write_settings({"statusLine": {"type": "command",
                                            "command": "a | b | c"}})
        statusline.install(self.settings, self.dir)
        settings, _ = _read_settings(self.settings)
        cmd = settings["statusLine"]["command"]
        self.assertEqual(statusline._strip_command(cmd), "a | b | c")


if __name__ == "__main__":
    unittest.main()
