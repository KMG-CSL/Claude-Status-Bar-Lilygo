"""Wave 3 (Item 1) — hooks-push pipeline.

Listener: localhost-only HTTP server, auto-port + port-file contract,
recorded-style payloads POSTed over real HTTP and routed through
BridgeCore.step(). Engine: hook evidence tier — instant waiting states,
perm-prompt latch clearing, authoritative turn edges, per-session
hook-freshness fallback. Installer: idempotent merge into a temp
settings.json, never ~/.claude.
"""

import http.client
import io
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from csb import hooks as hooks_mod
from csb.core import BridgeCore
from csb.engine import derive
from csb.hooks import HookListener

from tests.helpers import (T0, append_jsonl, assistant_text,
                           assistant_tool_use, base_cfg, hook_event,
                           hook_payload, make_session, seed_model_cache,
                           stub_usage, tool_result, user, write_jsonl)

ALL_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit", "Stop",
              "SubagentStop", "PreToolUse", "PostToolUse", "Notification")


# --------------------------------------------------------------- listener

class ListenerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.port_file = os.path.join(self.tmp.name, "hook-port")
        self.cfg = base_cfg(roots=[self.tmp.name], hook_port=0,
                            hook_port_file=self.port_file)
        self.listener = HookListener(self.cfg)
        self.addCleanup(self.listener.close)

    def post(self, payload, raw=None):
        body = raw if raw is not None else json.dumps(payload).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.listener.port,
                                          timeout=5)
        try:
            conn.request("POST", "/hook", body)
            resp = conn.getresponse()
            resp.read()
            return resp.status
        finally:
            conn.close()


class TestListener(ListenerCase):
    def test_port_file_holds_the_actual_port(self):
        # temp-dir port file, never ~/.claude
        with open(self.port_file, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), str(self.listener.port))
        self.assertTrue(self.port_file.startswith(self.tmp.name))

    def test_preferred_port_conflict_falls_back_to_auto(self):
        # occupy a port, prefer it -> auto-port fallback, port file correct
        blocker = socket.socket()
        self.addCleanup(blocker.close)
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken = blocker.getsockname()[1]
        pf2 = os.path.join(self.tmp.name, "hook-port-2")
        second = HookListener(base_cfg(hook_port=taken, hook_port_file=pf2))
        self.addCleanup(second.close)
        self.assertNotEqual(second.port, taken)
        with open(pf2, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), str(second.port))

    def test_all_eight_events_parse_via_http(self):
        path = os.path.join(self.tmp.name, "s.jsonl")
        for name in ALL_EVENTS:
            extra = {}
            if name == "PreToolUse":
                extra = {"tool_name": "ExitPlanMode",
                         "tool_input": {"plan": "Step 1"}}
            elif name == "Notification":
                extra = {"message": "Claude needs your permission to use Bash"}
            elif name == "UserPromptSubmit":
                extra = {"prompt": "fix the widget"}
            self.assertEqual(self.post(hook_payload(name, path, **extra)), 204)
        got = []
        while not self.listener.queue.empty():
            got.append(self.listener.queue.get_nowait())
        self.assertEqual([e["hook_event_name"] for e in got], list(ALL_EVENTS))
        for e in got:
            self.assertEqual(e["transcript_path"], path)
            self.assertEqual(e["session_id"], "sid-1")
            self.assertEqual(e["cwd"], "/home/u/projects/widget")
            self.assertIn("tool_name", e)
            self.assertIsInstance(e["ts"], float)

    def test_bad_json_is_ignored_and_does_not_wedge(self):
        self.assertEqual(self.post(None, raw=b"this is not json"), 204)
        self.assertTrue(self.listener.queue.empty())
        self.assertEqual(
            self.post(hook_payload("Stop", "/tmp/s.jsonl")), 204)
        self.assertEqual(self.listener.queue.get_nowait()["hook_event_name"],
                         "Stop")

    def test_self_probe_events_are_dropped(self):
        self.post(hook_payload("Stop", "/tmp/s.jsonl",
                               cwd="/tmp/probes/csb-self-probe"))
        self.assertTrue(self.listener.queue.empty())


# ------------------------------------------- HTTP -> BridgeCore pipeline

