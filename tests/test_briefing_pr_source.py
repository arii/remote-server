#!/usr/bin/env python3
"""Briefing open PRs come from REST, not search (issue #446).

Regression: the open-PR list came from `td gh search-prs`, and
GitHub's search index lags merges by minutes — a merged PR kept
showing as open, and its board row kept showing Open. The list now
comes from `gh pr list --state open` (strongly consistent), and
board rows for merged branches relabel to Merged.
"""

import unittest
from unittest import mock

import core_cli.briefing_summary as bs


OPEN_ROWS = [
    {"number": 576, "title": "live", "headRefName": "fix/live",
     "isDraft": False, "updatedAt": "2026-11-04T00:00:00Z",
     "mergeStateStatus": "CLEAN", "url": "https://example.invalid/576"},
]
MERGED_ROWS = [
    {"number": 434, "headRefName": "feat/repair-abort-407"},
]


def _payload_for(argv):
    if argv[:3] == ["gh", "pr", "list"]:
        if "merged" in argv:
            return list(MERGED_ROWS), "", ""
        return list(OPEN_ROWS), "raw-open", ""
    if argv[:3] == ["gh", "pr", "view"]:
        return {"number": 576}, "", ""
    if "read-pr-reviews" in argv:
        return {"reviews": []}, "", ""
    if "workflow-runs" in argv:
        return {"workflow_runs": []}, "", ""
    if "status-board" in argv:
        return {"work": []}, "", ""
    if "detect-conflicts" in argv:
        return {}, "", ""
    if "list-sessions" in argv:
        return {"sessions": []}, "", ""
    return {}, "", ""


def _run_fetch(payload_for=_payload_for,
               key="remote-server", gh="arii/remote-server"):
    calls = []

    def fake_run_json(cmd, timeout, cwd=None):
        calls.append(cmd)
        return payload_for(cmd)

    old_key, old_gh = bs.REPO_KEY, bs.REPO_GH
    bs.REPO_KEY, bs.REPO_GH = key, gh
    try:
        with mock.patch.object(bs, "run_json",
                               side_effect=fake_run_json), \
                mock.patch.object(bs, "_target_checkout_cwd",
                                  return_value=""), \
                mock.patch.object(bs, "_config_repo",
                                  return_value=gh):
            data = bs.fetch()
    finally:
        bs.REPO_KEY, bs.REPO_GH = old_key, old_gh
    return data, calls


def _analyze_data(**over):
    data = {
        "prs": None,
        "prs_raw": "",
        "prs_err": "",
        "pr_list": [],
        "gh": {},
        "conf": None,
        "conf_raw": "",
        "conf_err": "",
        "reviews": {},
        "reviews_err": {},
        "reviews_raw_parts": [],
        "board": None,
        "board_raw": "",
        "board_err": "",
        "runs": None,
        "runs_raw": "",
        "runs_err": "",
        "jobs": {},
        "jules": [],
        "jules_err": "",
        "jules_raw": "",
        "merged_heads": set(),
    }
    data.update(over)
    return data


class PrSourceTest(unittest.TestCase):
    def test_open_prs_come_from_rest_not_search(self):
        _, calls = _run_fetch()
        kinds = [" ".join(c) for c in calls]
        self.assertFalse(any("search-prs" in k for k in kinds))
        opens = [c for c in calls if c[:3] == ["gh", "pr", "list"]
                 and "merged" not in c]
        self.assertEqual(len(opens), 1)
        self.assertIn("--state", opens[0])
        self.assertIn("open", opens[0])
        self.assertIn("--repo", opens[0])
        self.assertIn("arii/remote-server", opens[0])

    def test_pr_list_parses_bare_rest_list(self):
        data, _ = _run_fetch()
        self.assertEqual(data["pr_list"], OPEN_ROWS)
        self.assertEqual(data["prs_err"], "")

    def test_merged_heads_recorded(self):
        data, _ = _run_fetch()
        self.assertEqual(data["merged_heads"],
                         {"feat/repair-abort-407"})

    def test_merged_query_failure_degrades_silently(self):
        def payload(argv):
            if argv[:3] == ["gh", "pr", "list"] and "merged" in argv:
                return None, "", "boom"
            return _payload_for(argv)

        data, _ = _run_fetch(payload_for=payload)
        self.assertEqual(data["merged_heads"], set())
        self.assertEqual(data["pr_list"], OPEN_ROWS)


class BoardRelabelTest(unittest.TestCase):
    def _board_out(self, work, pr_list=None, merged=None):
        data = _analyze_data(
            pr_list=(pr_list if pr_list is not None else list(OPEN_ROWS)),
            board={"work": work},
            merged_heads=(merged if merged is not None
                          else {"feat/repair-abort-407"}),
        )
        return bs.analyze(data)

    def test_merged_branch_open_row_relabels(self):
        out = self._board_out([
            {"branch": "feat/repair-abort-407", "issue": "",
             "status": "Open"},
        ])
        self.assertEqual(out["board"][0]["status"], "Merged")
        self.assertEqual(out["board_lines"],
                         ["- feat/repair-abort-407 — Merged"])

    def test_open_pr_branch_never_relabels(self):
        out = self._board_out(
            [{"branch": "fix/live", "issue": "", "status": "Open"}],
            merged={"fix/live"},
        )
        self.assertEqual(out["board"][0]["status"], "Open")

    def test_unknown_branch_keeps_status(self):
        out = self._board_out([
            {"branch": "wip/no-pr", "issue": "", "status": "Open"},
        ])
        self.assertEqual(out["board"][0]["status"], "Open")

    def test_non_open_status_untouched(self):
        out = self._board_out([
            {"branch": "feat/repair-abort-407", "issue": "",
             "status": "In review"},
        ])
        self.assertEqual(out["board"][0]["status"], "In review")

    def test_missing_merged_heads_tolerated(self):
        data = _analyze_data(
            pr_list=list(OPEN_ROWS),
            board={"work": [{"branch": "b", "issue": "",
                             "status": "Open"}]},
        )
        del data["merged_heads"]
        out = bs.analyze(data)
        self.assertEqual(out["board"][0]["status"], "Open")


class AppendixLabelTest(unittest.TestCase):
    def test_raw_section_named_pr_list(self):
        data = _analyze_data(prs_raw='[{"number": 576}]')
        _, _, raw = bs.build(data)
        self.assertIn("### pr-list", raw)
        self.assertNotIn("search-prs", raw)


if __name__ == "__main__":
    unittest.main()
