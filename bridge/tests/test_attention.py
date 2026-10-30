"""Attention board: new hook events, card facts parsed from transcripts,
unread/lanes/stingy chime (csb.attention), the live session registry
(csb.registry + BridgeCore registry mode), and the web routes (csb.web)."""

import http.client
import json
import os
import tempfile
import time
import unittest

from csb import attention
from csb.core import BridgeCore
from csb.engine import derive
from csb.hooks import HookListener
from csb.registry import Registry, transcript_path
from tests.helpers import (T0, assistant_text, assistant_tool_use, base_cfg,
                           hook_event, iso, make_session, stub_usage,
                           tool_result, user)


def peer(ts, name="WEEKEND-FABLE", body="three tasks for you"):
    return {"type": "user", "timestamp": iso(ts), "isMeta": True,
            "origin": {"kind": "peer", "name": name, "body": body},
            "message": {"role": "user", "content":
                        "Another Claude session sent a message:\n"
                        f"<cross-session-message from-name=\"{name}\">"}}


def human(ts, text="fix the widget"):
    rec = user(ts, text)
    rec["origin"] = {"kind": "human"}
    return rec


def ev(name, ts, **extra):
    e = hook_event(name, ts)
    e.update(extra)
    return e


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = base_cfg()

    def tearDown(self):
        self.tmp.cleanup()

    def sess(self, records, now=T0 + 100):
        return make_session(self.tmp.name, records, mtime=now)


# ------------------------------------------------------------ card facts

class CardFactsTest(Base):
    def test_human_prompt_is_seen_peer_message_is_not(self):
        s = self.sess([human(T0, "please refactor"),
                       assistant_text(T0 + 5, "done"),
                       peer(T0 + 10)])
        self.assertEqual(s.last_prompt, "please refactor")
        self.assertEqual(s.last_prompt_ts, T0)        # peer didn't move it
        self.assertEqual(s.peers[-1][:2], ("in", "WEEKEND-FABLE"))
        self.assertEqual(s.turn_start, T0 + 10)       # but it is a turn

    def test_legacy_records_without_origin_count_as_human(self):
        s = self.sess([user(T0, "old style")])
        self.assertEqual(s.last_prompt_ts, T0)

    def test_send_message_todo_and_agent_name(self):
        s = self.sess([
            human(T0),
            assistant_tool_use(T0 + 1, "SendMessage", "t1",
                               {"to": "worker", "summary": "go"}),
            assistant_tool_use(T0 + 2, "TodoWrite", "t2", {"todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "activeForm": "Writing tests",
                 "status": "in_progress"}]}),
            {"type": "agent-name", "agentName": "orchestrator"},
        ])
        self.assertEqual(s.peers[-1][:2], ("out", "worker"))
        self.assertEqual(s.current_task, "Writing tests")
        self.assertEqual(s.title(), "orchestrator")

    def test_registry_user_name_wins_and_follows_renames(self):
        s = self.sess([human(T0), {"type": "agent-name", "agentName": "old"}])
        s.reg = {"name": "renamed", "nameSource": "user"}
        self.assertEqual(s.title(), "renamed")
        s.reg = {"name": "auto-thing", "nameSource": "auto"}
        self.assertEqual(s.title(), "old")


# ------------------------------------------------------------ hook events

class HookEventsTest(Base):
    def test_stop_failure_ends_the_turn_as_error_not_working(self):
        s = self.sess([human(T0)])
        s.apply_hook(ev("UserPromptSubmit", T0))
        s.apply_hook(ev("StopFailure", T0 + 5, error_type="server_error"))
        r = derive(s, self.cfg, T0 + 6)
        self.assertEqual(r.st, "wait")
        self.assertTrue(r.err)
        self.assertEqual(attention.lane_of(s, r, self.cfg, T0 + 6), "needs")
        self.assertEqual(attention.need_kind(s, r), "error")

    def test_notification_types(self):
        s = self.sess([human(T0)])
        s.apply_hook(ev("UserPromptSubmit", T0))
        s.apply_hook(ev("Notification", T0 + 1, notification_type="idle_prompt"))
        self.assertIsNone(s.perm_prompt_at)
        s.apply_hook(ev("Notification", T0 + 2,
                        notification_type="elicitation_dialog"))
        self.assertEqual(s.perm_prompt_at, T0 + 2)
        s.apply_hook(ev("Notification", T0 + 3,
                        notification_type="elicitation_response"))
        self.assertIsNone(s.perm_prompt_at)
        s.apply_hook(ev("Notification", T0 + 4,
                        notification_type="permission_prompt"))
        self.assertEqual(derive(s, self.cfg, T0 + 5).st, "wait")

    def test_subagent_start_stop_tracked_by_id(self):
        s = self.sess([human(T0)])
        s.apply_hook(ev("SubagentStart", T0, agent_id="a1", agent_type="Explore"))
        s.apply_hook(ev("SubagentStart", T0, agent_id="a2", agent_type="Plan"))
        self.assertEqual(s.subagent_count(self.cfg, T0 + 1), 2)
        s.apply_hook(ev("SubagentStop", T0 + 2, agent_id="a1"))
        self.assertEqual(s.subagent_count(self.cfg, T0 + 3), 1)

    def test_stop_carries_last_reply(self):
        s = self.sess([human(T0)])
        s.apply_hook(ev("Stop", T0 + 5, last_assistant_message="All green."))
        self.assertEqual(s.last_assistant_text, "All green.")
        self.assertEqual(s.last_reply_ts, T0 + 5)


