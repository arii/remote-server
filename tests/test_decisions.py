"""deep-agent/decisions.py unit tests. Stdlib-only, no langchain."""
import unittest

import decisions


class TestBuildDecisions(unittest.TestCase):
    def test_tools_plus_verdict(self):
        out = decisions.build_decisions(
            [{"name": "grep_repo", "args": '{"q": "x"}'}],
            ["found it here"],
            "The answer is 42.",
            reason="explain turn, full path")
        self.assertEqual(len(out), 2)
        self.assertIn("grep_repo", out[0]["what"])
        self.assertIn("found it here", out[0]["why"])
        self.assertIn("1 tool result", out[1]["what"])
        self.assertEqual(out[1]["why"], "explain turn, full path")
        self.assertNotIn("42", out[1]["why"])

    def test_no_tools_answers_directly(self):
        out = decisions.build_decisions(
            [], [], "Hi there.", reason="trivial greeting, fast path")
        self.assertEqual(len(out), 1)
        self.assertIn("no tools", out[0]["what"])
        self.assertEqual(out[0]["why"], "trivial greeting, fast path")
        self.assertNotIn("Hi there", out[0]["why"])

    def test_answer_why_falls_back_to_reply(self):
        out = decisions.build_decisions([], [], "Hi there.")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["why"], "Hi there.")

    def test_answer_why_empty_reply(self):
        out = decisions.build_decisions([], [], "")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["why"], "(empty reply)")

    def test_error_path_reports_failure(self):
        out = decisions.build_decisions(
            [{"name": "read_repo_file", "args": "x"}], [],
            "Sorry, the assistant failed: boom",
            error="boom")
        whats = [d["what"] for d in out]
        self.assertTrue(any("failed" in w for w in whats))
        self.assertTrue(any("boom" in d["why"] for d in out))

    def test_hitl_reject_carries_reason(self):
        out = decisions.build_decisions(
            [], [], "Rejected, nothing ran.",
            verdict="reject", message="too risky for Friday",
            proposals=["rm -rf /tmp/x"])
        whats = [d["what"] for d in out]
        self.assertTrue(any("rejected" in w for w in whats))
        self.assertTrue(
            any("too risky" in d["why"] for d in out))

    def test_hitl_approve_without_message_uses_proposal(self):
        out = decisions.build_decisions(
            [], [], "Approved and ran.",
            verdict="approve", message="",
            proposals=["restart the service"])
        self.assertTrue(
            any("restart the service" in d["why"] for d in out))

    def test_caps_long_fields(self):
        out = decisions.build_decisions(
            [{"name": "t", "args": "a" * 500}], ["r" * 500],
            "v" * 500)
        for d in out:
            self.assertLessEqual(len(d["what"]), 260)
            self.assertLessEqual(len(d["why"]), 260)

    def test_never_raises_on_junk(self):
        out = decisions.build_decisions(
            None, None, None, verdict=None, message=None,
            error=None, proposals=None)
        self.assertIsInstance(out, list)
        out = decisions.build_decisions(
            ["nope", {"name": 1}], "junk", 42)
        self.assertIsInstance(out, list)

    def test_tool_text_handles_blocks(self):
        self.assertEqual(decisions.tool_text("plain"), "plain")
        self.assertEqual(
            decisions.tool_text([{"text": "a"}, {"text": "b"}]),
            "a b")
        self.assertEqual(decisions.tool_text(None), "")


if __name__ == "__main__":
    unittest.main()
