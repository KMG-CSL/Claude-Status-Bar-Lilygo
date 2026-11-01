"""Board round 2: user marks (seen / snooze / note, csb.marks) flowing
through is_unread to both consumers, the richer card facts (branch,
files, todos, exchanges, full pending text), peer chips carrying pids,
the /api/mark and /api/answer routes, and Approve/Deny keystrokes."""

import http.client
import json
import os
import queue
import subprocess
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

    def test_seen_is_monotonic_until_an_explicit_unseen(self):
        m = Marks(self.path)
        m.mark_seen("s", T0 + 50, now=T0 + 60)      # newer reply acked first
        m.mark_seen("s", T0 + 10, now=T0 + 61)      # a late Seen for an older one
        self.assertEqual(m.seen["s"], T0 + 50)
        m.mark_seen("s", float("inf"), now=T0 + 62)
        self.assertEqual(m.seen["s"], T0 + 50)
        m.unsee("s")
        m.mark_seen("s", T0 + 10, now=T0 + 63)
        self.assertEqual(m.seen["s"], T0 + 10)

    def test_non_finite_stamps_in_the_file_are_dropped(self):
        with open(self.path, "w") as f:
            f.write('{"seen": {"a": Infinity, "b": 5.0}, "snooze": {"c": NaN},'
                    ' "notes": {}, "touched": {}}')
        m = Marks(self.path)
        self.assertEqual((m.seen, m.snooze), ({"b": 5.0}, {}))

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

    def test_a_future_reply_ts_is_clamped_to_the_newest_reply(self):
        # a client stamp past the newest reply would pre-ack replies that
        # have not happened: the bridge acks the newest it knows instead
        k, ur, c = self.lane_and_ur(self.now)
        self.q.put({"_cmd": "mark", "sid": "sess-1", "op": "seen",
                    "reply_ts": c["reply_ts"] + 10 * 86400, "ts": self.now + 1})
        k, ur, c = self.lane_and_ur(self.now + 2)
        self.assertEqual((k, c["seen_ts"]), ("done", c["reply_ts"]))
        # ...so a reply that lands later is unread again
        from tests.helpers import append_jsonl
        append_jsonl(self.path, [assistant_text(self.now + 3, "more!")])
        k, ur, c = self.lane_and_ur(self.now + 60)    # settled: 57 s quiet
        self.assertEqual((k, ur), ("review", 1))

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
    """Approve/Deny reaches the terminal only through attention.answerable
    (the card's can_answer and the send share it), bound to the token of
    the dialog on screen, once per dialog, with the outcome on the card."""

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
        # the terminal side is StrictBindingTest's business: here the
        # adapter can act, the process binding holds, and sends succeed
        for target, fn, stub in (
                (focus, "send_key_session",
                 lambda core, s, key, **kw: (self.sent.append(key) or True, "")),
                (BridgeCore, "_can_jump", lambda self_, s, now=None: True),
                (attention, "_bound", lambda s: True)):
            p = mock.patch.object(target, fn, stub)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def arm(self, ntype="permission_prompt", at=None):
        at = self.now - 25 if at is None else at
        self.q.put(ev("UserPromptSubmit", at - 5, self.path))
        self.q.put(ev("Notification", at, self.path, notification_type=ntype))

    def needs(self):
        lane = self.core.board_doc["lanes"]["needs"]
        return lane[0] if lane else None

    def answer(self, what, token=None, at=None):
        c = self.needs()
        self.q.put({"_cmd": "answer", "sid": "sess-1", "answer": what,
                    "token": c["answer_token"] if token is None else token,
                    "ts": at or self.now})

    def test_approve_sends_once_then_the_dialog_is_spent(self):
        self.arm()
        self.core.step(now=self.now)
        c = self.needs()
        self.assertEqual((c["need"], c["src"], c["pending"], c["can_answer"]),
                         ("permission", "h", "git push", True))
        self.assertTrue(c["answer_token"].startswith("sess-1:"))
        self.assertTrue(c["answer_token"].endswith(":b1"))
        self.answer("approve")
        self.core.step(now=self.now + 1)
        self.assertEqual(self.sent, ["Enter"])
        c = self.needs()
        self.assertEqual((c["answer_result"]["ok"], c["answer_result"]["answer"],
                          c["answer_result"]["token"]),
                         (True, "approve", c["answer_token"]))
        # the long-running-approved-tool case: no PostToolUse yet, the latch
        # is still armed, the card still shows the dialog — but it is
        # spent: no Approve offered, and a second click types nothing
        self.assertFalse(c["can_answer"])
        self.answer("deny")
        self.core.step(now=self.now + 2)
        self.assertEqual(self.sent, ["Enter"])
        self.assertIn("already answered", self.needs()["answer_result"]["reason"])

    def test_without_an_exact_binding_approve_is_not_even_offered(self):
        # jump may be a cwd guess; a keystroke needs the registry binding,
        # so the card must not offer what the bridge would refuse
        attention._bound = lambda s: False
        self.arm()
        self.core.step(now=self.now)
        c = self.needs()
        self.assertEqual((c["jump"], c["can_answer"]), (True, False))

    def test_deny_sends_the_configured_key(self):
        self.cfg["focus"]["deny_key"] = "C-c"
        self.arm()
        self.core.step(now=self.now)
        self.answer("deny")
        self.core.step(now=self.now + 1)
        self.assertEqual(self.sent, ["C-c"])

    def test_stale_token_is_refused(self):
        self.arm()
        self.core.step(now=self.now)
        self.answer("approve", token="sess-1:1:old")
        self.core.step(now=self.now + 1)
        self.assertEqual(self.sent, [])
        r = self.needs()["answer_result"]
        self.assertFalse(r["ok"])
        self.assertIn("dialog changed", r["reason"])
        # a fresh click with the live token still works: nothing was spent
        self.answer("approve")
        self.core.step(now=self.now + 2)
        self.assertEqual(self.sent, ["Enter"])

    def test_a_new_dialog_is_a_new_token(self):
        self.arm()
        self.core.step(now=self.now)
        t1 = self.needs()["answer_token"]
        # the dialog was answered in the terminal and a new one came up
        self.q.put(ev("PostToolUse", self.now + 1, self.path))
        self.arm(at=self.now + 2)
        self.core.step(now=self.now + 3)
        t2 = self.needs()["answer_token"]
        self.assertNotEqual(t1, t2)
        self.answer("approve", token=t1)         # click aimed at the old one
        self.core.step(now=self.now + 4)
        self.assertEqual(self.sent, [])

    def test_elicitation_and_agent_input_are_never_answerable(self):
        for ntype in ("elicitation_dialog", "elicitation_url_dialog",
                      "agent_needs_input"):
            with self.subTest(ntype=ntype):
                self.sent.clear()
                self.arm(ntype=ntype)
                self.core.step(now=self.now)
                c = self.needs()
                self.assertEqual(c["lane"], "needs")      # still a wait
                self.assertFalse(c["can_answer"])
                self.answer("approve")
                self.core.step(now=self.now + 1)
                self.assertEqual(self.sent, [])
                self.assertIn("not permission", c and self.needs()["answer_result"]["reason"])

    def test_silence_guess_is_not_enough(self):
        # transcript tier alone: pending Bash + write silence -> wait, src t
        self.core.step(now=self.now)
        c = self.needs()
        self.assertEqual((c["src"], c["can_answer"]), ("t", False))
        self.answer("approve", token="sess-1:0:b1")
        self.core.step(now=self.now + 1)
        self.assertEqual(self.sent, [])

    def test_stale_hook_latch_with_a_silence_wait_is_refused(self):
        # hooks went quiet (> hook_fresh_s): the latch is still set on the
        # Session but src falls back to the transcript's guess
        self.arm(at=self.now - 2000)
        self.core.step(now=self.now)
        c = self.needs()
        self.assertEqual(c["src"], "t")
        self.assertFalse(c["can_answer"])
        self.answer("approve")
        self.core.step(now=self.now + 1)
        self.assertEqual(self.sent, [])
        self.assertIn("not hook-evidenced", self.needs()["answer_result"]["reason"])

    def test_truncated_pending_text_is_not_approvable(self):
        write_jsonl(self.path, [
            human(self.now - 30, "run it"),
            assistant_tool_use(self.now - 29, "Bash", "b1",
                               {"command": "echo ok; " * 400 + "rm -rf /"})])
        self.arm()
        self.core.step(now=self.now)
        c = self.needs()
        self.assertTrue(c["pending_truncated"])
        self.assertNotIn("rm -rf", c["pending"])
        self.assertFalse(c["can_answer"])
        self.answer("approve")
        self.core.step(now=self.now + 1)
        self.assertEqual(self.sent, [])
        self.assertIn("truncated", self.needs()["answer_result"]["reason"])

    def test_a_question_is_never_answered_with_enter(self):
        write_jsonl(self.path, [
            human(self.now - 30, "which?"),
            assistant_tool_use(self.now - 29, "AskUserQuestion", "q1",
                               {"questions": [{"question": "A or B?"}]})])
        self.arm()
        self.core.step(now=self.now)
        c = self.needs()
        self.assertEqual((c["need"], c["can_answer"]), ("question", False))
        self.answer("approve")
        self.core.step(now=self.now + 1)
        self.assertEqual(self.sent, [])

    def test_a_failed_send_is_reported_not_raised(self):
        focus.send_key_session = lambda core, s, key, **kw: (False, "no pane")
        self.arm()
        self.core.step(now=self.now)
        self.answer("approve")
        self.core.step(now=self.now + 1)           # must not raise
        r = self.needs()["answer_result"]
        self.assertEqual((r["ok"], r["reason"]), (False, "no pane"))


