import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import buttons.server as server


class TestJobsClearAndDelete(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.jobs_path = os.path.join(self.tmpdir.name, "jobs.json")
        self.ledger_path = os.path.join(self.tmpdir.name, "ledger.jsonl")
        self.patch_jobs_path = patch("buttons.server.JOBS_PATH", self.jobs_path)
        self.patch_ledger_path = patch("buttons.server.LEDGER_PATH", self.ledger_path)
        self.patch_jobs_path.start()
        self.patch_ledger_path.start()

        with server.jobs_lock:
            server.jobs.clear()

    def tearDown(self):
        with server.jobs_lock:
            server.jobs.clear()
        self.patch_jobs_path.stop()
        self.patch_ledger_path.stop()
        self.tmpdir.cleanup()

    def test_clear_terminal_jobs_preserves_active(self):
        with server.jobs_lock:
            server.jobs["job-1"] = {
                "id": "job-1", "state": "terminal", "outcome": "done",
                "goal": "Test 1", "repo": "arii/test", "steps": []
            }
            server.jobs["job-2"] = {
                "id": "job-2", "state": "active",
                "goal": "Test 2", "repo": "arii/test", "steps": []
            }
            server.jobs["job-3"] = {
                "id": "job-3", "state": "terminal", "outcome": "failed",
                "goal": "Test 3", "repo": "arii/test", "steps": []
            }

        # Call POST /buttons/api/jobs/clear
        mock_handler = MagicMock()
        mock_handler.path = "/buttons/api/jobs/clear"
        mock_handler.headers = {"Content-Length": "2"}
        mock_handler.rfile = MagicMock()
        mock_handler.rfile.read.return_value = b"{}"

        sent_json = {}
        sent_code = 200
        def mock_json(obj, code=200):
            nonlocal sent_json, sent_code
            sent_json = obj
            sent_code = code
        mock_handler._json = mock_json

        server.Handler.do_POST(mock_handler)
        self.assertEqual(sent_code, 200)
        self.assertTrue(sent_json.get("ok"))
        self.assertEqual(sent_json.get("cleared"), 2)

        with server.jobs_lock:
            self.assertNotIn("job-1", server.jobs)
            self.assertNotIn("job-3", server.jobs)
            self.assertIn("job-2", server.jobs)

        # Check jobs file was persisted
        self.assertTrue(os.path.exists(self.jobs_path))
        with open(self.jobs_path) as f:
            persisted = json.load(f)
            self.assertIn("job-2", persisted)
            self.assertNotIn("job-1", persisted)

        # Check ledger recorded entry
        self.assertTrue(os.path.exists(self.ledger_path))
        with open(self.ledger_path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
            self.assertTrue(any(l.get("event") == "jobs cleared" and l.get("cleared") == 2 for l in lines))

    def test_delete_individual_job(self):
        with server.jobs_lock:
            server.jobs["job-abc"] = {
                "id": "job-abc", "state": "terminal", "outcome": "declined",
                "goal": "Delete Me", "repo": "arii/test", "steps": []
            }

        mock_handler = MagicMock()
        mock_handler.path = "/buttons/api/jobs/job-abc/delete"
        mock_handler.headers = {"Content-Length": "0"}
        mock_handler.rfile = MagicMock()
        mock_handler.rfile.read.return_value = b""

        sent_json = {}
        sent_code = 200
        def mock_json(obj, code=200):
            nonlocal sent_json, sent_code
            sent_json = obj
            sent_code = code
        mock_handler._json = mock_json

        server.Handler.do_POST(mock_handler)
        self.assertEqual(sent_code, 200)
        self.assertTrue(sent_json.get("ok"))
        self.assertEqual(sent_json.get("deleted"), "job-abc")

        with server.jobs_lock:
            self.assertNotIn("job-abc", server.jobs)

        # Attempting to delete again returns 404
        server.Handler.do_POST(mock_handler)
        self.assertEqual(sent_code, 404)
        self.assertEqual(sent_json.get("error"), "unknown job")

    def test_job_public_includes_step_failure_reason_and_exit_code(self):
        job = {
            "id": "job-fail-1",
            "template": "fix-issues",
            "goal": "Fix issue",
            "repo": "arii/test",
            "state": "terminal",
            "outcome": "failed",
            "created_at": 1000.0,
            "plan": [
                {
                    "label": "Step 1",
                    "state": "failed",
                    "run_id": "r-123",
                    "note": "Fatal error: connection timed out",
                    "exit_code": 1
                },
                {
                    "label": "Step 2",
                    "state": "pending",
                    "run_id": None,
                    "note": None,
                    "exit_code": None
                }
            ]
        }
        pub = server._job_public(job)
        self.assertEqual(len(pub["steps"]), 2)
        self.assertEqual(pub["steps"][0]["state"], "failed")
        self.assertEqual(pub["steps"][0]["note"], "Fatal error: connection timed out")
        self.assertEqual(pub["steps"][0]["exit_code"], 1)
        self.assertIsNone(pub["steps"][1]["note"])
        self.assertIsNone(pub["steps"][1]["exit_code"])

    def test_job_step_failure_reason_extraction(self):
        # Stderr / error trace extraction
        out1 = "$ td preflight\nTraceback (most recent call last):\n  File 'a.py', line 1\nTypeError: bad\nstep failed with exit code 1; aborting"
        reason1 = server._job_step_failure_reason(out1, 1)
        self.assertIn("Traceback", reason1)

        # Fallback to exit code
        out2 = ""
        reason2 = server._job_step_failure_reason(out2, 2)
        self.assertEqual(reason2, "exit 2")

    def test_html_ui_has_inline_failure_reasons_collapse_and_cap_20(self):
        html = server.HTML
        # 1. Inline failure reason render path
        self.assertIn("jobstep-reason", html)
        self.assertIn("s.state === \"failed\"", html)
        self.assertIn("reasonEl.textContent = reasonText", html)

        # 2. Terminal collapse logic (terminal older than newest terminal collapsed)
        self.assertIn("newestTerminalIdx", html)
        self.assertIn("isOlderTerminal = j.state === \"terminal\" && idx > newestTerminalIdx", html)
        self.assertIn("jobcard-expander", html)
        self.assertIn("Show steps", html)

        # 3. Cap at 20 with show more
        self.assertIn("jobsShowAll", html)
        self.assertIn("limit = jobsShowAll ? jobs.length : 20", html)
        self.assertIn("jobsShowMoreBtn", html)
        self.assertIn("Show more (", html)


if __name__ == "__main__":
    unittest.main()
