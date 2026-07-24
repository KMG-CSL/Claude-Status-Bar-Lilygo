"""Config loading: CSB_* env overrides with type coercion, CSB_DATA_DIR,
bad/unknown config.json handling, input deep-merge."""

import contextlib
import io
import json
import os
import tempfile
import unittest

from csb import config as config_mod
from csb.config import DEFAULT_CONFIG, data_dir, load_config

_CSB_VARS = [k for k in os.environ if k.startswith("CSB_")]


class EnvCase(unittest.TestCase):
    """Isolates CSB_* env vars and points CSB_DATA_DIR at a temp dir so
    load_config never sees the developer's real config.json."""

    def setUp(self):
        self._saved = {k: os.environ.pop(k) for k in list(os.environ)
                       if k.startswith("CSB_")}
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["CSB_DATA_DIR"] = self.tmp.name

    def tearDown(self):
        for k in list(os.environ):
            if k.startswith("CSB_"):
                del os.environ[k]
        os.environ.update(self._saved)
        self.tmp.cleanup()

    def write_cfg(self, obj):
        with open(os.path.join(self.tmp.name, "config.json"), "w") as f:
            json.dump(obj, f)


class TestDataDir(EnvCase):
    def test_env_override_wins(self):
        self.assertEqual(data_dir(), self.tmp.name)

    def test_default_is_bridge_dir(self):
        del os.environ["CSB_DATA_DIR"]
        d = data_dir()
        self.assertTrue(os.path.exists(os.path.join(d, "claude_bar_bridge.py")))


class TestLoadConfig(EnvCase):
    def test_defaults_when_no_file(self):
        cfg = load_config()
        for key in ("roots", "port", "baud", "max_sessions", "active_window_min",
                    "idle_after_s", "wait_tool_s", "context_limit",
                    "done_after_s", "question_after_s", "send_interval_s",
                    "subagent_live_s", "subagent_cache_s", "input"):
            self.assertIn(key, cfg)
        self.assertEqual(cfg["subagent_live_s"], 15)
        self.assertEqual(cfg["subagent_cache_s"], 10)

    def test_user_file_merge_and_input_deep_merge(self):
        self.write_cfg({"baud": 57600, "input": {"tap": "page"}})
        cfg = load_config()
        self.assertEqual(cfg["baud"], 57600)
        self.assertEqual(cfg["input"]["tap"], "page")
        # untouched input keys keep their defaults
        self.assertEqual(cfg["input"]["hold"], "usage")
        self.assertEqual(cfg["input"]["boot_long"], "flip")

    def test_user_file_does_not_mutate_defaults(self):
        self.write_cfg({"input": {"tap": "page"}})
        load_config()
        self.assertEqual(DEFAULT_CONFIG["input"]["tap"], "cycle")

    def test_bad_json_warns_and_uses_defaults(self):
        with open(os.path.join(self.tmp.name, "config.json"), "w") as f:
            f.write("{not json")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cfg = load_config()
        self.assertEqual(cfg["baud"], DEFAULT_CONFIG["baud"])
        self.assertIn("bad config.json", out.getvalue())

    def test_unknown_key_warns_but_still_applies(self):
        self.write_cfg({"wait_tool_z": 5})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cfg = load_config()
        self.assertIn("wait_tool_z", out.getvalue())
        self.assertEqual(cfg["wait_tool_z"], 5)   # warning only, no drop

    def test_approval_silence_default(self):
        cfg = load_config()
        self.assertEqual(cfg["approval_silence_s"], 20)
        self.assertEqual(cfg["approval_confirm_s"], 0.5)

    def test_legacy_wait_tool_s_feeds_approval_silence(self):
        # a user who tuned wait_tool_s before the rename keeps their debounce
        self.write_cfg({"wait_tool_s": 7})
        self.assertEqual(load_config()["approval_silence_s"], 7)

    def test_explicit_approval_silence_beats_legacy(self):
        self.write_cfg({"wait_tool_s": 7, "approval_silence_s": 3.5})
        self.assertEqual(load_config()["approval_silence_s"], 3.5)

    def test_underscore_keys_are_comment_exempt(self):
        self.write_cfg({"_comment": "hi"})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            load_config()
        self.assertNotIn("_comment", out.getvalue())


