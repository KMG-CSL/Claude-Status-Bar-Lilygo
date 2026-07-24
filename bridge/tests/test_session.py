"""Pin the transcript parser: token accounting, pending tool_use lifecycle,
sidechain exclusion, titles, rotation/truncation, big-file seek."""

import json
import os
import tempfile
import unittest

from csb.session import Session, find_transcripts

from tests.helpers import (T0, assistant_text, assistant_tool_use, iso,
                           make_session, noise, sidechain, summary,
                           tool_result, user, write_jsonl)


class SessionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make(self, records, **kw):
        return make_session(self.tmp.name, records, **kw)


class TestParserFacts(SessionCase):
    def test_token_accounting(self):
        us = {"input_tokens": 100, "output_tokens": 20,
              "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10}
        events = []
        s = self.make([user(T0)])
        write_jsonl(s.path, [user(T0), assistant_text(T0 + 5, usage=us)])
        s.poll(events)
        self.assertEqual(s.tok_in, 110)          # input + cache_creation
        self.assertEqual(s.tok_out, 20)
        self.assertEqual(s.ctx_tokens, 160)      # input + cache_read + cache_creation
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][1], 130)      # input + cache_creation + output

    def test_usage_accumulates_but_ctx_is_last(self):
        us1 = {"input_tokens": 100, "output_tokens": 10}
        us2 = {"input_tokens": 200, "output_tokens": 30}
        s = self.make([user(T0), assistant_text(T0 + 1, usage=us1),
                       assistant_text(T0 + 2, usage=us2)])
        self.assertEqual(s.tok_in, 300)
        self.assertEqual(s.tok_out, 40)
        self.assertEqual(s.ctx_tokens, 200)      # snapshot, not a sum

    def test_pending_tool_lifecycle(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 1, "Bash", "tu_9")])
        self.assertIn("tu_9", s.pending_ids)
        name, ts, detail = s.pending_ids["tu_9"]
        self.assertEqual(name, "Bash")
        self.assertEqual(ts, T0 + 1)
        self.assertEqual(detail, "npm test")
        write_jsonl(s.path, [user(T0), assistant_tool_use(T0 + 1, "Bash", "tu_9"),
                             tool_result(T0 + 2, "tu_9")])
        s.poll([])
        self.assertEqual(s.pending_ids, {})

    def test_tool_result_does_not_move_turn_start(self):
        s = self.make([user(T0), assistant_tool_use(T0 + 1, "Bash", "tu_1"),
                       tool_result(T0 + 2, "tu_1")])
        self.assertEqual(s.turn_start, T0)
        self.assertEqual(s.last_role, "user")    # tool_result is a user record
        self.assertEqual(s.last_event_ts, T0 + 2)

    def test_sidechain_records_excluded(self):
        s = self.make([user(T0),
                       sidechain(assistant_tool_use(T0 + 1, "Bash", "tu_sc"))])
        self.assertEqual(s.pending_ids, {})
        self.assertEqual(s.model, "")
        self.assertEqual(s.last_role, "user")

    def test_synthetic_model_skipped(self):
        s = self.make([user(T0), assistant_text(T0 + 1, model="<synthetic>")])
        self.assertEqual(s.model, "")

    def test_project_from_first_cwd(self):
        s = self.make([user(T0, cwd="/home/u/projects/widget")])
        self.assertEqual(s.project, "widget")

    def test_name_from_first_user_text_and_summary_precedence(self):
        s = self.make([user(T0, text="please fix the frobnicator carefully")])
        self.assertEqual(s.name, "please fix the frobnicat")   # capped at 24
        # summary overrides the prompt-derived name
        with open(s.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(summary(T0 + 1, "Frobnicator repair")) + "\n")
        s.poll([])
        self.assertEqual(s.name, "Frobnicator repair")

    def test_titles(self):
        s = self.make([user(T0),
                       {"type": "ai-title", "timestamp": iso(T0 + 1),
                        "aiTitle": "AI title"},
                       {"type": "custom-title", "timestamp": iso(T0 + 2),
                        "customTitle": "My title"}])
        self.assertEqual(s.ai_title, "AI title")
        self.assertEqual(s.custom_title, "My title")

    def test_noise_records_bump_nothing(self):
        s = self.make([noise(T0, "file-history-snapshot"),
                       noise(T0 + 1, "queue-operation"),
                       noise(T0 + 2, "last-prompt"),
                       noise(T0 + 3, "attachment"),
                       noise(T0 + 4, "bridge-session")])
        self.assertIsNone(s.last_event_ts)
        self.assertIsNone(s.turn_start)
        self.assertEqual(s.model, "")
        self.assertEqual(s.last_role, "")

    def test_noise_after_real_events_does_not_advance_activity(self):
        s = self.make([user(T0), assistant_text(T0 + 5)])
        write_jsonl(s.path, [user(T0), assistant_text(T0 + 5),
                             noise(T0 + 50, "file-history-snapshot"),
                             noise(T0 + 51, "queue-operation"),
                             noise(T0 + 52, "last-prompt"),
                             noise(T0 + 53, "attachment"),
                             noise(T0 + 54, "bridge-session")])
        s.poll([])
        self.assertEqual(s.last_event_ts, T0 + 5)
        self.assertEqual(s.last_role, "assistant")

    def test_non_json_lines_ignored(self):
        s = self.make([user(T0)])
        with open(s.path, "a", encoding="utf-8") as f:
            f.write("garbage line\n{broken json\n")
        s.poll([])
        self.assertEqual(s.last_role, "user")


