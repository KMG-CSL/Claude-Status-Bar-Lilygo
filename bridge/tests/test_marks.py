"""Board round 2: user marks (seen / snooze / note, csb.marks) flowing
through is_unread to both consumers, the richer card facts (branch,
files, todos, exchanges, full pending text), peer chips carrying pids,
the /api/mark and /api/answer routes, and Approve/Deny keystrokes."""

import http.client
import json
import os
import queue
import tempfile
import time
import unittest
from unittest import mock

from csb import attention, focus
from csb.core import BridgeCore
from csb.engine import derive
from csb.hooks import HookListener
from csb.marks import Marks
from tests.helpers import (T0, assistant_text, assistant_tool_use, base_cfg,
                           hook_event, make_session, stub_usage, tool_result,
                           user, write_jsonl)
from tests.test_focus import TMUX_PANES, ctx, fake_runner


def human(ts, text="fix the widget", **extra):
    rec = user(ts, text)
    rec["origin"] = {"kind": "human"}
    rec.update(extra)
    return rec


def ev(name, ts, path="", **extra):
    e = hook_event(name, ts, transcript_path=path)
    e.update(extra)
    return e


# ------------------------------------------------------------------ store

class MarksStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "marks.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trip_and_undo(self):
        m = Marks(self.path)
        m.mark_seen("s1", T0 + 5, now=T0 + 9)
        m.snooze_until("s2", T0 + 1800, now=T0)
        m.set_note("s3", "  waiting on   review #demo ", now=T0)
        m2 = Marks(self.path)                       # a bridge restart
        self.assertEqual(m2.seen, {"s1": T0 + 5})
        self.assertEqual(m2.snooze, {"s2": T0 + 1800})
        self.assertEqual(m2.notes, {"s3": "waiting on review #demo"})
        m2.unsee("s1")
        m2.set_note("s3", "")
        self.assertEqual(Marks(self.path).seen, {})
        self.assertEqual(Marks(self.path).notes, {})

    def test_snooze_is_capped_and_expires_on_apply(self):
        m = Marks(self.path)
        m.snooze_until("s", T0 + 10 * 86400, now=T0)
        self.assertEqual(m.snooze["s"], T0 + 24 * 3600)

        class S:
            session_id = "s"
        s = S()
        m.apply(s, now=T0 + 60)
        self.assertEqual(s.snooze_until, T0 + 24 * 3600)
        m.apply(s, now=T0 + 25 * 3600)
        self.assertEqual(s.snooze_until, 0.0)

    def test_old_entries_are_pruned_on_save(self):
        m = Marks(self.path)
        m.mark_seen("old", T0, now=T0)
        m.mark_seen("new", T0, now=T0 + 40 * 86400)
        self.assertEqual(set(Marks(self.path).seen), {"new"})

    def test_unwritable_path_is_swallowed(self):
        m = Marks(os.path.join(self.tmp.name, "nope", "marks.json"))
        m.mark_seen("s", T0)                        # no raise
        self.assertEqual(m.seen, {"s": T0})         # in-memory still works


# ------------------------------------------------------------ card facts

class CardFactsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = base_cfg()

    def tearDown(self):
        self.tmp.cleanup()

    def sess(self, records, now=T0 + 100):
        return make_session(self.tmp.name, records, mtime=now)

    def test_branch_files_todos_exchanges(self):
        cwd = "/home/u/projects/widget"
        s = self.sess([
            human(T0, "turn one", gitBranch="main"),
            assistant_tool_use(T0 + 1, "Edit", "e1",
                               {"file_path": cwd + "/src/a.py"}),
            assistant_tool_use(T0 + 2, "Write", "e2",
                               {"file_path": "/elsewhere/b.md"}),
            assistant_tool_use(T0 + 2, "Edit", "e3",
                               {"file_path": cwd + "/src/a.py"}),   # dup
            assistant_tool_use(T0 + 3, "TodoWrite", "t1", {"todos": [
                {"content": "a", "status": "completed"},
                {"content": "b", "activeForm": "Doing b", "status": "in_progress"},
                {"content": "c", "status": "pending"}]}),
            assistant_text(T0 + 4, "first reply **bold**"),
            assistant_text(T0 + 5, "and the end of turn one"),
            human(T0 + 10, "turn two", gitBranch="feat/x"),
            assistant_text(T0 + 11, "reply two"),
        ])
        self.assertEqual(s.git_branch, "feat/x")
        self.assertEqual(s.turn_files, [])          # reset by turn two
        self.assertEqual(s.todos, [("a", "completed"), ("b", "in_progress"),
                                   ("c", "pending")])
        self.assertEqual(s.current_task, "Doing b")
        self.assertEqual([(w, t) for w, _ts, t in s.exchanges],
                         [("you", "turn one"),
                          ("claude", "and the end of turn one"),   # last text wins
                          ("you", "turn two"), ("claude", "reply two")])
        r = derive(s, self.cfg, T0 + 100)
        c = attention.card(s, r, {}, "review", self.cfg, T0 + 100)
        self.assertEqual(c["branch"], "feat/x")
        self.assertEqual(c["progress"], "1/3")
        self.assertEqual(c["todos"][1], {"c": "b", "s": "in_progress"})
        self.assertEqual(len(c["exchanges"]), 4)

    def test_files_this_turn_are_relative_to_cwd(self):
        cwd = "/home/u/projects/widget"
        s = self.sess([
            human(T0),
            assistant_tool_use(T0 + 1, "Edit", "e1",
                               {"file_path": cwd + "/src/a.py"}),
            assistant_tool_use(T0 + 2, "Write", "e2",
                               {"file_path": "/elsewhere/b.md"}),
        ])
        self.assertEqual(s.turn_files, ["src/a.py", "/elsewhere/b.md"])

    def test_pending_text_full_command_and_question_options(self):
        cmd = "git push origin feat/sso-on\n# second line"
        s = self.sess([human(T0),
                       assistant_tool_use(T0 + 1, "Bash", "b1", {"command": cmd})])
        r = derive(s, self.cfg, T0 + 40)             # silence -> needs approval
        self.assertEqual(r.st, "wait")
        c = attention.card(s, r, {}, "needs", self.cfg, T0 + 40)
        self.assertEqual((c["pending_tool"], c["pending"]), ("Bash", cmd))
        s2 = self.sess([human(T0), assistant_tool_use(
            T0 + 1, "AskUserQuestion", "q1", {"questions": [
                {"question": "Which   window?",
                 "options": [{"label": "Last 7 days"}, {"label": "All"}]}]})])
        r2 = derive(s2, self.cfg, T0 + 2)
        c2 = attention.card(s2, r2, {}, "needs", self.cfg, T0 + 2)
        self.assertEqual(c2["pending"], "Which window?")
        self.assertEqual(c2["options"], ["Last 7 days", "All"])
        self.assertEqual(attention.need_kind(s2, r2), "question")

    def test_pending_text_clears_with_the_result(self):
        s = self.sess([human(T0),
                       assistant_tool_use(T0 + 1, "Bash", "b1", {"command": "ls"}),
                       tool_result(T0 + 2, "b1")])
        self.assertEqual(s.pending_text, {})

    def test_peer_items_carry_pid_and_fall_back_to_board_title(self):
        s = self.sess([
            human(T0),
            assistant_tool_use(T0 + 1, "SendMessage", "m1",
                               {"to": "uds:/tmp/cc-socks/200.sock",
                                "summary": "please rescore"}),
        ])
        s.reg = {"pid": 100, "name": "me"}
        other = make_session(self.tmp.name, [human(T0, "I am the worker")],
                             mtime=T0 + 100, filename="worker.jsonl")
        other.reg = {"pid": 200}                      # live but unnamed
        other.custom_title = "worker-1 rescore"
        live = {"s-me": {"pid": 100, "name": "me"}, "s-w": {"pid": 200}}
        board = attention.build_board(
            [s, other], [derive(s, self.cfg, T0 + 5), derive(other, self.cfg, T0 + 5)],
            [{}, {}], self.cfg, now=T0 + 5, registry=live)
        cards = {c["sid"]: c for l in board["lanes"].values() for c in l}
        p = cards[s.session_id]["peers"][0]
        self.assertEqual((p["name"], p["pid"], p["gist"]),
                         ("worker-1 rescore", 200, "please rescore"))


# ---------------------------------------------------- seen / snooze / ur