class TestEnvOverrides(EnvCase):
    def _load_quiet(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return load_config()

    def test_int_coercion(self):
        os.environ["CSB_BAUD"] = "9600"
        self.assertEqual(self._load_quiet()["baud"], 9600)

    def test_float_coercion(self):
        os.environ["CSB_SEND_INTERVAL_S"] = "0.25"
        self.assertEqual(self._load_quiet()["send_interval_s"], 0.25)

    def test_approval_silence_env_override(self):
        os.environ["CSB_APPROVAL_SILENCE_S"] = "3.5"
        self.assertEqual(self._load_quiet()["approval_silence_s"], 3.5)

    def test_non_json_stays_string(self):
        os.environ["CSB_PORT"] = "COM7"
        self.assertEqual(self._load_quiet()["port"], "COM7")

    def test_list_coercion(self):
        os.environ["CSB_ROOTS"] = json.dumps(["/a", "/b"])
        self.assertEqual(self._load_quiet()["roots"], ["/a", "/b"])

    def test_plain_string_roots_becomes_list(self):
        # The natural single-root usage: CSB_ROOTS=/some/path (no JSON).
        # Must NOT stay a str, or find_transcripts iterates it char-by-char
        # and walks "/" (the entire filesystem).
        os.environ["CSB_ROOTS"] = "/some/path"
        self.assertEqual(self._load_quiet()["roots"], ["/some/path"])

    def test_pathsep_separated_roots(self):
        os.environ["CSB_ROOTS"] = os.pathsep.join(["/a", "/b"])
        self.assertEqual(self._load_quiet()["roots"], ["/a", "/b"])

    def test_string_roots_in_config_file(self):
        self.write_cfg({"roots": "/some/path"})
        self.assertEqual(self._load_quiet()["roots"], ["/some/path"])

    def test_non_list_non_string_roots_falls_back_to_defaults(self):
        os.environ["CSB_ROOTS"] = "42"   # json-parses to int
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cfg = load_config()
        self.assertEqual(cfg["roots"], DEFAULT_CONFIG["roots"])
        self.assertIn("roots", out.getvalue())

    def test_input_subkey(self):
        os.environ["CSB_INPUT_TAP"] = "flip"
        cfg = self._load_quiet()
        self.assertEqual(cfg["input"]["tap"], "flip")
        self.assertEqual(cfg["input"]["swipe"], "cycle")

    def test_chime_subkey(self):
        os.environ["CSB_CHIME_WAIT"] = "/snd/ding.aiff"
        cfg = self._load_quiet()
        self.assertEqual(cfg["chime"]["wait"], "/snd/ding.aiff")
        self.assertEqual(cfg["chime"]["done"], "")

    def test_chime_deep_merge_from_file(self):
        self.write_cfg({"chime": {"done": "/snd/done.aiff"}})
        cfg = self._load_quiet()
        self.assertEqual(cfg["chime"]["done"], "/snd/done.aiff")
        self.assertEqual(cfg["chime"]["wait"], "")   # untouched default

    def test_env_beats_config_file(self):
        self.write_cfg({"baud": 57600})
        os.environ["CSB_BAUD"] = "9600"
        self.assertEqual(self._load_quiet()["baud"], 9600)

    def test_override_is_logged(self):
        os.environ["CSB_BAUD"] = "9600"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            load_config()
        self.assertIn("CSB_BAUD", out.getvalue())


class TestDebugFlag(EnvCase):
    def test_debug_off_by_default(self):
        self.assertFalse(config_mod.debug_enabled())
        os.environ["CSB_DEBUG"] = "0"
        self.assertFalse(config_mod.debug_enabled())

    def test_debug_on(self):
        os.environ["CSB_DEBUG"] = "1"
        self.assertTrue(config_mod.debug_enabled())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            config_mod.debug("t", "visible")
        self.assertIn("visible", out.getvalue())


if __name__ == "__main__":
    unittest.main()
