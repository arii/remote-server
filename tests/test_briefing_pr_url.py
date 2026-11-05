#!/usr/bin/env python3
"""Briefing PR rows carry their web URL (issue #902).

The Briefing pipeline pills link out — verdict to the PR, CI to its
checks tab — so analyze() must pass the PR web URL through from the
`gh pr list` row, preferring the enriched `gh pr view` URL when the
PR was enriched. Rows without a URL keep a falsy value and the
frontend falls back to the in-app Review section.
"""

import unittest

import backend.briefing_summary as bs


def _analyze_data(**over):
    data = {
        "prs": None, "prs_raw": "", "prs_err": "",
        "pr_list": [], "gh": {},
        "conf": None, "conf_raw": "", "conf_err": "",
        "reviews": {}, "reviews_err": {},
        "reviews_raw_parts": [],
        "board": None, "board_raw": "", "board_err": "",
        "runs": None, "runs_raw": "", "runs_err": "",
        "jobs": {}, "merged_heads": set(),
    }
    data.update(over)
    return data


class PrUrlTest(unittest.TestCase):
    def test_list_row_url_flows_through(self):
        data = _analyze_data(pr_list=[
            {"number": 12, "title": "Fix it", "headRefName": "fix/it",
             "isDraft": False, "updatedAt": "2026-11-04T00:00:00Z",
             "mergeStateStatus": "CLEAN",
             "url": "https://github.com/o/r/pull/12"},
        ])
        prs = bs.analyze(data)["prs"]
        self.assertEqual(len(prs), 1)
        self.assertEqual(prs[0]["url"], "https://github.com/o/r/pull/12")

    def test_enriched_view_url_wins(self):
        data = _analyze_data(
            pr_list=[
                {"number": 12, "title": "Fix it",
                 "headRefName": "fix/it", "isDraft": False,
                 "updatedAt": "2026-11-04T00:00:00Z",
                 "mergeStateStatus": "CLEAN",
                 "url": "https://github.com/o/r/pull/12"},
            ],
            gh={12: {"url": "https://github.com/o/r/pull/12?view=enriched"}})
        prs = bs.analyze(data)["prs"]
        self.assertEqual(prs[0]["url"],
                         "https://github.com/o/r/pull/12?view=enriched")

    def test_missing_url_stays_falsy(self):
        data = _analyze_data(pr_list=[
            {"number": 12, "title": "Fix it", "headRefName": "fix/it",
             "isDraft": False, "updatedAt": "2026-11-04T00:00:00Z",
             "mergeStateStatus": "CLEAN"},
        ])
        prs = bs.analyze(data)["prs"]
        self.assertFalse(prs[0]["url"])


if __name__ == "__main__":
    unittest.main()
