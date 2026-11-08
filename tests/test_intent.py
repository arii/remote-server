"""intent.py unit tests. Deterministic keyword routing, no LLM.
Run: python3 tests/test_intent.py"""
import unittest

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


class TestTrivial(unittest.TestCase):
    def test_greetings_are_trivial(self):
        for text in ("hi", "Hi", "HI!", "hi?", "hi...", "hi…", "  hi  ",
                     "hello", "Hello!", "hey", "yo", "howdy", "sup",
                     "thanks", "Thanks!", "thank you", "thx", "bye",
                     "goodbye", "Goodbye.", "good morning",
                     "good afternoon", "good evening", "good night",
                     "morning", "night"):
            self.assertEqual(intent.classify(text), "trivial", text)

    def test_work_mentions_are_not_trivial(self):
        self.assertEqual(intent.classify("hi, fix the login bug"), "fix")
        self.assertEqual(
            intent.classify("thanks for fixing the bug"), "fix")
        self.assertEqual(
            intent.classify("hello, review PR #5 for me"), "review")
        self.assertEqual(
            intent.classify("Add a hello endpoint"), "implement")
        self.assertEqual(
            intent.classify("hey what does this do?"), "explain")


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

    def test_trivial_has_no_hint(self):
        self.assertIsNone(intent.hint("trivial"))


if __name__ == "__main__":
    unittest.main()
