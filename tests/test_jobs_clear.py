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


if __name__ == "__main__":
    unittest.main()
