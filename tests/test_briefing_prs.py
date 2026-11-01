#!/usr/bin/env python3
"""Tests to ensure open PRs are properly structured in briefing and handled in server UI."""

import unittest
from buttons.briefing_summary import analyze


class TestBriefingPrs(unittest.TestCase):
    def test_analyze_includes_pr_list_and_prs(self):
        mock_data = {
            "pr_list": [
                {"number": 101, "title": "Fix auth flow", "headRefName": "fix/auth"},
                {"number": 102, "title": "Update UI styling", "headRefName": "feat/ui"},
            ],
            "gh": {},
            "conf": {},
            "reviews": {},
            "reviews_err": {},
            "board": None,
            "board_err": None,
            "runs": None,
            "runs_err": None,
            "jobs": {},
            "jules": [],
            "jules_err": None,
            "prs_err": None,
            "conf_err": None,
        }
        res = analyze(mock_data)
        self.assertIn("prs", res)
        self.assertIn("pr_list", res)
        self.assertEqual(len(res["prs"]), 2)
        self.assertEqual(len(res["pr_list"]), 2)

        numbers_and_titles = [(p["number"], p["title"]) for p in res["pr_list"]]
        self.assertEqual(
            numbers_and_titles,
            [(101, "Fix auth flow"), (102, "Update UI styling")]
        )

    def test_server_html_fallback(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("briefingPrs = res.data.pr_list || res.data.prs || [];", content)


if __name__ == "__main__":
    unittest.main()