class TestTailing(SessionCase):
    def test_incremental_offset(self):
        s = self.make([user(T0)])
        size1 = os.path.getsize(s.path)
        self.assertEqual(s.offset, size1)
        with open(s.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(assistant_text(T0 + 5)) + "\n")
        s.poll([])
        self.assertEqual(s.last_role, "assistant")
        self.assertGreater(s.offset, size1)

    def test_truncation_resets_offset(self):
        s = self.make([user(T0, text="a long first prompt to make the file big"),
                       assistant_text(T0 + 1, text="working on it right now")])
        self.assertEqual(s.last_role, "assistant")
        # rotate: replace with a strictly smaller file
        write_jsonl(s.path, [user(T0 + 10, text="hi")])
        s.poll([])
        self.assertEqual(s.last_role, "user")
        self.assertEqual(s.turn_start, T0 + 10)

    def test_missing_file_is_noop(self):
        s = Session(os.path.join(self.tmp.name, "gone.jsonl"))
        s.poll([])   # must not raise
        self.assertEqual(s.offset, 0)

    def test_big_file_seeks_to_tail(self):
        path = os.path.join(self.tmp.name, "big.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x" * (21 * 1024 * 1024) + "\n")     # one giant junk line
            f.write(json.dumps(user(T0)) + "\n")
            f.write(json.dumps(assistant_text(T0 + 1)) + "\n")
        s = Session(path)
        s.poll([])
        self.assertEqual(s.last_role, "assistant")        # tail was parsed
        self.assertEqual(s.turn_start, T0)
        self.assertEqual(s.offset, os.path.getsize(path))


class TestFindTranscripts(SessionCase):
    def test_string_root_treated_as_single_path(self):
        # A bare-string root must not be iterated char-by-char ("/" is a
        # directory -> whole-filesystem walk). It should behave like [root].
        path = os.path.join(self.tmp.name, "a.jsonl")
        with open(path, "w") as f:
            f.write("{}\n")
        found = find_transcripts(self.tmp.name)
        self.assertEqual(list(found), [path])
        self.assertEqual(found, find_transcripts([self.tmp.name]))

    def test_audit_jsonl_skipped(self):
        with open(os.path.join(self.tmp.name, "audit.jsonl"), "w") as f:
            f.write("{}\n")
        self.assertEqual(find_transcripts([self.tmp.name]), {})

    def test_compact_basenames_skipped(self):
        # compaction artifacts are not sessions (Stargx §6.1)
        for fn in ("acompact-1234.jsonl", "pre-compact-save.jsonl"):
            with open(os.path.join(self.tmp.name, fn), "w") as f:
                f.write("{}\n")
        keep = os.path.join(self.tmp.name, "real-session.jsonl")
        with open(keep, "w") as f:
            f.write("{}\n")
        self.assertEqual(list(find_transcripts([self.tmp.name])), [keep])


