#!/usr/bin/env python3
import json
import unittest
from unittest.mock import patch, MagicMock

import buttons.server as bs
import buttons.briefing_summary as bs_summary


class TestExecutablePlans(unittest.TestCase):
    def setUp(self):
        bs.jobs.clear()

    def test_briefing_summary_marker_generation(self):
        fake_data = {
            "prs": None, "prs_raw": "", "prs_err": "",
            "pr_list": [{"number": 123, "title": "Fix bug", "headRefName": "bugfix", "updatedAt": "2026-11-01T00:00:00Z"}],
            "gh": {123: {"statusCheckRollup": [{"conclusion": "FAILURE", "name": "test"}], "mergeStateStatus": "DIRTY"}},
            "board": None, "board_err": "",
            "conf": {"123": ["file.py"]}, "conf_err": "",
            "reviews": {}, "reviews_err": {}, "reviews_raw_parts": [],
            "runs": None, "runs_err": "", "jobs": {},
            "jules": [], "jules_err": "", "jules_raw": "",
            "prs_raw": "", "board_raw": "", "conf_raw": "", "runs_raw": "",
        }
        res = bs_summary.analyze(fake_data)
        self.assertIn("attention_with_markers", res)
        markers = dict(res["attention_with_markers"])
        # Check that conflicted has update marker
        conflict_keys = [k for k in markers if "conflicted" in k]
        self.assertTrue(any("action:update pr:123" in markers[k] for k in conflict_keys))

    def test_parse_plan_markdown(self):
        plan_text = """
# Plan
## Next actions
- [ ] Fix CI on PR #101 <!-- action:fix-ci pr:101 -->
- [ ] Update branch for PR #102 <!-- action:update pr:102 -->
- [ ] Re-run the briefing after acting <!-- action:briefing-td -->
- [ ] Manual review note
"""
        steps = bs._parse_plan_markdown(plan_text, "tech-dancer")
        self.assertEqual(len(steps), 4)
        self.assertEqual(steps[0]["kind"], "pr-action")
        self.assertEqual(steps[0]["action"], "fix-ci")
        self.assertEqual(steps[0]["pr"], 101)
        self.assertTrue(steps[0]["write"])

        self.assertEqual(steps[1]["kind"], "pr-action")
        self.assertEqual(steps[1]["action"], "update")
        self.assertEqual(steps[1]["pr"], 102)
        self.assertTrue(steps[1]["write"])

        self.assertEqual(steps[2]["kind"], "action")
        self.assertEqual(steps[2]["action"], "briefing-td")
        self.assertFalse(steps[2]["write"])

        self.assertEqual(steps[3]["kind"], "info")
        self.assertFalse(steps[3]["write"])

    @patch("buttons.server._discover_repos")
    @patch("buttons.server._start_run")
    def test_plan_with_only_reads_autocompletes(self, mock_start_run, mock_discover):
        mock_discover.return_value = {"remote-server": {"path": "/srv/remote-server", "gh": "arii/remote-server"}}
        plan_text = """
- [ ] Read step 1 <!-- action:info -->
- [ ] Read step 2 <!-- action:info -->
"""
        job, err = bs._job_create_from_plan(plan_text, "remote-server")
        self.assertIsNone(err)
        self.assertIsNotNone(job)
        # Informational read steps complete automatically
        self.assertEqual(job["state"], "terminal")
        self.assertEqual(job["outcome"], "done")
        self.assertEqual(job["plan"][0]["state"], "done")
        self.assertEqual(job["plan"][1]["state"], "done")

    @patch("buttons.server._discover_repos")
    @patch("buttons.server._pr_action")
    def test_plan_consolidated_approval_and_non_blocking_decline(self, mock_pr_action, mock_discover):
        mock_discover.return_value = {"remote-server": {"path": "/srv/remote-server", "gh": "arii/remote-server"}}
        mock_pr_action.return_value = {"ok": True}
        plan_text = """
- [ ] Fix CI on PR #1 <!-- action:fix-ci pr:1 -->
- [ ] Fix CI on PR #2 <!-- action:fix-ci pr:2 -->
- [ ] Refresh briefing <!-- action:info -->
"""
        job, err = bs._job_create_from_plan(plan_text, "remote-server")
        self.assertIsNone(err)
        self.assertEqual(job["state"], "active")
        self.assertEqual(job["plan"][0]["state"], "pending")
        self.assertEqual(job["plan"][1]["state"], "pending")

        # Decline the first write step
        # Simulating sub == "decline" for step 0
        with bs.jobs_lock:
            st0 = job["plan"][0]
            st0["state"] = "declined"
            bs._job_after_step(job)
        bs._job_advance(job)

        # Job is still active because step 1 and step 2 remain!
        self.assertEqual(job["state"], "active")
        self.assertEqual(job["plan"][0]["state"], "declined")

        # Approve remaining write step
        with bs.jobs_lock:
            st1 = job["plan"][1]
            st1["state"] = "approved"
        ok, _ = bs._execute_job_step(job, 1, st1)
        self.assertTrue(ok)
        self.assertEqual(st1["state"], "done")

        bs._job_advance(job)
        # Step 2 (info) auto-completes and job finishes as done
        self.assertEqual(job["plan"][2]["state"], "done")
        self.assertEqual(job["state"], "terminal")
        self.assertEqual(job["outcome"], "done")


if __name__ == "__main__":
    unittest.main()
