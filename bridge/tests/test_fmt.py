"""Pin the pure formatting helpers."""

import unittest

from csb.fmt import (fmt_countdown, fmt_tokens, parse_ts, pretty_model,
                     pretty_tool, tool_detail)


class TestParseTs(unittest.TestCase):
    def test_z_suffix(self):
        self.assertEqual(parse_ts("1970-01-01T00:01:00Z"), 60.0)

    def test_explicit_offset(self):
        self.assertEqual(parse_ts("1970-01-01T01:01:00+01:00"), 60.0)

    def test_millis(self):
        self.assertAlmostEqual(parse_ts("1970-01-01T00:01:00.500Z"), 60.5)

    def test_bad_input(self):
        self.assertIsNone(parse_ts(None))
        self.assertIsNone(parse_ts(""))
        self.assertIsNone(parse_ts("not a date"))


class TestPrettyModel(unittest.TestCase):
    def test_fable(self):
        self.assertEqual(pretty_model("claude-fable-5-20251101"), "Fable 5")

    def test_opus_two_part_version(self):
        self.assertEqual(pretty_model("claude-opus-4-8-20250115"), "Opus 4.8")

    def test_version_before_name(self):
        # digits collect into the version tail regardless of position
        self.assertEqual(pretty_model("claude-3-5-sonnet-20241022"), "Sonnet 3.5")

    def test_empty(self):
        self.assertEqual(pretty_model(""), "Claude")
        self.assertEqual(pretty_model(None), "Claude")

    def test_truncated_to_20(self):
        self.assertLessEqual(len(pretty_model("claude-" + "x" * 40)), 20)


class TestPrettyTool(unittest.TestCase):
    def test_passthrough(self):
        self.assertEqual(pretty_tool("Read"), "Read")

    def test_mcp_prefix_stripped_and_capitalized(self):
        self.assertEqual(pretty_tool("mcp__workspace__bash"), "Bash")

    def test_underscores_to_spaces(self):
        self.assertEqual(pretty_tool("mcp__ws__run_thing"), "Run thing")

    def test_empty(self):
        self.assertEqual(pretty_tool(""), "")

    def test_truncated_to_16(self):
        self.assertLessEqual(len(pretty_tool("a" * 40)), 16)


class TestToolDetail(unittest.TestCase):
    def test_command_whitespace_collapsed(self):
        self.assertEqual(tool_detail({"command": "npm   test\n--watch"}),
                         "npm test --watch")

    def test_path_basename(self):
        self.assertEqual(tool_detail({"file_path": "/a/b/c.py"}), "c.py")

    def test_question_wins_over_command(self):
        d = tool_detail({"questions": [{"question": "Deploy now?"}],
                         "command": "ls"})
        self.assertEqual(d, "Deploy now?")

    def test_priority_command_over_url(self):
        self.assertEqual(tool_detail({"url": "http://x", "command": "ls"}), "ls")

    def test_non_dict(self):
        self.assertEqual(tool_detail(None), "")
        self.assertEqual(tool_detail("x"), "")

    def test_truncated_to_36(self):
        self.assertEqual(len(tool_detail({"command": "x" * 100})), 36)


class TestFmtTokens(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(fmt_tokens(999), "999")
        self.assertEqual(fmt_tokens(1000), "1k")
        self.assertEqual(fmt_tokens(1999), "1k")      # floor division
        self.assertEqual(fmt_tokens(999999), "999k")
        self.assertEqual(fmt_tokens(1000000), "1.0M")
        self.assertEqual(fmt_tokens(1500000), "1.5M")


class TestFmtCountdown(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(fmt_countdown(None), "")
        self.assertEqual(fmt_countdown(-1), "")
        self.assertEqual(fmt_countdown(0), "0m")
        self.assertEqual(fmt_countdown(59 * 60), "59m")
        self.assertEqual(fmt_countdown(3600), "1h00m")
        self.assertEqual(fmt_countdown(3600 * 2 + 14 * 60), "2h14m")
        self.assertEqual(fmt_countdown(36000), "10h")   # h >= 10 drops minutes
        self.assertEqual(fmt_countdown(86400 + 3600), "1d1h")


if __name__ == "__main__":
    unittest.main()
