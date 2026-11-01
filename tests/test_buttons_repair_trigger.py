#!/usr/bin/env python3
"""Tests for buttons repair trigger dispatch and issue webhook handler."""

import hashlib
import hmac
import http.client
import json
import os
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import buttons.server as server


class TestButtonsRepairTrigger(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["WEBHOOK_SECRET"] = "test-secret-123"
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        with server.repair_state_lock:
            server.repair_state.clear()
            server.repair_state.update({
                "status": "idle",
                "timestamp": None,
                "log_buffer": "",
                "issue": None,
                "exit_code": None,
            })

    def _post(self, path, payload, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps(payload).encode("utf-8") if isinstance(payload, (dict, list)) else (payload or b"")
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        conn.request("POST", path, body=body, headers=req_headers)
        resp = conn.getresponse()
        resp_data = resp.read().decode("utf-8")
        conn.close()
        try:
            parsed = json.loads(resp_data)
        except Exception:
            parsed = resp_data
        return resp.status, parsed

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

    def test_parse_issue_number(self):
        self.assertEqual(server._parse_issue_number(42), 42)
        self.assertEqual(server._parse_issue_number("42"), 42)
        self.assertEqual(server._parse_issue_number("#42"), 42)
        self.assertEqual(server._parse_issue_number(" #101 "), 101)
        self.assertEqual(server._parse_issue_number("https://github.com/arii/remote-server/issues/88"), 88)
        self.assertEqual(server._parse_issue_number("https://github.com/owner/repo/issues/123/"), 123)
        self.assertEqual(server._parse_issue_number("https://github.com/owner/repo/issues/123?param=val"), 123)
        self.assertEqual(server._parse_issue_number("https://github.com/owner/repo/issues/123#issuecomment-456"), 123)

        # Invalid formats
        self.assertIsNone(server._parse_issue_number(None))
        self.assertIsNone(server._parse_issue_number(True))
        self.assertIsNone(server._parse_issue_number(False))
        self.assertIsNone(server._parse_issue_number(0))
        self.assertIsNone(server._parse_issue_number(-1))
        self.assertIsNone(server._parse_issue_number("not-a-number"))
        self.assertIsNone(server._parse_issue_number("https://github.com/owner/repo/pull/42"))
        self.assertIsNone(server._parse_issue_number(""))

    @patch("buttons.server._trigger_repair")
    def test_dispatch_endpoint_valid_integer(self, mock_trigger):
        mock_trigger.return_value = None
        status, data = self._post("/buttons/api/repair/dispatch", {"issue": 42})
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("issue"), 42)
        self.assertEqual(data.get("status"), "running")
        mock_trigger.assert_called_once_with(42)

    @patch("buttons.server._trigger_repair")
    def test_dispatch_endpoint_valid_url(self, mock_trigger):
        mock_trigger.return_value = None
        status, data = self._post("/buttons/api/repair/dispatch", {
            "issue": "https://github.com/arii/remote-server/issues/99"
        })
        self.assertEqual(status, 200)
        self.assertTrue(data.get("ok"))
        self.assertEqual(data.get("issue"), 99)
        mock_trigger.assert_called_once_with(99)

    def test_dispatch_endpoint_validation_failure(self):
        # Missing issue
        status, data = self._post("/buttons/api/repair/dispatch", {})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

        # Non-numeric string
        status, data = self._post("/buttons/api/repair/dispatch", {"issue": "invalid-issue"})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

        # Zero or negative
        status, data = self._post("/buttons/api/repair/dispatch", {"issue": 0})
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    def test_repair_status_endpoints(self):
        with server.repair_state_lock:
            server.repair_state.update({
                "status": "running",
                "timestamp": 123456789.0,
                "log_buffer": "Running worker...\n",
                "issue": 55,
                "exit_code": None,
            })

        # Dedicated repair status endpoint
        status, data = self._get("/buttons/api/repair/status")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "running")
        self.assertEqual(data.get("issue"), 55)
        self.assertIn("Running worker...", data.get("log_buffer"))

        # Main status endpoint includes repair
        status, data = self._get("/buttons/api/status")
        self.assertEqual(status, 200)
        self.assertIn("repair", data)
        self.assertEqual(data["repair"].get("status"), "running")
        self.assertEqual(data["repair"].get("issue"), 55)

    @patch("buttons.server._trigger_repair")
    def test_webhook_labeled_agent_repair_triggers_worker(self, mock_trigger):
        mock_trigger.return_value = None
        payload = {
            "action": "labeled",
            "issue": {"number": 105, "title": "CI failure in worker"},
            "label": {"name": "agent:repair"},
            "repository": {"full_name": "arii/remote-server"},
        }
        body = json.dumps(payload).encode("utf-8")
        secret = os.environ.get("WEBHOOK_SECRET", "")
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        status, data = self._post(
            server.WEBHOOK_PATH,
            body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issues",
            },
        )

        self.assertEqual(status, 202)
        self.assertTrue(data.get("queued"))
        self.assertTrue(data.get("repair"))
        self.assertEqual(data.get("issue"), 105)
        mock_trigger.assert_called_once_with(105)

    @patch("buttons.server._trigger_repair")
    def test_webhook_labeled_auto_repair_triggers_worker(self, mock_trigger):
        mock_trigger.return_value = None
        payload = {
            "action": "labeled",
            "issue": {"number": 204},
            "label": {"name": "auto-repair"},
            "repository": {"full_name": "arii/remote-server"},
        }
        body = json.dumps(payload).encode("utf-8")
        secret = os.environ.get("WEBHOOK_SECRET", "")
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        status, data = self._post(
            server.WEBHOOK_PATH,
            body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issues",
            },
        )

        self.assertEqual(status, 202)
        self.assertTrue(data.get("queued"))
        self.assertTrue(data.get("repair"))
        self.assertEqual(data.get("issue"), 204)
        mock_trigger.assert_called_once_with(204)

    @patch("buttons.server._trigger_repair")
    def test_webhook_other_labels_ignored(self, mock_trigger):
        payload = {
            "action": "labeled",
            "issue": {"number": 303},
            "label": {"name": "documentation"},
            "repository": {"full_name": "arii/remote-server"},
        }
        body = json.dumps(payload).encode("utf-8")
        secret = os.environ.get("WEBHOOK_SECRET", "")
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

        status, data = self._post(
            server.WEBHOOK_PATH,
            body,
            headers={
                "X-Hub-Signature-256": sig,
                "X-GitHub-Event": "issues",
            },
        )

        self.assertEqual(status, 200)
        self.assertIn("ignored", data)
        mock_trigger.assert_not_called()

    def test_ui_contains_repair_card(self):
        status, html = self._get("/buttons/")
        self.assertEqual(status, 200)
        self.assertIn("Agent / Tools: Local Repair Worker", html)
        self.assertIn('id="repairIssueInput"', html)
        self.assertIn('id="dispatchRepairBtn"', html)
        self.assertIn('id="repairStatusText"', html)
        self.assertIn('id="repairLog"', html)

    @patch("buttons.server._trigger_repair")
    def test_fix_issues_job_executes_repair_worker(self, mock_trigger):
        mock_trigger.return_value = None
        # Verify job creation for fix-issues creates repair-issue steps
        job, err = server._job_create("fix-issues", "remote-server", issues=[12, 34])
        self.assertIsNone(err)
        self.assertIsNotNone(job)
        self.assertEqual(len(job["plan"]), 2)
        self.assertEqual(job["plan"][0]["kind"], "repair-issue")
        self.assertEqual(job["plan"][0]["issue"], 12)
        self.assertIn("Dispatch repair worker", job["plan"][0]["label"])

        # Execute the synchronous job step
        ok, err = server._execute_job_step(job, 0, job["plan"][0])
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(job["plan"][0]["state"], "done")
        mock_trigger.assert_called_once_with(12)

    def test_ui_attention_item_repair_button(self):
        status, html = self._get("/buttons/")
        self.assertEqual(status, 200)
        # Check that briefing rendering code includes the Repair button logic for attention items
        self.assertIn('btn.textContent = "Repair"', html)
        self.assertIn('/buttons/api/repair/dispatch', html)

    def test_ui_fix_issues_copy_and_expandable_titles(self):
        status, html = self._get("/buttons/")
        self.assertEqual(status, 200)
        # Verify updated copy describes checkboxes and Start job
        self.assertIn('Pick issues with checkboxes and tap Start job to dispatch', html)
        self.assertIn('the autonomous repair worker sequentially.', html)
        self.assertNotIn('each gets one Approve tap', html)

        # Verify expandable issue title DOM elements and tooltips exist in HTML script
        self.assertIn('lab.className = "issuepick-label";', html)
        self.assertIn('lab.title = fullTitle;', html)
        self.assertIn('titleSpan.className = "issuepick-title compact";', html)
        self.assertIn('expBtn.className = "issuepick-expand";', html)
        self.assertIn('.issuepick-expand', html)


if __name__ == "__main__":
    unittest.main()
