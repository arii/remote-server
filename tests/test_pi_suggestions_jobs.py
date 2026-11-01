#!/usr/bin/env python3
import tempfile
import unittest
from unittest.mock import patch

from buttons.server import (
    _parse_suggestions,
    _parse_proposal,
    _extract_suggestion_note,
    _job_create,
    _validate_td_command,
)


class TestPiSuggestionsJobs(unittest.TestCase):
    def test_parse_suggestions_markdown_and_case(self):
        raw = """
Here are recommendations:
* SUGGEST: `td --json preflight` :: check repository health
- suggest: td context-warm :: warm up context cache
1. SUGGEST: td gh read-pr-reviews --pr 101 :: review PR
"""
        suggs = _parse_suggestions(raw)
        self.assertEqual(len(suggs), 3)
        self.assertEqual(suggs[0]["command"], "td --json preflight")
        self.assertEqual(suggs[0]["reason"], "check repository health")
        self.assertEqual(suggs[1]["command"], "td context-warm")
        self.assertEqual(suggs[2]["command"], "td gh read-pr-reviews --pr 101")

    def test_parse_suggestions_rambling_fallback(self):
        raw = """
Asking Pi (local model, may take minutes)…
I checked the logs and everything seems fine.
We might need to check failing CI, but there is no specific td command available.
"""
        suggs = _parse_suggestions(raw)
        self.assertEqual(len(suggs), 0)
        note = _extract_suggestion_note(raw, suggs)
        self.assertIn("I checked the logs and everything seems fine.", note)
        self.assertNotIn("Asking Pi", note)

    def test_parse_proposal_hardened(self):
        raw_td = """
Here is what we should run:
**PROPOSE td**
COMMAND: `td --json agent list-sessions`
**END PROPOSE**
"""
        prop_td = _parse_proposal(raw_td)
        self.assertIsNotNone(prop_td)
        self.assertEqual(prop_td["kind"], "td")
        self.assertEqual(prop_td["command"], "td --json agent list-sessions")

        raw_issue = """
- PROPOSE issue
TITLE: Fix briefing race condition
BODY:
Investigate why briefing times out on slow disks.
- END PROPOSE
"""
        prop_issue = _parse_proposal(raw_issue)
        self.assertIsNotNone(prop_issue)
        self.assertEqual(prop_issue["kind"], "issue")
        self.assertEqual(prop_issue["title"], "Fix briefing race condition")
        self.assertIn("Investigate why briefing times out", prop_issue["body"])

    def test_create_job_from_suggestions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jobs_path = f"{tmpdir}/jobs.json"
            audit_dir = f"{tmpdir}/audits"
            ledger_path = f"{tmpdir}/ledger.jsonl"
            with patch("buttons.server.JOBS_PATH", jobs_path), \
                 patch("buttons.server.JOBS_AUDIT_DIR", audit_dir), \
                 patch("buttons.server.LEDGER_PATH", ledger_path), \
                 patch("buttons.server._discover_repos", return_value={"tech-dancer": {"gh": "arii/tech-dancer", "td_cwd": "/test"}}):
                suggestions = [
                    {"command": "td --json preflight", "reason": "health check"},
                    {"command": "td gh read-pr-comments --pr 5", "reason": "check comments"},
                ]
                job, err = _job_create("pi-suggestions", "tech-dancer", suggestions=suggestions)
                self.assertIsNone(err)
                self.assertIsNotNone(job)
                self.assertEqual(job["template"], "pi-suggestions")
                self.assertEqual(len(job["plan"]), 2)
                self.assertEqual(job["plan"][0]["kind"], "run-td")
                self.assertEqual(job["plan"][0]["td_argv"], ["td", "--json", "preflight"])
                self.assertTrue(job["plan"][0]["write"])
                self.assertEqual(job["plan"][1]["kind"], "run-td")
                self.assertEqual(job["plan"][1]["td_argv"], ["td", "gh", "read-pr-comments", "--pr", "5"])


if __name__ == "__main__":
    unittest.main()