# ------------------------------------------------------- unread and lanes

class UnreadLanesTest(Base):
    def done_session(self, records):
        s = self.sess(records)
        s.apply_hook(ev("UserPromptSubmit", T0))
        s.apply_hook(ev("Stop", T0 + 5))
        return s, derive(s, self.cfg, T0 + 6)

    def test_finished_after_last_prompt_is_unread_review(self):
        s, r = self.done_session([human(T0), assistant_text(T0 + 4, "ok")])
        self.assertTrue(attention.is_unread(s, r, self.cfg, T0 + 6))
        self.assertEqual(attention.lane_of(s, r, self.cfg, T0 + 6), "review")

    def test_cancel_is_seen(self):
        s = self.sess([human(T0), assistant_tool_use(T0 + 1)])
        s.apply_hook(ev("UserPromptSubmit", T0))
        s.apply_hook(ev("Stop", T0 + 5))          # unmatched tool_use: cancel
        r = derive(s, self.cfg, T0 + 6)
        self.assertEqual(r.fin, "cancel")
        self.assertFalse(attention.is_unread(s, r, self.cfg, T0 + 6))

    def test_peer_triggered_turn_finishing_is_unread(self):
        s, r = self.done_session([human(T0 - 50), assistant_text(T0 - 40, "x"),
                                  peer(T0), assistant_text(T0 + 4, "did it")])
        self.assertTrue(attention.is_unread(s, r, self.cfg, T0 + 6))

    def test_delegating_parent_is_running_and_chimes_only_when_settled(self):
        s = self.sess([human(T0), assistant_tool_use(T0 + 1, "Task", "tk")])
        s.apply_hook(ev("UserPromptSubmit", T0))
        s.apply_hook(ev("Stop", T0 + 5))
        r = derive(s, self.cfg, T0 + 6)
        self.assertEqual(r.st, "done")
        self.assertEqual(attention.lane_of(s, r, self.cfg, T0 + 6), "running")
        self.assertEqual(attention.chime_state(s, r, self.cfg, T0 + 6), "tool")
        s.task_ids.clear()                        # last worker reported back
        self.assertEqual(attention.chime_state(s, r, self.cfg, T0 + 7), "done")

    def test_quiet_for_hours_parks_and_keeps_unread(self):
        s, r = self.done_session([human(T0), assistant_text(T0 + 4, "ok")])
        later = T0 + self.cfg["parked_after_s"] + 60
        self.assertEqual(attention.lane_of(s, r, self.cfg, later), "parked")
        self.assertTrue(attention.is_unread(s, r, self.cfg, later))

    def test_rate_limit_lane(self):
        s = self.sess([human(T0)])
        s.limit_reset = T0 + 3600
        r = derive(s, self.cfg, T0 + 1)
        self.assertEqual(attention.lane_of(s, r, self.cfg, T0 + 1), "limited")

    def test_board_sorts_needs_longest_waiting_first(self):
        mk = lambda n, el: {"lane": "needs", "el": el, "sid": n}
        lanes = sorted([mk("a", 5), mk("b", 90)], key=attention._SORT["needs"])
        self.assertEqual([c["sid"] for c in lanes], ["b", "a"])


# --------------------------------------------------------------- registry

