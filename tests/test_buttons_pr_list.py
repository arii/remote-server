#!/usr/bin/env python3
"""Tests for buttons PR list display and PR quick actions."""

import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import buttons.server as server


class TestButtonsPRList(unittest.TestCase):
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

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("GET", path)
        resp = conn.getresponse()
        resp_data = resp.read().decode("utf-8")
        conn.close()
        try:
            parsed = json.loads(resp_data)
        except Exception:
            parsed = resp_data
        return resp.status, parsed

    def _post(self, path, payload):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps(payload).encode("utf-8") if isinstance(payload, (dict, list)) else (payload or b"")
        req_headers = {"Content-Type": "application/json"}
        conn.request("POST", path, body=body, headers=req_headers)
        resp = conn.getresponse()
        resp_data = resp.read().decode("utf-8")
        conn.close()
        try:
            parsed = json.loads(resp_data)
        except Exception:
            parsed = resp_data
        return resp.status, parsed

    def test_ci_status_formatting(self):
        # Passing rollup
        passing_rollup = [{"status": "COMPLETED", "conclusion": "SUCCESS"}]
        self.assertEqual(server._format_ci_status(passing_rollup), "Passing")

        # Failing rollup
        failing_rollup = [{"status": "COMPLETED", "conclusion": "FAILURE"}]
        self.assertEqual(server._format_ci_status(failing_rollup), "Failing")

        # Running / In-progress rollup
        running_rollup = [{"status": "IN_PROGRESS", "conclusion": None}]
        self.assertEqual(server._format_ci_status(running_rollup), "Running")

        # Empty / no checks
        self.assertEqual(server._format_ci_status([]), "No checks")
        self.assertEqual(server._format_ci_status(None), "Unknown")

    def test_review_decision_formatting(self):
        self.assertEqual(server._format_review_decision("APPROVED"), "APPROVED")
        self.assertEqual(server._format_review_decision("CHANGES_REQUESTED"), "CHANGES_REQUESTED")
        self.assertEqual(server._format_review_decision("REVIEW_REQUIRED"), "PENDING")
        self.assertEqual(server._format_review_decision(""), "PENDING")
        self.assertEqual(server._format_review_decision(None), "PENDING")

    @patch("buttons.server._td_run")
    def test_get_prs_endpoint_success(self, mock_td_run):
        mock_raw_prs = [
            {
                "number": 101,
                "title": "Fix memory leak in websocket listener",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "reviewDecision": "APPROVED",
                "headRefName": "fix/websocket-leak",
                "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
                "url": "https://github.com/arii/remote-server/pull/101",
                "author": {"login": "octocat"},
            },
            {
                "number": 102,
                "title": "WIP: New streaming chat endpoint",
                "isDraft": True,
                "mergeable": "CONFLICTING",
                "reviewDecision": "CHANGES_REQUESTED",
                "headRefName": "feat/streaming-chat",
                "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
                "url": "https://github.com/arii/remote-server/pull/102",
                "author": "collaborator",
            },
            {
                "number": 103,
                "title": "Update dependencies",
                "isDraft": False,
                "mergeable": "UNKNOWN",
                "reviewDecision": "REVIEW_REQUIRED",
                "headRefName": "chore/deps",
                "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": None}],
                "url": "https://github.com/arii/remote-server/pull/103",
                "author": None,
            },
        ]
        mock_td_run.return_value = json.dumps(mock_raw_prs)

        status, data = self._get("/buttons/api/prs?repo=remote-server")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("repo"), "remote-server")
        prs = data.get("prs", [])
        self.assertEqual(len(prs), 3)

        # Check PR #101
        pr1 = prs[0]
        self.assertEqual(pr1["number"], 101)
        self.assertEqual(pr1["title"], "Fix memory leak in websocket listener")
        self.assertEqual(pr1["author"], "octocat")
        self.assertEqual(pr1["branch"], "fix/websocket-leak")
        self.assertFalse(pr1["isDraft"])
        self.assertEqual(pr1["ciStatus"], "Passing")
        self.assertEqual(pr1["reviewDecision"], "APPROVED")
        self.assertEqual(pr1["mergeable"], "MERGEABLE")
        self.assertEqual(pr1["url"], "https://github.com/arii/remote-server/pull/101")

        # Check PR #102
        pr2 = prs[1]
        self.assertEqual(pr2["number"], 102)
        self.assertTrue(pr2["isDraft"])
        self.assertEqual(pr2["ciStatus"], "Failing")
        self.assertEqual(pr2["reviewDecision"], "CHANGES_REQUESTED")

        # Check PR #103
        pr3 = prs[2]
        self.assertEqual(pr3["number"], 103)
        self.assertEqual(pr3["ciStatus"], "Running")
        self.assertEqual(pr3["reviewDecision"], "PENDING")

    def test_get_prs_endpoint_unknown_repo(self):
        status, data = self._get("/buttons/api/prs?repo=nonexistent-repo")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    @patch("buttons.server._pr_view")
    @patch("buttons.server._td_run")
    def test_pr_action_ready_success(self, mock_td_run, mock_pr_view):
        # ci="passing", merge="clean", review="PENDING", draft=True
        mock_pr_view.return_value = ("passing", "clean", "PENDING", True)
        mock_td_run.return_value = "Marked #102 ready for review\n"

        status, data = self._post("/buttons/api/pr-action", {
            "pr_number": 102,
            "action": "ready",
            "repo": "remote-server"
        })
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        mock_td_run.assert_called_once()
        self.assertIn("gh pr ready 102", mock_td_run.call_args[0][0])

    @patch("buttons.server._pr_view")
    def test_pr_action_ready_refused_if_not_draft(self, mock_pr_view):
        mock_pr_view.return_value = ("passing", "clean", "APPROVED", False)

        status, data = self._post("/buttons/api/pr-action", {
            "pr_number": 101,
            "action": "ready",
            "repo": "remote-server"
        })
        self.assertEqual(status, 400)
        self.assertIn("already ready", data.get("error", "").lower())

    @patch("buttons.server._pr_view")
    @patch("buttons.server._td_run")
    def test_pr_action_merge_success(self, mock_td_run, mock_pr_view):
        # ci="passing", merge="clean", review="APPROVED", draft=False
        mock_pr_view.return_value = ("passing", "clean", "APPROVED", False)
        mock_td_run.return_value = "Squashed and merged #101\n"

        status, data = self._post("/buttons/api/pr-action", {
            "pr_number": 101,
            "action": "merge",
            "repo": "remote-server"
        })
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        mock_td_run.assert_called_once()
        self.assertIn("gh pr merge 101 --squash --delete-branch", mock_td_run.call_args[0][0])

    @patch("buttons.server._pr_view")
    def test_pr_action_merge_refused_not_approved(self, mock_pr_view):
        # ci="passing", merge="clean", review="PENDING", draft=False
        mock_pr_view.return_value = ("passing", "clean", "PENDING", False)

        status, data = self._post("/buttons/api/pr-action", {
            "pr_number": 101,
            "action": "merge",
            "repo": "remote-server"
        })
        self.assertEqual(status, 400)
        self.assertIn("must be approved", data.get("error", "").lower())

    @patch("buttons.server._pr_view")
    def test_pr_action_merge_refused_draft(self, mock_pr_view):
        mock_pr_view.return_value = ("passing", "clean", "APPROVED", True)

        status, data = self._post("/buttons/api/pr-action", {
            "pr_number": 101,
            "action": "merge",
            "repo": "remote-server"
        })
        self.assertEqual(status, 400)
        self.assertIn("draft", data.get("error", "").lower())

    @patch("buttons.server._pr_view")
    def test_pr_action_merge_refused_ci_failing(self, mock_pr_view):
        mock_pr_view.return_value = ("failing", "clean", "APPROVED", False)

        status, data = self._post("/buttons/api/pr-action", {
            "pr_number": 101,
            "action": "merge",
            "repo": "remote-server"
        })
        self.assertEqual(status, 400)
        self.assertIn("ci", data.get("error", "").lower())

    def test_ui_contains_pull_requests_tab_and_components(self):
        status, html = self._get("/buttons/")
        self.assertEqual(status, 200)
        # Tab bar item
        self.assertIn('data-tab="prs"', html)
        self.assertIn("Pull Requests", html)
        # Tabpanel section
        self.assertIn('id="tab-prs"', html)
        self.assertIn('id="prsListContainer"', html)
        self.assertIn('id="refreshPrsBtn"', html)
        self.assertIn('id="prsErr"', html)
        # Client JS hooks
        self.assertIn("refreshPullRequests", html)
        self.assertIn("renderPullRequests", html)
        self.assertIn("Squash & Merge", html)
        self.assertIn("Mark Ready", html)


if __name__ == "__main__":
    unittest.main()
