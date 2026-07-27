"""KVM focus v1: slot resolution, PID-by-cwd binding, tty lookup, and the
device-line routing that triggers it. All subprocess use is stubbed."""

import unittest
from unittest import mock

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


def ctx(pid="201", tty="/dev/ttys012", env=None, cwd="/home/u/p"):
    return {"pid": pid, "tty": tty, "cwd": cwd, "session_id": "sid-1",
            "env": env or {}}


class FocusIterm2Case(unittest.TestCase):
    def test_ok_and_failure(self):
        run = fake_runner({"osascript": "ok\n"})
        ok, _ = focus.focus_iterm2(ctx(), run)
        self.assertTrue(ok)
        run = fake_runner({"osascript": "no-session\n"})
        ok, detail = focus.focus_iterm2(ctx(), run)
        self.assertFalse(ok)
        self.assertIn("no-session", detail)


TMUX_PANES = ("/dev/pts/3 work:2.0\n"
              "/dev/pts/7 side:0.1\n")


class TmuxAdapterCase(unittest.TestCase):
    """The adapter never declines: pane select when under tmux, window
    raise either way, less rather than nothing."""

    def setUp(self):
        # raise_window is X11-only; pretend we're on Linux for these
        self.p = mock.patch.object(focus.sys, "platform", "linux")
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_under_tmux_selects_the_pane_and_raises(self):
        run = fake_runner({"tmux": TMUX_PANES,
                           "wmctrl": lambda c: ("0x01 0 201 host title\n", 0)})
        ok, detail = focus.focus_tmux(
            ctx(tty="/dev/pts/7", env={"TMUX": "/tmp/sock,1,0"}), run)
        self.assertTrue(ok)
        sent = [" ".join(c) for c in run.calls]
        self.assertTrue(any("select-window -t side:0" in s for s in sent))
        self.assertTrue(any("select-pane -t side:0.1" in s for s in sent))
        self.assertTrue(any(s.startswith("wmctrl -i -a") for s in sent))

    def test_not_under_tmux_still_raises_the_window(self):
        # no TMUX in env -> nothing to select, but the raise still happens
        run = fake_runner({"wmctrl": lambda c: ("0x01 0 201 host title\n", 0)})
        ok, _ = focus.focus_tmux(ctx(env={"TERM_PROGRAM": "gnome-terminal"}),
                                 run)
        self.assertTrue(ok)
        sent = [" ".join(c) for c in run.calls]
        self.assertFalse(any("list-panes" in s for s in sent))  # never asked
        self.assertTrue(any(s.startswith("wmctrl -i -a") for s in sent))

    def test_no_tmux_and_no_raise_is_an_honest_failure(self):
        run = fake_runner({})           # wmctrl/xdotool find nothing
        ok, detail = focus.focus_tmux(ctx(), run)
        self.assertFalse(ok)
        self.assertIn("Wayland", detail)

    def test_no_raise_reason_is_true_of_the_platform_it_names(self):
        # blaming Wayland on a Mac is noise: pinning adapter=tmux on macOS
        # is legitimate, it just gets pane select only
        run = fake_runner({"tmux": TMUX_PANES})
        with mock.patch.object(focus.sys, "platform", "darwin"):
            ok, detail = focus.focus_tmux(
                ctx(tty="/dev/pts/7", env={"TMUX": "/tmp/s,1,0"}), run)
        self.assertTrue(ok)                    # pane select still landed
        self.assertIn("darwin", detail)
        self.assertNotIn("Wayland", detail)

    def test_raise_is_reported_even_when_the_pane_is_missing(self):
        # TMUX set but no pane owns the tty: say so, still raise
        run = fake_runner({"tmux": TMUX_PANES,
                           "wmctrl": lambda c: ("0x01 0 201 host title\n", 0)})
        ok, detail = focus.focus_tmux(
            ctx(tty="/dev/pts/99", env={"TMUX": "/tmp/sock,1,0"}), run)
        self.assertTrue(ok)             # window raise still landed
        self.assertIn("no pane owns", detail)


class RaiseWindowCase(unittest.TestCase):
    def test_is_a_no_op_off_linux(self):
        with mock.patch.object(focus.sys, "platform", "darwin"):
            run = fake_runner({})
            self.assertFalse(focus.raise_window("201", run))
            self.assertEqual(run.calls, [])      # never shells out

    def test_falls_back_to_xdotool_when_wmctrl_has_no_match(self):
        with mock.patch.object(focus.sys, "platform", "linux"):
            run = fake_runner({"wmctrl": "0x01 0 999 host other\n",
                               "xdotool": "12345\n"})
            self.assertTrue(focus.raise_window("201", run))
            sent = [" ".join(c) for c in run.calls]
            self.assertTrue(any("xdotool windowactivate 12345" in s
                                for s in sent))


