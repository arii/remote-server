#!/usr/bin/env python3
"""Tests for issue details rendering and issue assignment endpoint."""

import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import buttons.server as server


class TestIssueAssign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(payload).encode("utf-8")
        conn.request("POST", path, data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        conn.close()
        try:
            return resp.status, json.loads(body)
        except Exception:
            return resp.status, body

    @patch("buttons.server._discover_repos")
    @patch("buttons.server._td_run")
    def test_assign_issue_success(self, mock_td_run, mock_discover_repos):
        mock_discover_repos.return_value = {
            "remote-server": {
                "gh": "arii/remote-server",
                "td_cwd": "/srv/remote-server",
                "host_path": "/home/ari/remote-server",
            }
        }
        mock_td_run.return_value = ""

        status, data = self._post("/buttons/api/assign-issue", {
            "repo": "remote-server",
            "issue": 42,
            "assignee": "octocat"
        })

        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        mock_td_run.assert_called_once()
        cmd = mock_td_run.call_args[0][0]
        self.assertIn("gh issue edit 42", cmd)
        self.assertIn("--repo arii/remote-server", cmd)
        self.assertIn("--add-assignee octocat", cmd)

    @patch("buttons.server._discover_repos")
    def test_assign_issue_validation_failures(self, mock_discover_repos):
        mock_discover_repos.return_value = {
            "remote-server": {
                "gh": "arii/remote-server",
                "td_cwd": "/srv/remote-server",
                "host_path": "/home/ari/remote-server",
            }
        }

        # Unknown repo
        status, data = self._post("/buttons/api/assign-issue", {
            "repo": "nonexistent-repo",
            "issue": 42,
            "assignee": "octocat"
        })
        self.assertEqual(status, 400)
        self.assertIn("bad repo", data.get("error", ""))

        # Invalid issue number (non-digits)
        status, data = self._post("/buttons/api/assign-issue", {
            "repo": "remote-server",
            "issue": "abc42",
            "assignee": "octocat"
        })
        self.assertEqual(status, 400)

        # Invalid assignee username (contains illegal characters)
        status, data = self._post("/buttons/api/assign-issue", {
            "repo": "remote-server",
            "issue": 42,
            "assignee": "octo;rm -rf /"
        })
        self.assertEqual(status, 400)

        # Empty assignee
        status, data = self._post("/buttons/api/assign-issue", {
            "repo": "remote-server",
            "issue": 42,
            "assignee": ""
        })
        self.assertEqual(status, 400)

    @patch("buttons.server._discover_repos")
    @patch("buttons.server._td_run")
    def test_assign_issue_td_run_failure(self, mock_td_run, mock_discover_repos):
        mock_discover_repos.return_value = {
            "remote-server": {
                "gh": "arii/remote-server",
                "td_cwd": "/srv/remote-server",
                "host_path": "/home/ari/remote-server",
            }
        }
        mock_td_run.side_effect = RuntimeError("user 'ghost' does not exist")

        status, data = self._post("/buttons/api/assign-issue", {
            "repo": "remote-server",
            "issue": 42,
            "assignee": "ghost"
        })

        self.assertEqual(status, 500)
        self.assertIn("user 'ghost' does not exist", data.get("error", ""))

    def test_frontend_issue_rendering_contract(self):
        # Verify buttons/server.py includes label chips, description, assignee chips, and error elements
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn('sum.textContent = "Details / assign"', content)
        self.assertIn('labelsHead.textContent = "Labels:"', content)
        self.assertIn('bodyHead.textContent = "Description:"', content)
        self.assertIn('assignHead.textContent = "Assignee:"', content)
        self.assertIn('cardErr.textContent = "Assign failed: "', content)
        self.assertIn('fetchJson("/buttons/api/assign-issue"', content)


if __name__ == "__main__":
    unittest.main()
