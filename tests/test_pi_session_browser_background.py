import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import buttons.server as server


class TestPiSessionBrowserBackground(unittest.TestCase):
    def setUp(self):
        # Clear pi_runs before each test
        with server.pi_runs_lock:
            server.pi_runs.clear()

    def tearDown(self):
        with server.pi_runs_lock:
            server.pi_runs.clear()

    def test_background_runs_merged_in_pi_sessions(self):
        # Insert a background run
        rid = "1234567890ab"
        now = time.time()
        with server.pi_runs_lock:
            server.pi_runs[rid] = {
                "id": rid,
                "kind": "ask",
                "repo": "remote-server",
                "done": True,
                "started_at": now - 10,
                "finished_at": now,
                "instruction": "How does server work?",
                "result": {"repo": "remote-server", "reply": "It serves HTTP requests."},
                "error": None
            }

        # Mock Handler and subprocess
        mock_handler = MagicMock()
        mock_handler.path = "/buttons/api/pi-sessions"
        sent_json = {}
        def mock_json(obj, code=200):
            nonlocal sent_json
            sent_json = obj
        mock_handler._json = mock_json

        with patch("subprocess.run") as mock_proc:
            mock_proc.return_value = MagicMock(returncode=0, stdout=json.dumps({"sessions": []}))
            server.Handler.do_GET(mock_handler)

        sessions = sent_json.get("sessions", [])
        self.assertEqual(len(sessions), 1)
        bg = sessions[0]
        self.assertEqual(bg["file"], f"bg-{rid}.jsonl")
        self.assertEqual(bg["repo"], "remote-server")
        self.assertEqual(bg["title"], "How does server work?")
        self.assertEqual(bg["kind"], "ask")
        self.assertEqual(bg["source"], "background")

    def test_background_run_detail_in_pi_session(self):
        rid = "abcdef123456"
        now = time.time()
        with server.pi_runs_lock:
            server.pi_runs[rid] = {
                "id": rid,
                "kind": "ask",
                "repo": "remote-server",
                "done": True,
                "started_at": now - 10,
                "finished_at": now,
                "instruction": "Diagnose failing test",
                "result": {"repo": "remote-server", "reply": "Test failed due to timeout."},
                "error": None
            }

        mock_handler = MagicMock()
        mock_handler.path = f"/buttons/api/pi-session?session=bg-{rid}.jsonl"
        sent_json = {}
        def mock_json(obj, code=200):
            nonlocal sent_json
            sent_json = obj
        mock_handler._json = mock_json

        server.Handler.do_GET(mock_handler)
        self.assertEqual(sent_json.get("repo"), "remote-server")
        self.assertEqual(sent_json.get("source"), "background")
        turns = sent_json.get("turns", [])
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["role"], "user")
        self.assertEqual(turns[0]["text"], "Diagnose failing test")
        self.assertEqual(turns[1]["role"], "assistant")
        self.assertIn("Test failed due to timeout.", turns[1]["text"])

    def test_background_run_delete(self):
        rid = "fedcba654321"
        with server.pi_runs_lock:
            server.pi_runs[rid] = {
                "id": rid,
                "kind": "suggest",
                "repo": "remote-server",
                "done": True,
                "started_at": time.time(),
                "finished_at": time.time(),
                "instruction": "Suggest next actions",
                "result": None,
                "error": None
            }

        mock_handler = MagicMock()
        mock_handler.path = "/buttons/api/pi-sessions/delete"
        mock_handler.headers = {"Content-Length": "100"}
        body = json.dumps({"session": f"bg-{rid}.jsonl"}).encode("utf-8")
        mock_handler.rfile = MagicMock()
        mock_handler.rfile.read.return_value = body

        sent_json = {}
        def mock_json(obj, code=200):
            nonlocal sent_json
            sent_json = obj
        mock_handler._json = mock_json

        server.Handler.do_POST(mock_handler)
        self.assertTrue(sent_json.get("ok"))
        with server.pi_runs_lock:
            self.assertNotIn(rid, server.pi_runs)


if __name__ == "__main__":
    unittest.main()