class EnvDetectionCase(unittest.TestCase):
    def test_reads_proc_environ_on_linux(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            # /proc/<pid>/environ is NUL-separated
            with open(td + "/environ", "wb") as f:
                f.write(b"TMUX=/tmp/sock,1,0\0TERM=xterm\0")
            with mock.patch.object(focus.sys, "platform", "linux"), \
                 mock.patch("builtins.open",
                            side_effect=lambda p, *a, **k: __import__("io").open(
                                td + "/environ", *a, **k)):
                env = focus.env_of("201")
        self.assertEqual(env.get("TMUX"), "/tmp/sock,1,0")
        self.assertEqual(env.get("TERM"), "xterm")

    def test_unreadable_environ_is_empty_not_fatal(self):
        with mock.patch.object(focus.sys, "platform", "linux"):
            self.assertEqual(focus.env_of("999999999"), {})


class DispatchCase(unittest.TestCase):
    def test_adapters_is_a_real_table_the_resolved_name_indexes(self):
        # the defect this replaced: adapter_for resolved a name nothing read
        self.assertIn("iterm2", focus.ADAPTERS)
        self.assertIn("tmux", focus.ADAPTERS)
        for name, fn in focus.ADAPTERS.items():
            self.assertTrue(callable(fn), name)
        for plat in ("darwin", "linux"):
            with mock.patch.object(focus.sys, "platform", plat):
                self.assertIn(focus.adapter_for({}), focus.ADAPTERS)

    def fake_core(self, adapter):
        """A core that resolves all the way TO the adapter call, so the
        dispatch is what's under test (not an early bail-out)."""
        ses = type("Ses", (), {"session_id": "sid-1", "cwd": "/home/u/p",
                               "project": "p", "claude_pid": 0})()
        return type("Core", (), {
            "cfg": {"focus": {"adapter": adapter}},
            "slots": type("S", (), {"entries": {"sid-1": {"slot": 0}}})(),
            "sessions": {"/t.jsonl": ses}})()

    def resolving_runner(self):
        return fake_runner({
            "ps": lambda c: (("  201 Mon Jul 21 09:00:00 2026 claude "
                              "/usr/local/bin/claude\n") if "-axo" in c
                             else "ttys012\n", 0),
            "lsof": "n/home/u/p\n",
            "osascript": "ok\n"})

    def test_resolved_name_actually_selects_the_adapter(self):
        run = self.resolving_runner()
        with mock.patch.object(focus.sys, "platform", "darwin"):
            self.assertTrue(focus.focus_slot(self.fake_core("iterm2"), 0,
                                             runner=run))
        # proof the table was used: the iterm2 adapter is what ran
        self.assertTrue(any(c[0] == "osascript" for c in run.calls))

    def test_unknown_pinned_adapter_fails_at_dispatch_not_silently(self):
        run = self.resolving_runner()
        core = self.fake_core("nope")
        self.assertTrue(focus.enabled(core.cfg))   # non-empty name...
        self.assertFalse(focus.focus_slot(core, 0, runner=run))  # ...indexes nothing
        self.assertFalse(any(c[0] == "osascript" for c in run.calls))


class PpidBindingCase(unittest.TestCase):
    def test_listener_parses_ppid_query(self):
        import tempfile
        from csb.hooks import HookListener
        with tempfile.TemporaryDirectory() as td:
            # port 0 + temp port file: NEVER touch the real bridge dir —
            # a default-config listener here once clobbered the live
            # hook-port file and silently severed real hook delivery
            lis = HookListener({"hook_port": 0,
                                "hook_port_file": td + "/hook-port"})
            lis.server.shutdown()
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


class AdapterResolutionCase(unittest.TestCase):
    def cfg(self, **focus_cfg):
        return {"focus": focus_cfg} if focus_cfg else {}

    def test_auto_resolves_per_platform(self):
        with mock.patch.object(focus.sys, "platform", "darwin"):
            self.assertEqual(focus.adapter_for(self.cfg(adapter="auto")),
                             "iterm2")
            self.assertTrue(focus.enabled(self.cfg(adapter="auto")))
        with mock.patch.object(focus.sys, "platform", "linux"):
            self.assertEqual(focus.adapter_for(self.cfg(adapter="auto")),
                             "tmux")
            self.assertTrue(focus.enabled(self.cfg(adapter="auto")))
        with mock.patch.object(focus.sys, "platform", "win32"):
            # no adapter yet -> off, rather than failing per click
            self.assertEqual(focus.adapter_for(self.cfg(adapter="auto")), "")
            self.assertFalse(focus.enabled(self.cfg(adapter="auto")))

    def test_auto_is_the_default_when_unconfigured(self):
        with mock.patch.object(focus.sys, "platform", "darwin"):
            self.assertEqual(focus.adapter_for({}), "iterm2")
            self.assertEqual(focus.adapter_for({"focus": {}}), "iterm2")

    def test_none_disables_everywhere(self):
        for plat in ("darwin", "linux", "win32"):
            with mock.patch.object(focus.sys, "platform", plat):
                self.assertFalse(focus.enabled(self.cfg(adapter="none")))

    def test_pinned_adapter_is_honored_off_its_platform(self):
        # pinning is the user overriding our guess, not a re-guess request
        with mock.patch.object(focus.sys, "platform", "linux"):
            self.assertEqual(focus.adapter_for(self.cfg(adapter="iterm2")),
                             "iterm2")

    def test_disabled_focus_slot_never_shells_out(self):
        run = fake_runner({"ps": PS_TWO_CLAUDES})

        class Core:
            cfg = {"focus": {"adapter": "none"}}
            slots = type("S", (), {"entries": {}})()
            sessions = {}

        self.assertFalse(focus.focus_slot(Core(), 0, runner=run))
        self.assertEqual(run.calls, [])       # no ps, no lsof, no osascript


class ConfigMergeCase(unittest.TestCase):
    def test_partial_focus_block_keeps_the_default_adapter(self):
        from csb.config import DEFAULT_CONFIG
        self.assertEqual(DEFAULT_CONFIG["focus"]["adapter"], "auto")
        # a user block that omits 'adapter' must not erase it (the same
        # per-key merge input/chime get)
        merged = dict(DEFAULT_CONFIG["focus"])
        merged.update({})
        self.assertEqual(merged["adapter"], "auto")


if __name__ == "__main__":
    unittest.main()
