"""csb.board: fleet.json snapshot (BridgeCore._write_fleet) and the
terminal waiting-on-you row rendered from it."""

import json
import os
import tempfile
import unittest

from csb import board
from csb.core import BridgeCore

from tests.helpers import T0, base_cfg, stub_usage


def ses(nm, st, el=0, at=False, lim=0, pj="proj"):
    return {"nm": nm, "pj": pj, "st": st, "el": el, "at": at, "lim": lim,
            "tl": ""}


class SummarizeTest(unittest.TestCase):
    def test_bridge_off(self):
        self.assertEqual(board.summarize(None, color=False),
                         "board: bridge off")

    def test_nothing_waiting(self):
        doc = {"ses": [ses("a", "run"), ses("b", "tool"), ses("c", "done")]}
        self.assertEqual(board.summarize(doc, color=False),
                         "✓ nothing waiting │ 2 running · 1 done")

    def test_waits_longest_first_and_rate_limit_not_actionable(self):
        doc = {"ses": [
            ses("short", "wait", el=65, at=True),
            ses("long", "wait", el=725, at=True),
            ses("limited", "wait", el=900, at=False, lim=T0 + 3600),
            ses("busy", "run"),
        ]}
        self.assertEqual(
            board.summarize(doc, color=False),
            "⏳ waiting on you: long 12m · short 1m │ 1 running · 1 rate-limited")

    def test_long_names_truncated(self):
        doc = {"ses": [ses("x" * 40, "wait", el=5, at=True)]}
        out = board.summarize(doc, color=False)
        self.assertIn("x" * (board.NAME_MAX - 1) + "…", out)
        self.assertNotIn("x" * board.NAME_MAX, out)

    def test_hostile_title_stays_one_clean_line(self):
        doc = {"ses": [ses("first\nsecond\x1b[31m red\x1b]0;pwn\x07 \x1b[2J",
                           "wait", el=5, at=True)]}
        out = board.summarize(doc, color=False)
        self.assertNotIn("\n", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("first second red", out)

    def test_fmt_age(self):
        self.assertEqual(board.fmt_age(None), "0s")
        self.assertEqual(board.fmt_age(59), "59s")
        self.assertEqual(board.fmt_age(3600 + 5 * 60), "1h05")


class FleetFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "fleet.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_step_writes_fresh_snapshot_board_reads(self):
        cfg = base_cfg(roots=[self.tmp.name], fleet_file=self.path)
        core = BridgeCore(cfg, usage=stub_usage(cfg))
        pkt = core.step(now=T0)
        with open(self.path, encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["ts"], T0)
        self.assertEqual(doc["ses"], pkt["ses"])
        self.assertNotIn("ts", pkt)          # wire packet unchanged
        self.assertIsNotNone(board.read_fleet(self.path, T0 + 1))
        self.assertIsNone(board.read_fleet(self.path,
                                           T0 + board.STALE_S + 1))

    def test_unwritable_path_is_swallowed(self):
        cfg = base_cfg(roots=[self.tmp.name],
                       fleet_file=os.path.join(self.tmp.name, "no", "f.json"))
        BridgeCore(cfg, usage=stub_usage(cfg)).step(now=T0)   # no raise

    def test_line_prefixes_own_context_and_never_echoes_payload(self):
        cfg = base_cfg(fleet_file=self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"ts": T0, "ses": [ses("brain", "wait", 240, True)]}, f)
        payload = json.dumps({"session_id": "abc",
                              "context_window": {"used_percentage": 33.6}})
        out = board.line(payload.encode(), cfg, now=T0 + 1,
                         capture_dir=self.tmp.name)
        self.assertTrue(out.startswith("ctx 34% │ "))
        self.assertIn("waiting on you: brain 4m", out)
        self.assertNotIn("session_id", out)

    def _cli_line(self, env, stdin=b"{}"):
        import subprocess
        import sys
        bridge = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        full = dict(os.environ, PYTHONPATH=bridge, **env)
        return subprocess.run(
            [sys.executable, "-m", "csb.board", "line",
             "--dir", self.tmp.name],
            input=stdin, capture_output=True, env=full, timeout=30)

    def test_cli_stdout_is_exactly_one_row_despite_logs(self):
        # an env override makes load_config() log; CSB_DEBUG makes the
        # collector log its capture failure on bad stdin — both to stderr
        r = self._cli_line({"CSB_DATA_DIR": self.tmp.name,
                            "CSB_FLEET_FILE": self.path, "CSB_DEBUG": "1"},
                           stdin=b"not json")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode().count("\n"), 1)
        self.assertIn("board: bridge off", r.stdout.decode())

    def test_cli_config_failure_still_prints_a_row(self):
        blocker = os.path.join(self.tmp.name, "file")
        open(blocker, "w").close()
        r = self._cli_line({"CSB_DATA_DIR": os.path.join(blocker, "sub")})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.decode(), "board: error\n")

    def test_board_lists_waits_first(self):
        cfg = base_cfg(fleet_file=self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"ts": T0, "hid": 2, "ses": [
                ses("worker", "run", 30), ses("asker", "wait", 90, True)]}, f)
        rows = board.board(cfg, now=T0).splitlines()
        self.assertTrue(rows[0].startswith("WAIT"))
        self.assertIn("asker", rows[0])
        self.assertIn("+2 idle/done hidden", rows[-1])


if __name__ == "__main__":
    unittest.main()