class RegistryTest(Base):
    def entry(self, pid, sid, **kw):
        e = {"pid": pid, "sessionId": sid, "cwd": "/home/u/proj",
             "procStart": "Fri Oct 30 20:47:46 2026", "status": "idle",
             "name": "worker", "nameSource": "user", "updatedAt": 1}
        e.update(kw)
        d = os.path.join(self.tmp.name, "sessions")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{pid}.json"), "w") as f:
            json.dump(e, f)
        return d

    def test_missing_dir_means_no_registry(self):
        r = Registry({"registry_dir": os.path.join(self.tmp.name, "nope")})
        self.assertIsNone(r.read())

    def test_dead_and_reused_pids_are_dropped(self):
        d = self.entry(11, "s-live")
        self.entry(12, "s-dead")
        self.entry(13, "s-reused")
        reg = Registry({"registry_dir": d},
                       alive=lambda pid: pid != 12,
                       lstart=lambda pid: ("Sat Oct 31 01:00:00 2026"
                                           if pid == 13 else
                                           "Fri Oct 30 20:47:46 2026"))
        self.assertEqual(sorted(reg.read()), ["s-live"])

    def test_transcript_path_sanitizes_cwd(self):
        p = transcript_path({"sessionId": "abc", "cwd": "/Users/x/my.repo"},
                            [self.tmp.name])
        self.assertEqual(p, os.path.join(self.tmp.name, "-Users-x-my-repo",
                                         "abc.jsonl"))

    def test_core_registry_mode_shows_quiet_sessions_and_drops_ended(self):
        d = self.entry(21, "sid-a", cwd="/home/u/widget")
        root = os.path.join(self.tmp.name, "projects")
        tp = transcript_path({"sessionId": "sid-a", "cwd": "/home/u/widget"},
                             [root])
        os.makedirs(os.path.dirname(tp))
        with open(tp, "w") as f:
            f.write(json.dumps(human(T0)) + "\n")
            f.write(json.dumps(assistant_text(T0 + 2, "ok")) + "\n")
        cfg = base_cfg(roots=[root], registry_dir=d)
        alive = {21}
        reg = Registry(cfg, alive=lambda p: p in alive, lstart=lambda p: "")
        core = BridgeCore(cfg, usage=stub_usage(cfg), registry=reg)
        boards = []
        core._publish_board = lambda *a, now: boards.append(a)
        # days quiet: the 30-min active window would hide it; registry
        # mode shows it because the process is alive
        pkt = core.step(now=T0 + 3 * 86400)
        self.assertTrue(core.registry_mode)
        self.assertEqual([e["nm"] for e in pkt["ses"]], ["worker"])
        self.assertEqual(core.sessions[tp].claude_pid, 21)
        alive.clear()                                 # process exited
        pkt = core.step(now=T0 + 3 * 86400 + 1)
        self.assertEqual(pkt["ses"], [])


# -------------------------------------------------------------------- web

class WebTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = base_cfg(hook_port=0, hook_port_file=os.path.join(
            self.tmp.name, "hook-port"))
        self.l = HookListener(cfg)
        self.port = self.l.port

    def tearDown(self):
        self.l.close()
        self.tmp.cleanup()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_page_and_snapshot(self):
        st, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"EventSource", body)
        self.l.feed.publish({"ts": 1, "lanes": {}})
        st, body = self.req("GET", "/api/board")
        self.assertEqual(json.loads(body), {"ts": 1, "lanes": {}})

    def test_foreign_host_is_refused(self):
        st, _ = self.req("GET", "/api/board",
                         headers={"Host": f"evil.example:{self.port}"})
        self.assertEqual(st, 403)

    def test_sse_delivers_published_board(self):
        self.l.feed.publish({"ts": 2})
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", "/api/events",
                  headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        self.assertEqual(r.getheader("Content-Type"), "text/event-stream")
        first = r.fp.readline() + r.fp.readline()
        self.assertEqual(first, b'event: board\ndata: {"ts":2}\n')
        c.close()

    def test_focus_requires_json_and_same_origin_then_enqueues(self):
        st, _ = self.req("POST", "/api/focus", b'{"sid":"x"}',
                         {"Content-Type": "text/plain"})
        self.assertEqual(st, 403)
        st, _ = self.req("POST", "/api/focus", b'{"sid":"x"}',
                         {"Content-Type": "application/json",
                          "Origin": "https://evil.example"})
        self.assertEqual(st, 403)
        st, _ = self.req("POST", "/api/focus", b'{"sid":"x"}',
                         {"Content-Type": "application/json",
                          "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(st, 202)
        cmd = self.l.queue.get(timeout=2)
        self.assertEqual((cmd["_cmd"], cmd["sid"]), ("focus", "x"))

    def test_hook_posts_still_work(self):
        st, _ = self.req("POST", "/hook", json.dumps(
            {"hook_event_name": "Stop", "session_id": "s"}).encode())
        self.assertEqual(st, 204)
        self.assertEqual(self.l.queue.get(timeout=2)["hook_event_name"], "Stop")


if __name__ == "__main__":
    unittest.main()