class PipelineCase(ListenerCase):
    """Recorded-style payloads over real HTTP, drained by BridgeCore.step.
    Uses the real clock: every assertion sits far from any threshold."""

    def setUp(self):
        super().setUp()
        seed_model_cache()
        self.core = BridgeCore(self.cfg, usage=stub_usage(self.cfg),
                               hook_queue=self.listener.queue)

    def transcript(self, records, filename="live.jsonl"):
        path = os.path.join(self.tmp.name, filename)
        write_jsonl(path, records)
        return path

    def entry(self, pkt, path):
        by_path = {s.path: i for i, s in enumerate(
            sorted(self.core.sessions.values(), key=lambda s: s.first_seen))}
        del by_path  # sessions may be filtered; match on name instead
        self.assertEqual(len(pkt["ses"]), 1)
        return pkt["ses"][0]


class TestPipeline(PipelineCase):
    def test_eager_session_first_turn_never_lies(self):
        # UserPromptSubmit for a transcript that does not exist yet ->
        # eager Session, visible, st=run; the Stop then reports done —
        # never done-before-run (§c)
        path = os.path.join(self.tmp.name, "unborn.jsonl")
        self.post(hook_payload("UserPromptSubmit", path, prompt="go"))
        pkt = self.core.step()
        self.assertIn(path, self.core.sessions)
        e = self.entry(pkt, path)
        self.assertEqual(e["st"], "run")
        self.assertEqual(e["src"], "h")
        self.assertEqual(e["pj"], "widget")      # seeded from the payload cwd
        self.post(hook_payload("Stop", path))
        e = self.entry(self.core.step(), path)
        self.assertEqual(e["st"], "done")

    def test_notification_permission_prompt_is_instant_wait(self):
        t = time.time()
        path = self.transcript([user(t - 4), assistant_tool_use(t - 1, "Bash",
                                                                "tu_1")])
        e = self.entry(self.core.step(), path)
        self.assertEqual(e["st"], "tool")        # 1s silence: below debounce
        self.post(hook_payload("Notification", path,
                               message="Claude needs your permission"))
        e = self.entry(self.core.step(), path)
        self.assertEqual((e["st"], e["src"]), ("wait", "h"))  # no debounce
        self.assertEqual(e["tl"], "Bash")
        # approval: the tool runs and the transcript moves (after the
        # prompt — anchor to the latch ts, the test appends sub-ms after
        # the POST) -> latch clears
        p = self.core.sessions[path].perm_prompt_at
        append_jsonl(path, [tool_result(p + 0.5, "tu_1"),
                            assistant_text(p + 1, text="ran fine")])
        e = self.entry(self.core.step(), path)
        self.assertEqual(e["st"], "run")

    def test_pre_tool_use_plan_approval_then_post_tool_use(self):
        t = time.time()
        path = self.transcript([user(t - 3), assistant_text(t - 1)])
        self.post(hook_payload("UserPromptSubmit", path, prompt="plan it"))
        self.post(hook_payload("PreToolUse", path, tool_name="ExitPlanMode",
                               tool_input={"plan": "Step 1"}))
        e = self.entry(self.core.step(), path)
        self.assertEqual((e["st"], e["src"]), ("wait", "h"))
        self.post(hook_payload("PostToolUse", path, tool_name="ExitPlanMode"))
        e = self.entry(self.core.step(), path)
        self.assertEqual(e["st"], "run")         # approved, turn continues

    def test_stop_clears_abandoned_tool_use(self):
        t = time.time()
        path = self.transcript([user(t - 5),
                                assistant_tool_use(t - 2, "Bash", "tu_esc")])
        self.core.step()
        self.post(hook_payload("Stop", path))    # user hit Escape
        e = self.entry(self.core.step(), path)
        self.assertEqual(e["st"], "done")
        self.assertEqual(self.core.sessions[path].pending_ids, {})

    def test_subagent_stop_forces_recount(self):
        t = time.time()
        path = self.transcript([user(t - 5), assistant_text(t - 1)])
        self.core.step()
        s = self.core.sessions[path]
        s._sa_count, s._sa_checked = 3, time.time() + 1000   # cache pinned
        e = self.entry(self.core.step(), path)
        self.assertEqual(e["sa"], 3)
        self.post(hook_payload("SubagentStop", path))
        e = self.entry(self.core.step(), path)
        self.assertEqual(e["sa"], 0)             # recounted, no live files

    def test_session_id_fallback_routes_pathless_events(self):
        t = time.time()
        path = self.transcript([user(t - 5), assistant_text(t - 1)])
        self.post(hook_payload("UserPromptSubmit", path, session_id="sid-9"))
        self.core.step()
        # a later event with no transcript_path routes via session_id
        self.post(hook_payload("Stop", "", session_id="sid-9"))
        e = self.entry(self.core.step(), path)
        self.assertEqual(e["st"], "done")

    def test_session_end_recorded(self):
        t = time.time()
        path = self.transcript([user(t - 5), assistant_text(t - 1)])
        self.core.step()
        self.post(hook_payload("SessionEnd", path, reason="exit"))
        self.core.step()
        self.assertIsNotNone(self.core.sessions[path].session_ended_at)


