"""Review consumes BoomTick's serialized PrDiffResponse contract (#1319)."""

import json
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from backend import review


class ReviewDiffContract(unittest.TestCase):
    def setUp(self):
        # Generated with dev_tools.models.PrDiffResponse.model_dump_json.
        self.payload = json.loads(
            Path(__file__).with_name("pr_diff_response.json").read_text()
        )

    def test_serialized_diff_reaches_the_review_prompt_and_plan(self):
        with mock.patch.object(review, "_td_json", return_value=self.payload):
            files, diff, truncated = review.fetch_pr_diff("arii/remote-server", 1319)

        self.assertEqual(diff, self.payload["diffText"])
        self.assertTrue(truncated)
        plan = review.build_plan(
            "arii/remote-server", 1319, {"diff_truncated": truncated}, files, diff
        )
        self.assertTrue(plan["diff_truncated"])
        self.assertEqual(len(plan["files"]), 1)
        entry = plan["files"][0]
        self.assertEqual(
            {key: entry[key] for key in ("path", "status", "additions", "deletions")},
            self.payload["files"][0],
        )
        self.assertIn(diff, review._review_prompt(plan, diff, ""))

    def test_empty_diff_payload(self):
        payload = {**self.payload, "files": [], "diffText": "", "isTruncated": False}
        with mock.patch.object(review, "_td_json", return_value=payload):
            self.assertEqual(
                review.fetch_pr_diff("arii/remote-server", 1319), ([], "", False)
            )

    def test_diff_command_failure_is_reported(self):
        response = subprocess.CompletedProcess(
            [], 1, stdout=json.dumps({"status": "error", "message": "PR not found"})
        )
        with mock.patch.object(review.subprocess, "run", return_value=response):
            with self.assertRaisesRegex(review.ReviewError, "PR not found"):
                review.fetch_pr_diff("arii/remote-server", 1319)

    def test_review_heading_uses_plain_punctuation(self):
        body = review.render_markdown(
            "arii/remote-server",
            1319,
            {"title": "Diff contract"},
            [],
            "Summary",
            "Approve",
            "local",
        )
        self.assertTrue(body.startswith("# Review: #1319: Diff contract\n"))


if __name__ == "__main__":
    unittest.main()