class TestPermissionMode(SessionCase):
    def test_newest_mode_wins(self):
        s = self.make([dict(user(T0), permissionMode="plan"),
                       dict(assistant_text(T0 + 1),
                            permissionMode="acceptEdits")])
        self.assertEqual(s.permission_mode, "acceptEdits")

    def test_sidechain_mode_is_not_the_sessions(self):
        s = self.make([dict(user(T0), permissionMode="plan"),
                       sidechain(dict(assistant_text(T0 + 1),
                                      permissionMode="bypassPermissions"))])
        self.assertEqual(s.permission_mode, "plan")

    def test_absent_mode_stays_empty(self):
        s = self.make([user(T0), assistant_text(T0 + 1)])
        self.assertEqual(s.permission_mode, "")


class TestSubagentLiveness(SessionCase):
    """sa counts only subagents with events in the last subagent_live_s
    (Stargx liveness, default 15); compaction helpers never count."""

    def _with_subagents(self, files, now=T0):
        s = self.make([user(T0), assistant_text(T0 + 1)])
        base = os.path.join(
            os.path.dirname(s.path),
            os.path.splitext(os.path.basename(s.path))[0], "subagents")
        os.makedirs(base)
        for fn, age in files:
            p = os.path.join(base, fn)
            with open(p, "w") as f:
                f.write("{}\n")
            os.utime(p, (now - age, now - age))
        return s

    def test_only_recently_active_subagents_count(self):
        s = self._with_subagents([("agent-a1.jsonl", 5),
                                  ("agent-a2.jsonl", 14),
                                  ("agent-a3.jsonl", 16),
                                  ("agent-a4.jsonl", 120)])
        self.assertEqual(s.subagent_count({"subagent_live_s": 15}, now=T0), 2)

    def test_default_window_is_15s(self):
        # was 90: a helper idle for 20s no longer counts as live
        s = self._with_subagents([("agent-old.jsonl", 20),
                                  ("agent-live.jsonl", 3)])
        self.assertEqual(s.subagent_count({}, now=T0), 1)

    def test_compact_helpers_never_count(self):
        s = self._with_subagents([("agent-acompact123.jsonl", 1),
                                  ("agent-precompact-x.jsonl", 1),
                                  ("agent-real.jsonl", 1)])
        self.assertEqual(s.subagent_count({"subagent_live_s": 15}, now=T0), 1)


class IgnoreProjectsCase(unittest.TestCase):
    """Self-probe filter: tooling-spawned scratch sessions never display."""

    def test_ignore_filters_project_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            for proj in ("-Users-u-projects-real",
                         "-private-tmp-claude-123-abc-scratchpad",
                         "-tmp-claude-456-def-scratchpad"):
                d = os.path.join(td, proj)
                os.makedirs(d)
                with open(os.path.join(d, "s.jsonl"), "w") as f:
                    f.write("{}\n")
            self.assertEqual(len(find_transcripts([td])), 3)
            kept = find_transcripts([td], ["-tmp-claude-"])
            self.assertEqual(len(kept), 1)
            self.assertIn("real", next(iter(kept)))

    def test_empty_patterns_filter_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            d = os.path.join(td, "-Users-u-projects-real")
            os.makedirs(d)
            with open(os.path.join(d, "s.jsonl"), "w") as f:
                f.write("{}\n")
            self.assertEqual(len(find_transcripts([td], ["", None])), 1)


if __name__ == "__main__":
    unittest.main()