# ------------------------------------------------------ engine hook tier

class EngineHookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = base_cfg()

    def make(self, records, mtime, **kw):
        return make_session(self.tmp.name, records, mtime=mtime, **kw)

    def at(self, session, now):
        return derive(session, self.cfg, now)


class TestHookPrecedence(EngineHookCase):
    def test_prompt_edge_beats_transcript_done(self):
        # tier 2 would say done (quiet 35s); a newer UserPromptSubmit edge
        # says the turn is running -> the edge wins
        s = self.make([user(T0), assistant_text(T0 + 10)], mtime=T0 + 10)
        self.assertEqual(self.at(s, T0 + 45).st, "done")
        s.apply_hook(hook_event("UserPromptSubmit", T0 + 44,
                                transcript_path=s.path))
        r = self.at(s, T0 + 45)
        self.assertEqual((r.st, r.src), ("run", "h"))

    def test_stop_edge_beats_transcript_run(self):
        # tier 2 would say run (quiet 2s); the Stop edge ends the turn now
        s = self.make([user(T0), assistant_text(T0 + 10, text="all done.")],
                      mtime=T0 + 10)
        s.apply_hook(hook_event("UserPromptSubmit", T0, transcript_path=s.path))
        s.apply_hook(hook_event("Stop", T0 + 11, transcript_path=s.path))
        r = self.at(s, T0 + 12)
        self.assertEqual((r.st, r.src, r.el), ("done", "h", 10))

    def test_stop_edge_does_not_negate_a_trailing_question(self):
        # §b step 5: Stop confirms the turn ended; the "?" heuristic still
        # flips to wait after question_after_s (12)
        s = self.make([user(T0), assistant_text(T0 + 10,
                                                text="should I deploy?")],
                      mtime=T0 + 10)
        s.apply_hook(hook_event("UserPromptSubmit", T0, transcript_path=s.path))
        s.apply_hook(hook_event("Stop", T0 + 11, transcript_path=s.path))
        self.assertEqual(self.at(s, T0 + 12).st, "done")     # quiet 1s
        r = self.at(s, T0 + 11 + 12.5)                        # quiet 12.5s
        self.assertEqual((r.st, r.src), ("wait", "h"))

    def test_hook_fresh_silence_suppression(self):
        # pending Bash, 60s of write silence, no perm_prompt edge ->
        # positive evidence of auto-approval: stays tool, never "approval"
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        s.apply_hook(hook_event("UserPromptSubmit", T0, transcript_path=s.path))
        r = self.at(s, T0 + 65)
        self.assertEqual((r.st, r.tl, r.src), ("tool", "Bash", "h"))

    def test_hook_freshness_boundary_restores_tier2(self):
        # hook_fresh_s = 900, strict: inside -> hook tier, outside -> the
        # exact transcript/mtime behavior of an uninstrumented session
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        s.apply_hook(hook_event("UserPromptSubmit", T0, transcript_path=s.path))
        self.assertEqual(self.at(s, T0 + 899).st, "tool")     # hook-fresh
        r = self.at(s, T0 + 901)                              # hooks stale
        # the pending tool_use is transcript evidence, hence src="t"
        self.assertEqual((r.st, r.src), ("wait", "t"))        # stale pending

    def test_freshness_is_per_session(self):
        # one instrumented session must not change an uninstrumented one:
        # the plain session keeps the 20s approval flip
        inst = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                         mtime=T0 + 5, filename="instrumented.jsonl")
        inst.apply_hook(hook_event("UserPromptSubmit", T0,
                                   transcript_path=inst.path))
        plain = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_2")],
                          mtime=T0 + 5, filename="plain.jsonl")
        self.assertEqual(self.at(inst, T0 + 30).st, "tool")
        self.assertEqual(self.at(plain, T0 + 30).st, "wait")
        self.assertEqual(self.at(plain, T0 + 30).src, "t")