class SeenFlowTest(unittest.TestCase):
    """End to end on a fallback-mode core: a finished session sits in
    Review with ur=1; a board Seen moves it to Done and flips ur; a newer
    reply re-arms it; Unseen undoes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.path = os.path.join(self.tmp.name, "sess-1.jsonl")
        write_jsonl(self.path, [human(self.now - 60, "do it"),
                                assistant_text(self.now - 50, "done!")])
        self.q = queue.Queue()
        self.cfg = base_cfg(roots=[self.tmp.name],
                            marks_file=os.path.join(self.tmp.name, "marks.json"))
        self.core = BridgeCore(self.cfg, usage=stub_usage(self.cfg),
                               hook_queue=self.q)

    def tearDown(self):
        self.tmp.cleanup()

    def lane_and_ur(self, now):
        pkt = self.core.step(now=now)
        doc = self.core.board_doc
        lanes = {c["sid"]: (k, c) for k, l in doc["lanes"].items() for c in l}
        k, c = lanes["sess-1"]
        return k, pkt["ses"][0]["ur"], c

    def test_seen_moves_to_done_flips_ur_and_survives_restart(self):
        k, ur, c = self.lane_and_ur(self.now)
        self.assertEqual((k, ur, c["acked"]), ("review", 1, False))
        self.q.put({"_cmd": "mark", "sid": "sess-1", "op": "seen",
                    "reply_ts": c["reply_ts"], "ts": self.now + 1})
        k, ur, c = self.lane_and_ur(self.now + 2)
        self.assertEqual((k, ur, c["acked"]), ("done", 0, True))
        # a second bridge (restart) reads the same answer from the file
        core2 = BridgeCore(self.cfg, usage=stub_usage(self.cfg))
        pkt = core2.step(now=self.now + 3)
        self.assertEqual(pkt["ses"][0]["ur"], 0)
        # a NEWER reply is news again
        with open(self.path, "a") as f:
            f.write(json.dumps(assistant_text(self.now + 5, "more")) + "\n")
        k, ur, c = self.lane_and_ur(self.now + 60)
        self.assertEqual((k, ur), ("review", 1))

    def test_unseen_undoes(self):
        self.core.step(now=self.now - 1)              # discover the session
        self.q.put({"_cmd": "mark", "sid": "sess-1", "op": "seen", "ts": self.now})
        self.assertEqual(self.lane_and_ur(self.now + 1)[0], "done")
        self.q.put({"_cmd": "mark", "sid": "sess-1", "op": "unseen",
                    "ts": self.now + 2})
        self.assertEqual(self.lane_and_ur(self.now + 3)[0], "review")

    def test_note_lands_on_the_card(self):
        self.core.step(now=self.now - 1)
        self.q.put({"_cmd": "mark", "sid": "sess-1", "op": "note",
                    "text": "blocked on upstream #demo-wed", "ts": self.now})
        _k, _ur, c = self.lane_and_ur(self.now + 1)
        self.assertEqual(c["note"], "blocked on upstream #demo-wed")

    def test_unknown_session_is_ignored(self):
        self.q.put({"_cmd": "mark", "sid": "nope", "op": "seen", "ts": self.now})
        self.core.step(now=self.now + 1)             # no raise
        self.assertEqual(self.core.marks.seen, {})


class SnoozeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = base_cfg()

    def tearDown(self):
        self.tmp.cleanup()

    def test_snoozed_needs_stays_in_lane_but_out_of_the_count_and_last(self):
        a = make_session(self.tmp.name, [
            human(T0), assistant_tool_use(T0 + 1, "Bash", "b1")],
            filename="a.jsonl")
        b = make_session(self.tmp.name, [
            human(T0 + 10), assistant_tool_use(T0 + 11, "Bash", "b2")],
            filename="b.jsonl")
        a.snooze_until = T0 + 3600                    # a waited longer, but snoozed
        now = T0 + 60
        board = attention.build_board(
            [a, b], [derive(a, self.cfg, now), derive(b, self.cfg, now)],
            [{"sl": 0}, {"sl": 1}], self.cfg, now=now)
        needs = board["lanes"]["needs"]
        self.assertEqual([c["sid"] for c in needs], ["b", "a"])
        self.assertEqual(needs[1]["snooze"], T0 + 3600)
        self.assertEqual((board["counts"]["needs"], board["counts"]["snoozed"]),
                         (1, 1))


# ------------------------------------------------------------- approve

class AnswerTest(unittest.TestCase):
    """Approve/Deny only reaches the terminal for a hook-evidenced
    permission dialog, checked after the step's poll."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.path = os.path.join(self.tmp.name, "sess-1.jsonl")
        write_jsonl(self.path, [
            human(self.now - 30, "push it"),
            assistant_tool_use(self.now - 29, "Bash", "b1",
                               {"command": "git push"})])
        self.q = queue.Queue()
        self.cfg = base_cfg(roots=[self.tmp.name],
                            marks_file=os.path.join(self.tmp.name, "marks.json"))
        self.core = BridgeCore(self.cfg, usage=stub_usage(self.cfg),
                               hook_queue=self.q)
        self.sent = []
        p = mock.patch.object(focus, "send_key_session",
                              lambda core, s, key: self.sent.append(key) or True)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def arm(self):
        self.q.put(ev("UserPromptSubmit", self.now - 30, self.path))
        self.q.put(ev("Notification", self.now - 25, self.path,
                      notification_type="permission_prompt"))

    def test_approve_and_deny_send_the_configured_keys(self):
        self.arm()
        self.core.step(now=self.now)
        c = self.core.board_doc["lanes"]["needs"][0]
        self.assertEqual((c["need"], c["src"], c["pending"]),
                         ("permission", "h", "git push"))
        self.q.put({"_cmd": "answer", "sid": "sess-1", "answer": "approve",
                    "ts": self.now})
        self.core.step(now=self.now + 1)
        self.q.put({"_cmd": "answer", "sid": "sess-1", "answer": "deny",
                    "ts": self.now + 1})
        self.core.step(now=self.now + 2)
        self.assertEqual(self.sent, ["Enter", "Escape"])

    def test_silence_guess_is_not_enough(self):
        # transcript tier alone: pending Bash + write silence -> wait, src t
        self.core.step(now=self.now)
        c = self.core.board_doc["lanes"]["needs"][0]
        self.assertEqual(c["src"], "t")
        self.q.put({"_cmd": "answer", "sid": "sess-1", "answer": "approve",
                    "ts": self.now})
        self.core.step(now=self.now + 1)
        self.assertEqual(self.sent, [])

    def test_a_question_is_never_answered_with_enter(self):
        write_jsonl(self.path, [
            human(self.now - 30, "which?"),
            assistant_tool_use(self.now - 29, "AskUserQuestion", "q1",
                               {"questions": [{"question": "A or B?"}]})])
        self.arm()
        self.core.step(now=self.now)
        self.assertEqual(self.core.board_doc["lanes"]["needs"][0]["need"],
                         "question")
        self.q.put({"_cmd": "answer", "sid": "sess-1", "answer": "approve",
                    "ts": self.now})
        self.core.step(now=self.now + 1)
        self.assertEqual(self.sent, [])


