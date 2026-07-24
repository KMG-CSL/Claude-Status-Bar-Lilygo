"""KVM focus v1: slot resolution, PID-by-cwd binding, tty lookup, and the
device-line routing that triggers it. All subprocess use is stubbed."""

import unittest

from csb import focus


def fake_runner(responses):
    """runner(cmd) -> canned result keyed by cmd[0]; records calls."""
    calls = []

    class R:
        def __init__(self, stdout, returncode=0):
            self.stdout = stdout
            self.stderr = ""
            self.returncode = returncode

    def run(cmd, timeout=5):
        calls.append(cmd)
        key = cmd[0]
        v = responses.get(key, "")
        if callable(v):
            return R(*v(cmd))
        return R(v)

    run.calls = calls
    return run


PS_TWO_CLAUDES = (
    "  201 Mon Jul 21 09:00:00 2026 claude /usr/local/bin/claude\n"
    "  202 Mon Jul 21 10:00:00 2026 claude /usr/local/bin/claude\n"
    "  303 Mon Jul 21 10:00:00 2026 vim /usr/bin/vim\n"
    "  404 Mon Jul 21 10:00:00 2026 claude claude --version\n"
)


class PidBindingCase(unittest.TestCase):
    def test_matches_only_claude_pids_with_cwd(self):
        def lsof(cmd):
            pid = cmd[-1]
            cwd = "/Users/u/Brain" if pid in ("201", "202") else "/elsewhere"
            return (f"p{pid}\nn{cwd}\n", 0)

        run = fake_runner({"ps": PS_TWO_CLAUDES, "lsof": lsof})
        self.assertEqual(focus.claude_pids_by_cwd("/Users/u/Brain", run),
                         ["201", "202"])
        self.assertEqual(focus.claude_pids_by_cwd("/nope", run), [])

    def test_version_probe_excluded(self):
        run = fake_runner({"ps": PS_TWO_CLAUDES,
                           "lsof": lambda cmd: ("n/x\n", 0)})
        focus.claude_pids_by_cwd("/x", run)
        probed = [c[-1] for c in run.calls if c[0] == "lsof"]
        self.assertNotIn("404", probed)   # --version never lsof'd

    def test_tty_normalization(self):
        run = fake_runner({"ps": "ttys012\n"})
        self.assertEqual(focus.tty_of("201", run), "/dev/ttys012")
        run = fake_runner({"ps": "??\n"})
        self.assertEqual(focus.tty_of("201", run), "")


class FocusTtyCase(unittest.TestCase):
    def test_ok_and_failure(self):
        run = fake_runner({"osascript": "ok\n"})
        ok, _ = focus.focus_tty("/dev/ttys012", run)
        self.assertTrue(ok)
        run = fake_runner({"osascript": "no-session\n"})
        ok, detail = focus.focus_tty("/dev/ttys012", run)
        self.assertFalse(ok)
        self.assertIn("no-session", detail)


class PpidBindingCase(unittest.TestCase):
    def test_listener_parses_ppid_query(self):
        from csb.hooks import HookListener
        lis = HookListener()
        H = lis._handler_class()

        class Fake(H):
            def __init__(self, path, body):
                self.path = path
                self.headers = {"Content-Length": str(len(body))}
                import io
                self.rfile = io.BytesIO(body)
                self.sent = []
                self.do_POST()

            def send_response(self, code):
                self.sent.append(code)

            def end_headers(self):
                pass

        Fake('/hook?ppid=4242', b'{"session_id":"s1","hook_event_name":"Stop"}')
        ev = lis.queue.get_nowait()
        self.assertEqual(ev.get("ppid"), 4242)

        Fake('/hook', b'{"session_id":"s2","hook_event_name":"Stop"}')
        ev = lis.queue.get_nowait()
        self.assertNotIn("ppid", ev)


class DeviceLineCase(unittest.TestCase):
    def test_non_json_and_other_packets_ignored(self):
        from csb.core import BridgeCore
        from tests.helpers import base_cfg
        core = BridgeCore(base_cfg())
        core.handle_device_line("[touch] tap (dy=1 dt=100)")   # no crash
        core.handle_device_line('{"t":"other"}')

    def test_focus_routes_to_focus_slot(self):
        from csb.core import BridgeCore
        from tests.helpers import base_cfg
        core = BridgeCore(base_cfg())
        seen = {}
        orig = focus.focus_slot
        focus.focus_slot = lambda c, sl, runner=None: seen.setdefault("sl", sl)
        try:
            core.handle_device_line('{"t":"focus","sl":3}')
        finally:
            focus.focus_slot = orig
        self.assertEqual(seen.get("sl"), 3)


if __name__ == "__main__":
    unittest.main()
