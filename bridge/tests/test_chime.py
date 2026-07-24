"""Opt-in chime: plays only on a real transition into wait/done, never on
the first packet after startup, one sound per 10s globally, subprocess
mocked — no sound, no processes."""

import unittest
from unittest import mock

from csb.chime import PLAYER, RATE_LIMIT_S, Chimer

from tests.helpers import T0, base_cfg


def chimer(wait="/snd/wait.aiff", done="/snd/done.aiff"):
    return Chimer(base_cfg(chime={"wait": wait, "done": done}))


class ChimeCase(unittest.TestCase):
    def setUp(self):
        p = mock.patch("csb.chime.subprocess")
        self.subprocess = p.start()
        self.addCleanup(p.stop)
        # keep the tests honest on any dev box: never the Windows no-op
        p2 = mock.patch("csb.chime.IS_WINDOWS", False)
        p2.start()
        self.addCleanup(p2.stop)

    def played(self):
        return [c.args[0] for c in self.subprocess.Popen.call_args_list]


class TestChime(ChimeCase):
    def test_first_packet_after_startup_never_plays(self):
        c = chimer()
        c.observe({"a": "wait", "b": "done"}, now=T0)
        self.assertEqual(self.played(), [])

    def test_transition_into_wait_plays_the_wait_sound(self):
        c = chimer()
        c.observe({"a": "run"}, now=T0)
        c.observe({"a": "wait"}, now=T0 + 1)
        self.assertEqual(self.played(), [[PLAYER, "/snd/wait.aiff"]])

    def test_transition_into_done_plays_the_done_sound(self):
        c = chimer()
        c.observe({"a": "tool"}, now=T0)
        c.observe({"a": "done"}, now=T0 + 1)
        self.assertEqual(self.played(), [[PLAYER, "/snd/done.aiff"]])

    def test_steady_state_never_replays(self):
        c = chimer()
        c.observe({"a": "run"}, now=T0)
        for dt in range(1, 40):
            c.observe({"a": "wait"}, now=T0 + dt)
        self.assertEqual(len(self.played()), 1)

    def test_rate_limited_to_one_sound_per_window(self):
        c = chimer()
        c.observe({"a": "run", "b": "run"}, now=T0)
        c.observe({"a": "wait", "b": "run"}, now=T0 + 1)
        c.observe({"a": "wait", "b": "wait"}, now=T0 + 5)   # inside window
        self.assertEqual(len(self.played()), 1)
        c.observe({"a": "run", "b": "wait"}, now=T0 + 6)
        c.observe({"a": "wait", "b": "wait"},
                  now=T0 + 1 + RATE_LIMIT_S + 1)            # window passed
        self.assertEqual(len(self.played()), 2)

    def test_unconfigured_sound_is_silent(self):
        c = Chimer(base_cfg())                              # defaults: ""
        c.observe({"a": "run"}, now=T0)
        c.observe({"a": "wait"}, now=T0 + 1)
        self.assertEqual(self.played(), [])

    def test_brand_new_session_is_not_a_transition(self):
        c = chimer()
        c.observe({"a": "run"}, now=T0)
        c.observe({"a": "run", "b": "wait"}, now=T0 + 1)    # b just appeared
        self.assertEqual(self.played(), [])

    def test_player_failure_is_swallowed(self):
        self.subprocess.Popen.side_effect = OSError("no afplay")
        c = chimer()
        c.observe({"a": "run"}, now=T0)
        c.observe({"a": "wait"}, now=T0 + 1)                # must not raise

    def test_fire_and_forget_never_waits(self):
        c = chimer()
        c.observe({"a": "run"}, now=T0)
        c.observe({"a": "wait"}, now=T0 + 1)
        self.subprocess.Popen.return_value.wait.assert_not_called()
        self.subprocess.run.assert_not_called()


class TestCoreWiring(unittest.TestCase):
    def test_step_feeds_the_chimer(self):
        import tempfile

        from csb.core import BridgeCore
        from tests.helpers import stub_usage

        with tempfile.TemporaryDirectory() as tmp:
            cfg = base_cfg(roots=[tmp], hooks_enabled=False)
            core = BridgeCore(cfg, usage=stub_usage(cfg))
            with mock.patch.object(core.chimer, "observe") as obs:
                core.step(now=T0)
            obs.assert_called_once()


if __name__ == "__main__":
    unittest.main()