class StrictBindingTest(unittest.TestCase):
    """A keystroke needs the exact process: registry entry for this
    session id, pid alive and not reused, a tty. Never a cwd guess."""

    def core(self, adapter="tmux"):
        return type("Core", (), {"cfg": {"focus": {"adapter": adapter}}})()

    def ses(self, reg, claude_pid=0, sid="sid-1"):
        return type("Ses", (), {"session_id": sid, "cwd": "/home/u/p",
                                "project": "p", "claude_pid": claude_pid,
                                "reg": reg})()

    def runner(self):
        return fake_runner({
            "ps": lambda c: ("ttys012\n" if "tty=" in c
                             else ("  201 Mon Jul 21 09:00:00 2026 claude "
                                   "/usr/local/bin/claude\n"), 0),
            "lsof": "n/home/u/p\n",
            "tmux": lambda c: ((TMUX_PANES if "list-panes" in c else ""), 0)})

    def test_live_registry_pid_sends_to_its_own_tmux_server(self):
        run = self.runner()
        me = os.getpid()
        reg = {"pid": me, "sessionId": "sid-1", "procStart": "X"}
        with mock.patch.object(focus, "env_of",
                               lambda pid, runner=None: {"TMUX": "/tmp/s,1,0"}), \
                mock.patch.object(focus, "tty_of",
                                  lambda pid, runner=None: "/dev/pts/7"):
            ok, detail = focus.send_key_session(
                self.core(), self.ses(reg), "Enter", runner=run,
                proc_start=lambda pid: "X")
        self.assertTrue(ok, detail)
        self.assertIn(["tmux", "-S", "/tmp/s", "send-keys", "-t", "%5", "Enter"],
                      run.calls)

    def assert_refused(self, ses, proc_start=lambda pid: "X", why=""):
        run = self.runner()
        ok, detail = focus.send_key_session(self.core(), ses, "Enter",
                                            runner=run, proc_start=proc_start)
        self.assertFalse(ok)
        self.assertIn(why, detail)
        # never fell back to Jump's process hunt
        self.assertFalse(any(c[0] == "lsof" or (c[0] == "ps" and "-axo" in c)
                             for c in run.calls), run.calls)
        self.assertFalse(any("send-keys" in c for c in run.calls))
        return detail

    def test_no_registry_entry_is_refused_even_with_a_hook_pid(self):
        self.assert_refused(self.ses(None, claude_pid=os.getpid()),
                            why="no live registry entry")

    def test_dead_pid_is_refused(self):
        p = subprocess.Popen(["true"])
        p.wait()
        self.assert_refused(self.ses({"pid": p.pid, "sessionId": "sid-1",
                                      "procStart": ""}), why="gone")

    def test_reused_pid_is_refused(self):
        reg = {"pid": os.getpid(), "sessionId": "sid-1", "procStart": "X"}
        self.assert_refused(self.ses(reg), proc_start=lambda pid: "Y",
                            why="reused")

    def test_unreadable_start_time_is_refused(self):
        reg = {"pid": os.getpid(), "sessionId": "sid-1", "procStart": "X"}
        self.assert_refused(self.ses(reg), proc_start=lambda pid: "",
                            why="unreadable")

    def test_pid_moved_to_another_session_is_refused(self):
        # /clear: same process, new session id in the registry
        reg = {"pid": os.getpid(), "sessionId": "sid-NEW", "procStart": "X"}
        self.assert_refused(self.ses(reg), why="another session")

    def test_hook_ppid_disagreeing_with_the_registry_is_refused(self):
        reg = {"pid": os.getpid(), "sessionId": "sid-1", "procStart": "X"}
        self.assert_refused(self.ses(reg, claude_pid=os.getpid() + 1),
                            why="!= registry pid")

    def test_adapter_exception_is_contained(self):
        def boom(cmd, timeout=5):
            raise subprocess.TimeoutExpired(cmd, timeout)
        reg = {"pid": os.getpid(), "sessionId": "sid-1", "procStart": ""}
        ok, detail = focus.send_key_session(self.core(), self.ses(reg), "Enter",
                                            runner=boom,
                                            proc_start=lambda pid: "")
        self.assertFalse(ok)
        self.assertIn("TimeoutExpired", detail)

    def test_run_itself_never_raises(self):
        r = focus._run(["/nonexistent/binary/for/csb", "x"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("FileNotFoundError", r.stderr)


class SendKeyTest(unittest.TestCase):
    def test_tmux_sends_to_the_owning_pane_on_its_own_server(self):
        run = fake_runner({"tmux": TMUX_PANES})
        ok, _ = focus.send_tmux(ctx(tty="/dev/pts/7", env={"TMUX": "/s.sock,9,0"}),
                                "Enter", run)
        self.assertTrue(ok)
        self.assertIn(["tmux", "-S", "/s.sock", "send-keys", "-t", "%5", "Enter"],
                      run.calls)

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

    def post(self, path, body, origin=True, raw=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}",
             "Content-Type": "application/json"}
        if origin:
            h["Origin"] = f"http://127.0.0.1:{self.port}"
        c.request("POST", path, body=raw if raw is not None
                  else json.dumps(body).encode(), headers=h)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}"})
        r = c.getresponse()
        r.read()
        c.close()
        return r

    def test_mark_validates_and_enqueues(self):
        self.assertEqual(self.post("/api/mark", {"sid": "x", "op": "nuke"}), 400)
        self.assertEqual(self.post("/api/mark", {"op": "seen"}), 400)
        self.assertEqual(self.post("/api/mark", {"sid": "x", "op": "snooze",
                                                 "minutes": 100000}), 400)
        self.assertEqual(self.post("/api/mark", {"sid": "x", "op": "seen",
                                                 "reply_ts": T0 + 12.5}), 202)
        cmd = self.l.queue.get(timeout=2)
        self.assertEqual((cmd["_cmd"], cmd["op"], cmd["reply_ts"]),
                         ("mark", "seen", T0 + 12.5))
        self.assertEqual(self.post("/api/mark", {"sid": "x", "op": "note",
                                                 "text": "n" * 500}), 202)
        self.assertEqual(len(self.l.queue.get(timeout=2)["text"]), 200)

    def test_numbers_must_be_finite_plausible_and_not_bool(self):
        bad = [b'{"sid":"x","op":"seen","reply_ts":1e309}',      # -> inf
               b'{"sid":"x","op":"seen","reply_ts":true}',
               b'{"sid":"x","op":"seen","reply_ts":12.5}',       # not an epoch
               b'{"sid":"x","op":"seen","reply_ts":"1750000000"}',
               b'{"sid":"x","op":"snooze","minutes":true}',
               b'{"sid":"x","op":"snooze","minutes":1e309}',
               b'{"sid":"x","op":"note","text":["a"]}']
        for raw in bad:
            with self.subTest(raw=raw):
                self.assertEqual(self.post("/api/mark", None, raw=raw), 400)
        self.assertTrue(self.l.queue.empty())

    def test_answer_requires_a_token(self):
        self.assertEqual(self.post("/api/answer", {"sid": "x", "answer": "yes",
                                                   "token": "t"}), 400)
        self.assertEqual(self.post("/api/answer", {"sid": "x", "answer": "deny"}), 400)
        self.assertEqual(self.post("/api/answer", {"sid": "x", "answer": "deny",
                                                   "token": ""}), 400)
        self.assertEqual(self.post("/api/answer", {"sid": "x", "answer": "deny",
                                                   "token": "x" * 500}), 400)
        self.assertEqual(self.post("/api/answer", {"sid": "x", "answer": "deny",
                                                   "token": "x:1:b1"}), 202)
        cmd = self.l.queue.get(timeout=2)
        self.assertEqual((cmd["answer"], cmd["token"]), ("deny", "x:1:b1"))

    def test_same_origin_rules_apply_to_new_routes(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/api/answer",
                  body=b'{"sid":"x","answer":"approve","token":"t"}',
                  headers={"Host": f"127.0.0.1:{self.port}",
                           "Content-Type": "application/json",
                           "Origin": "https://evil.example"})
        self.assertEqual(c.getresponse().status, 403)
        c.close()

    def test_board_page_cannot_be_framed(self):
        for path in ("/", "/api/board"):
            r = self.get(path)
            self.assertEqual(r.getheader("Content-Security-Policy"),
                             "frame-ancestors 'none'", path)
            self.assertEqual(r.getheader("X-Frame-Options"), "DENY", path)

    def test_board_feed_refuses_non_finite_numbers(self):
        from csb.web import BoardFeed
        with self.assertRaises(ValueError):
            BoardFeed().publish({"ts": float("inf")})


if __name__ == "__main__":
    unittest.main()
