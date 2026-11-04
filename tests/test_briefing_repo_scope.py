#!/usr/bin/env python3
"""Briefing honors its --repo target (issue #260).

Regression: every td/gh subprocess ran unscoped, so briefing always
briefed the checkout repo (td discovers config from cwd; gh resolves
PR numbers against the local remote). Review (/api/prs, gh-scoped)
and Briefing (td, unscoped) disagreed about open PRs.
"""

import unittest
from unittest import mock

import core_cli.briefing_summary as bs


def _payload_for(argv):
    if argv[:3] == ["gh", "pr", "list"]:
        if "merged" in argv:
            return [{"number": 1094, "headRefName": "old"}], "", ""
        return [{"number": 1095, "title": "t",
                 "headRefName": "b"}], "", ""
    if argv[:3] == ["gh", "pr", "view"]:
        return {"number": 1095}, "", ""
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


class FetchScopeTest(unittest.TestCase):
    def _run_fetch(self, key="boomtick", gh="arii/boomtick",
                   checkout_cwd="", config_repo="arii/boomtick"):
        calls = []

        def fake_run_json(cmd, timeout, cwd=None):
            calls.append((cmd, cwd))
            return _payload_for(cmd)

        old_key, old_gh = bs.REPO_KEY, bs.REPO_GH
        bs.REPO_KEY, bs.REPO_GH = key, gh
        try:
            with mock.patch.object(bs, "run_json",
                                   side_effect=fake_run_json), \
                    mock.patch.object(bs, "_target_checkout_cwd",
                                      return_value=checkout_cwd), \
                    mock.patch.object(bs, "_config_repo",
                                      return_value=config_repo):
                data = bs.fetch()
        finally:
            bs.REPO_KEY, bs.REPO_GH = old_key, old_gh
        return data, calls

    def test_every_td_gh_call_carries_repo(self):
        _, calls = self._run_fetch()
        scoped = [c for c, _ in calls
                  if "status-board" not in c
                  and "detect-conflicts" not in c
                  and "list-sessions" not in c]
        self.assertTrue(scoped)
        for cmd in scoped:
            self.assertIn("--repo", cmd, cmd)
            self.assertIn("arii/boomtick", cmd, cmd)

    def test_board_scoped_via_checkout_cwd(self):
        _, calls = self._run_fetch(checkout_cwd="/srv/repos/boomtick")
        board = [(c, w) for c, w in calls if "status-board" in c]
        conf = [(c, w) for c, w in calls if "detect-conflicts" in c]
        self.assertEqual(len(board), 1)
        self.assertEqual(board[0][1], "/srv/repos/boomtick")
        self.assertEqual(len(conf), 1)
        self.assertEqual(conf[0][1], "/srv/repos/boomtick")

    def test_board_unavailable_cross_repo_without_checkout(self):
        data, calls = self._run_fetch(
            checkout_cwd="", config_repo="arii/remote-server")
        self.assertIsNone(data["board"])
        self.assertIn("arii/boomtick", data["board_err"])
        self.assertIsNone(data["conf"])
        self.assertIn("arii/boomtick", data["conf_err"])
        kinds = [" ".join(c) for c, _ in calls]
        self.assertFalse(any("status-board" in k for k in kinds))
        self.assertFalse(any("detect-conflicts" in k for k in kinds))

    def test_board_runs_unspecified_for_own_repo(self):
        data, calls = self._run_fetch(
            key="remote-server", gh="arii/remote-server",
            checkout_cwd="", config_repo="arii/remote-server")
        self.assertEqual(data["board"], {"work": []})
        self.assertEqual(data["board_err"], "")


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
    }
    data.update(over)
    return data


class LineFormatTest(unittest.TestCase):
    def test_reviews_render_single_lines(self):
        data = _analyze_data(
            pr_list=[{"number": 256, "title": "t",
                      "headRefName": "b"}],
            reviews={256: [
                ("boomtick[bot]", "APPROVED", "", "s"),
                ("ari", "COMMENTED", "nice", "s"),
                ("ari", "CHANGES_REQUESTED", "", "s"),
            ]},
        )
        out = bs.analyze(data)
        self.assertEqual(out["reviews_lines"], [
            "- PR #256 — changes requested by @ari",
            "- PR #256 — approved by @boomtick[bot] (+1 more)",
        ])

    def test_reviews_include_body_when_present(self):
        data = _analyze_data(
            pr_list=[{"number": 7, "title": "t",
                      "headRefName": "b"}],
            reviews={7: [("ari", "COMMENTED", "looks good", "s")]},
        )
        out = bs.analyze(data)
        self.assertEqual(
            out["reviews_lines"],
            ["- PR #7 — commented by @ari — 'looks good'"])

    def test_board_lines_skip_empty_fields(self):
        data = _analyze_data(
            board={"work": [
                {"branch": "feat", "issue": "", "status": "Open"},
                {"branch": "x", "status": ""},
                {"branch": "y", "issue": "—", "status": "Open"},
            ]},
        )
        out = bs.analyze(data)
        self.assertEqual(out["board_lines"], [
            "- feat — Open",
            "- x",
            "- y — Open",
        ])
        self.assertEqual(out["board"][0]["branch"], "feat")

    def test_excerpt_strips_html_comments(self):
        self.assertEqual(
            bs._excerpt("<!-- ai:x --> ## Title body here"), "## Title body here"
        )


if __name__ == "__main__":
    unittest.main()
