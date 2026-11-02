#!/usr/bin/env python3
"""Deterministic suggest mapping: briefing state to runnable commands."""

import unittest

import buttons.server as srv


class TestSuggestMapping(unittest.TestCase):
    def suggest(self, brief):
        return srv._parse_suggestions(srv._suggest_from_briefing(brief))

    def test_conflict_first(self):
        suggs = self.suggest({"prs": [{"number": 148, "conflicted": True,
                                       "ci": "failing",
                                       "review": "CHANGES_REQUESTED"}]})
        self.assertEqual(len(suggs), 1)
        self.assertIn("resolve-conflicts --pr 148", suggs[0]["command"])

    def test_failing_ci(self):
        suggs = self.suggest({"prs": [{"number": 7, "ci": "failing"}]})
        self.assertEqual(len(suggs), 1)
        self.assertIn("fix-ci --pr 7", suggs[0]["command"])

    def test_changes_requested(self):
        suggs = self.suggest({"prs": [{"number": 9, "ci": "passing",
                                       "review": "CHANGES_REQUESTED"}]})
        self.assertEqual(len(suggs), 1)
        self.assertIn("read-pr-reviews --pr 9", suggs[0]["command"])

    def test_failing_run_matched_by_branch(self):
        brief = {"prs": [{"number": 11, "branch": "feat-x",
                          "ci": "passing"}],
                 "failing_runs": [{"name": "CI", "branch": "feat-x"}]}
        suggs = self.suggest(brief)
        self.assertEqual(len(suggs), 1)
        self.assertIn("fix-ci --pr 11", suggs[0]["command"])

    def test_unmatched_attention_suggests_none(self):
        brief = {"prs": [{"number": "x"}],
                 "attention": ["something"],
                 "failing_runs": [{"name": "CI", "branch": "main"}]}
        raw = srv._suggest_from_briefing(brief)
        self.assertIn("SUGGEST: none", raw)
        self.assertEqual(self.suggest(brief), [])

    def test_preflight_never_suggested(self):
        brief = {"prs": [], "attention": ["a"],
                 "failing_runs": [{"name": "CI", "branch": "main"}]}
        self.assertNotIn("preflight", srv._suggest_from_briefing(brief))

    def test_bad_pr_numbers_ignored(self):
        suggs = self.suggest({"prs": [{"number": 0, "conflicted": True},
                                      {"number": -3, "ci": "failing"},
                                      {"number": "12", "ci": "failing"}]})
        self.assertEqual(suggs, [])


if __name__ == "__main__":
    unittest.main()