class SendKeyTest(unittest.TestCase):
    def test_tmux_sends_to_the_owning_pane(self):
        run = fake_runner({"tmux": TMUX_PANES})
        ok, _ = focus.send_tmux(ctx(tty="/dev/pts/7", env={"TMUX": "x"}),
                                "Enter", run)
        self.assertTrue(ok)
        self.assertIn(["tmux", "send-keys", "-t", "side:0.1", "Enter"], run.calls)

    def test_tmux_off_tmux_is_an_honest_no(self):
        run = fake_runner({"tmux": TMUX_PANES})
        ok, detail = focus.send_tmux(ctx(tty="/dev/pts/7"), "Enter", run)
        self.assertFalse(ok)
        self.assertNotIn("send-keys", " ".join(" ".join(c) for c in run.calls))

    def test_iterm2_types_raw_bytes_for_named_keys(self):
        run = fake_runner({"osascript": "ok"})
        ok, _ = focus.send_iterm2(ctx(), "Escape", run)
        self.assertTrue(ok)
        self.assertEqual(run.calls[0][-1], "\x1b")
        focus.send_iterm2(ctx(), "Enter", run)
        self.assertEqual(run.calls[1][-1], "\r")
        self.assertIn("newline NO", run.calls[0][2])


# ----------------------------------------------------------------- web

class WebRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = base_cfg(hook_port=0, hook_port_file=os.path.join(
            self.tmp.name, "hook-port"))
        self.l = HookListener(cfg)
        self.port = self.l.port

    def tearDown(self):
        self.l.close()
        self.tmp.cleanup()

    def post(self, path, body, origin=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}",
             "Content-Type": "application/json"}
        if origin:
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        c.request("POST", path, body=json.dumps(body).encode(), headers=h)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def test_mark_validates_and_enqueues(self):
        self.assertEqual(self.post("/api/mark", {"sid": "x", "op": "nuke"}), 400)
        self.assertEqual(self.post("/api/mark", {"op": "seen"}), 400)
        self.assertEqual(self.post("/api/mark", {"sid": "x", "op": "snooze",
                                                 "minutes": 100000}), 400)
        self.assertEqual(self.post("/api/mark", {"sid": "x", "op": "seen",
                                                 "reply_ts": 12.5}), 202)
        cmd = self.l.queue.get(timeout=2)
        self.assertEqual((cmd["_cmd"], cmd["op"], cmd["reply_ts"]),
                         ("mark", "seen", 12.5))
        self.assertEqual(self.post("/api/mark", {"sid": "x", "op": "note",
                                                 "text": "n" * 500}), 202)
        self.assertEqual(len(self.l.queue.get(timeout=2)["text"]), 200)

    def test_answer_validates_and_enqueues(self):
        self.assertEqual(self.post("/api/answer", {"sid": "x", "answer": "yes"}), 400)
        self.assertEqual(self.post("/api/answer", {"sid": "x", "answer": "deny"}), 202)
        self.assertEqual(self.l.queue.get(timeout=2)["answer"], "deny")

    def test_same_origin_rules_apply_to_new_routes(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/answer", body=b'{"sid":"x","answer":"approve"}',
                  headers={"Host": f"127.0.0.1:{self.port}",
                           "Content-Type": "application/json",
                           "Origin": "https://evil.example"})
        self.assertEqual(c.getresponse().status, 403)
        c.close()


if __name__ == "__main__":
    unittest.main()
