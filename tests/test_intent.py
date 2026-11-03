"""intent.py unit tests. Deterministic keyword routing, no LLM.
Run: python3 tests/test_intent.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import intent


class TestClassify(unittest.TestCase):
    def test_imperative_implement(self):
        self.assertEqual(intent.classify("Implement a dark mode toggle"),
                         "implement")
        self.assertEqual(intent.classify("Build me a CSV exporter"),
                         "implement")
        self.assertEqual(intent.classify("Add rate limiting to /api/chat"),
                         "implement")

    def test_action_request_implement(self):
        self.assertEqual(
            intent.classify("Can you implement SSO login for me?"),
            "implement")

    def test_how_question_is_explain(self):
        self.assertEqual(
            intent.classify("How do I implement caching here?"), "explain")
        self.assertEqual(
            intent.classify("What does the router do?"), "explain")

    def test_fix_signals(self):
        self.assertEqual(intent.classify("Fix the login redirect loop"),
                         "fix")
        self.assertEqual(intent.classify("The briefing page is broken"),
                         "fix")
        self.assertEqual(intent.classify("Can you fix the failing test?"),
                         "fix")
        self.assertEqual(intent.classify("PR #123 fails CI"), "fix")

    def test_error_meaning_is_explain(self):
        self.assertEqual(
            intent.classify("What does this error mean: 502?"), "explain")

    def test_review(self):
        self.assertEqual(intent.classify("Review PR #45"), "review")
        self.assertEqual(intent.classify("Can you review my pull request?"),
                         "review")

    def test_default_explain(self):
        self.assertEqual(intent.classify("Reply with exactly: hi"), "explain")
        self.assertEqual(intent.classify(""), "explain")

    def test_case_insensitive(self):
        self.assertEqual(intent.classify("FIX IT NOW"), "fix")


class TestHint(unittest.TestCase):
    def test_implement_points_at_dispatch(self):
        self.assertIn("dispatch", intent.hint("implement"))

    def test_fix_points_at_investigate_then_propose(self):
        hint = intent.hint("fix")
        self.assertIn("propose_action", hint)

    def test_review_is_readonly(self):
        self.assertIn("read-only", intent.hint("review"))

    def test_explain_has_no_hint(self):
        self.assertIsNone(intent.hint("explain"))
        self.assertIsNone(intent.hint("bogus"))


if __name__ == "__main__":
    unittest.main()