class TestPermPromptLatch(EngineHookCase):
    def _pending(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_1")],
                      mtime=T0 + 5)
        s.apply_hook(hook_event("UserPromptSubmit", T0, transcript_path=s.path))
        return s

    def test_notification_arms_instant_wait(self):
        s = self._pending()
        s.apply_hook(hook_event("Notification", T0 + 6,
                                transcript_path=s.path))
        r = self.at(s, T0 + 6.5)                 # sub-second, no debounce
        self.assertEqual((r.st, r.tl, r.src), ("wait", "Bash", "h"))

    def test_pre_tool_use_matcher_tools_arm(self):
        for tool in ("ExitPlanMode", "AskUserQuestion"):
            s = self.make([user(T0), assistant_text(T0 + 3)], mtime=T0 + 3,
                          filename=f"pre-{tool}.jsonl")
            s.apply_hook(hook_event("UserPromptSubmit", T0,
                                    transcript_path=s.path))
            s.apply_hook(hook_event("PreToolUse", T0 + 4,
                                    transcript_path=s.path, tool_name=tool))
            self.assertEqual(self.at(s, T0 + 4.5).st, "wait", tool)

    def test_pre_tool_use_other_tools_do_not_arm(self):
        s = self.make([user(T0), assistant_text(T0 + 3)], mtime=T0 + 3)
        s.apply_hook(hook_event("UserPromptSubmit", T0, transcript_path=s.path))
        s.apply_hook(hook_event("PreToolUse", T0 + 4, transcript_path=s.path,
                                tool_name="Bash"))
        self.assertEqual(self.at(s, T0 + 4.5).st, "run")

    def test_cleared_by_post_tool_use(self):
        s = self._pending()
        s.apply_hook(hook_event("Notification", T0 + 6, transcript_path=s.path))
        s.apply_hook(hook_event("PostToolUse", T0 + 8, transcript_path=s.path,
                                tool_name="Bash"))
        self.assertEqual(self.at(s, T0 + 9).st, "tool")   # approved, executing

    def test_cleared_by_transcript_activity(self):
        # approving emits no UserPromptSubmit: the tool_result + assistant
        # output after the prompt must release the latch (never stuck-wait)
        s = self._pending()
        s.apply_hook(hook_event("Notification", T0 + 6, transcript_path=s.path))
        append_jsonl(s.path, [tool_result(T0 + 9, "tu_1"),
                              assistant_text(T0 + 10, text="ran fine")])
        s.poll([])
        self.assertEqual(self.at(s, T0 + 11).st, "run")
        self.assertIsNone(s.perm_prompt_at)

    def test_cleared_by_stop(self):
        s = self._pending()
        s.apply_hook(hook_event("Notification", T0 + 6, transcript_path=s.path))
        s.apply_hook(hook_event("Stop", T0 + 9, transcript_path=s.path))
        r = self.at(s, T0 + 10)
        self.assertEqual(r.st, "done")           # turn over, prompt gone
        self.assertIsNone(s.perm_prompt_at)

    def test_cleared_by_user_prompt_submit(self):
        s = self._pending()
        s.apply_hook(hook_event("Notification", T0 + 6, transcript_path=s.path))
        s.apply_hook(hook_event("UserPromptSubmit", T0 + 9,
                                transcript_path=s.path))
        self.assertIsNone(s.perm_prompt_at)

    def test_cleared_by_session_start(self):
        s = self._pending()
        s.apply_hook(hook_event("Notification", T0 + 6, transcript_path=s.path))
        s.apply_hook(hook_event("SessionStart", T0 + 9,
                                transcript_path=s.path))
        self.assertIsNone(s.perm_prompt_at)
        self.assertEqual(s.pending_ids, {})      # inherited wait state gone


class TestSessionStartReset(EngineHookCase):
    def test_clears_inherited_wait_and_sticky_state(self):
        # previous incarnation left an unanswered tool_use (would be a
        # stuck orange tile); SessionStart resets it (happy)
        s = self.make([user(T0), assistant_tool_use(T0 + 5, "Bash", "tu_old")],
                      mtime=T0 + 5)
        self.assertEqual(self.at(s, T0 + 40).st, "wait")
        s.apply_hook(hook_event("SessionStart", T0 + 41,
                                transcript_path=s.path))
        r = self.at(s, T0 + 42)
        self.assertNotEqual(r.st, "wait")
        self.assertEqual(s.pending_ids, {})
        self.assertIsNone(s.done_latch)
        self.assertIsNone(s.turn_started_at)


# -------------------------------------------------------------- installer

class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = os.path.join(self.tmp.name, "settings.json")
        self.port_file = os.path.join(self.tmp.name, "hook-port")

    def install(self):
        return hooks_mod.install(self.settings, port_file=self.port_file)

    def read(self):
        with open(self.settings, "r", encoding="utf-8") as f:
            return f.read()

    def write_fixture(self, obj):
        with open(self.settings, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2) + "\n")

    USER_FIXTURE = {
        "model": "opus",
        "permissions": {"allow": ["Bash(npm test)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash",
                            "hooks": [{"type": "command",
                                       "command": "echo user-pre"}]}],
            "Stop": [{"hooks": [{"type": "command",
                                 "command": "afplay ding.aiff"}]}],
        },
    }


class TestInstaller(InstallerCase):
    def test_install_registers_exactly_the_eight_events(self):
        self.assertEqual(self.install(), 0)
        cfg = json.loads(self.read())
        self.assertEqual(sorted(cfg["hooks"]), sorted(ALL_EVENTS))
        for name, groups in cfg["hooks"].items():
            self.assertEqual(len(groups), 1)
            g = groups[0]
            self.assertEqual(g["_tag"], "claudestatusbar")
            cmd = g["hooks"][0]["command"]
            self.assertIn(hooks_mod.MARKER, cmd)
            self.assertIn(self.port_file, cmd)   # port read from port file
        self.assertEqual(cfg["hooks"]["PreToolUse"][0]["matcher"],
                         "ExitPlanMode|AskUserQuestion")
        self.assertEqual(cfg["hooks"]["Notification"][0]["matcher"],
                         "permission_prompt")
        self.assertNotIn("matcher", cfg["hooks"]["Stop"][0])

    def test_install_is_idempotent(self):
        self.install()
        first = self.read()
        self.install()
        self.assertEqual(self.read(), first)     # byte-identical, no dupes

    def test_user_hooks_and_keys_survive_install_and_uninstall(self):
        self.write_fixture(self.USER_FIXTURE)
        original = self.read()
        self.install()
        cfg = json.loads(self.read())
        self.assertEqual(cfg["model"], "opus")
        self.assertEqual(cfg["permissions"], self.USER_FIXTURE["permissions"])
        # user entries intact, ours appended after them
        pre = cfg["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], self.USER_FIXTURE["hooks"]["PreToolUse"][0])
        self.assertEqual(len(pre), 2)
        self.assertEqual(cfg["hooks"]["Stop"][0],
                         self.USER_FIXTURE["hooks"]["Stop"][0])
        self.assertEqual(hooks_mod.uninstall(self.settings), 0)
        self.assertEqual(self.read(), original)  # byte-identical round trip

    def test_uninstall_removes_only_marker_entries(self):
        self.write_fixture(self.USER_FIXTURE)
        self.install()
        # an untagged entry whose command carries the marker is also ours
        cfg = json.loads(self.read())
        cfg["hooks"]["Stop"].append(
            {"hooks": [{"type": "command",
                        "command": f"legacy {hooks_mod.MARKER} cmd"}]})
        self.write_fixture(cfg)
        hooks_mod.uninstall(self.settings)
        cfg = json.loads(self.read())
        self.assertEqual(cfg["hooks"]["Stop"],
                         self.USER_FIXTURE["hooks"]["Stop"])
        self.assertNotIn("UserPromptSubmit", cfg["hooks"])

    def test_uninstall_without_install_is_a_noop(self):
        self.write_fixture(self.USER_FIXTURE)
        original = self.read()
        self.assertEqual(hooks_mod.uninstall(self.settings), 0)
        self.assertEqual(self.read(), original)
        self.assertEqual(hooks_mod.uninstall(
            os.path.join(self.tmp.name, "missing.json")), 0)

    def test_empty_file_round_trip(self):
        self.write_fixture({})
        self.install()
        hooks_mod.uninstall(self.settings)
        self.assertEqual(json.loads(self.read()), {})

    def test_backup_left_exactly_once(self):
        self.write_fixture(self.USER_FIXTURE)
        original = self.read()
        self.install()
        bak = self.settings + ".csb-bak"
        with open(bak, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), original)
        self.install()                           # again: backup untouched
        with open(bak, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), original)

    def test_unparseable_settings_refused(self):
        with open(self.settings, "w", encoding="utf-8") as f:
            f.write("{ not json")
        self.assertEqual(self.install(), 1)
        self.assertEqual(self.read(), "{ not json")

    def test_windows_refused_and_untouched(self):
        self.write_fixture(self.USER_FIXTURE)
        original = self.read()
        with mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(self.install(), 2)
        self.assertEqual(self.read(), original)

    def test_cli_main_status_and_flow(self):
        rc = hooks_mod.main(["install", "--settings", self.settings,
                             "--port-file", self.port_file])
        self.assertEqual(rc, 0)
        self.assertEqual(hooks_mod.installed_events(self.settings),
                         sorted(ALL_EVENTS))
        rc = hooks_mod.main(["uninstall", "--settings", self.settings])
        self.assertEqual(rc, 0)
        self.assertEqual(hooks_mod.installed_events(self.settings), [])

    def test_install_honors_configured_hook_port_file(self):
        # a bridge configured with hook_port_file (config.json or
        # CSB_HOOK_PORT_FILE) writes its port there — the installed hook
        # command must read the SAME path, or every hook curls an empty
        # port and the hook tier silently never activates
        custom = os.path.join(self.tmp.name, "custom-port-file")
        cfg_path = os.path.join(self.tmp.name, "config.json")
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump({"hook_port_file": custom}, f)
        with mock.patch.dict(os.environ, {"CSB_DATA_DIR": self.tmp.name}):
            rc = hooks_mod.main(["install", "--settings", self.settings])
        self.assertEqual(rc, 0)
        cmd = json.loads(self.read())["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn(f"'{custom}'", cmd)
        self.assertNotIn("/hook-port", cmd)      # not the ignored default

    def test_install_honors_env_hook_port_file(self):
        custom = os.path.join(self.tmp.name, "env-port-file")
        env = {"CSB_DATA_DIR": self.tmp.name, "CSB_HOOK_PORT_FILE": custom}
        with mock.patch.dict(os.environ, env):
            rc = hooks_mod.main(["install", "--settings", self.settings])
        self.assertEqual(rc, 0)
        cmd = json.loads(self.read())["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn(f"'{custom}'", cmd)
        # an explicit --port-file still beats the config
        with mock.patch.dict(os.environ, env):
            hooks_mod.main(["install", "--settings", self.settings,
                            "--port-file", self.port_file])
        cmd = json.loads(self.read())["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertIn(f"'{self.port_file}'", cmd)

    def test_status_reads_configured_hook_port_file(self):
        custom = os.path.join(self.tmp.name, "custom-port-file")
        with open(custom, "w", encoding="utf-8") as f:
            f.write("54321")
        self.write_fixture({})
        env = {"CSB_DATA_DIR": self.tmp.name, "CSB_HOOK_PORT_FILE": custom}
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env), redirect_stdout(buf):
            rc = hooks_mod.main(["status", "--settings", self.settings])
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("listener port 54321", out)
        self.assertIn(custom, out)


if __name__ == "__main__":
    unittest.main()
