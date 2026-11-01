#!/usr/bin/env python3
"""Tests for buttons audit/CI plans viewer and 1-tap plan execution."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import MagicMock, patch

import buttons.server as server


class TestButtonsPlansViewer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_dir = tempfile.mkdtemp(prefix="test_plans_")
        cls.plans_dir = os.path.join(cls.test_dir, ".agents", "plans")
        os.makedirs(cls.plans_dir, exist_ok=True)
        os.environ["BUTTONS_PLANS_DIR"] = cls.plans_dir

        cls.orig_repos_dir = server.REPOS_DIR
        server.REPOS_DIR = cls.test_dir

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.REPOS_DIR = cls.orig_repos_dir
        if "BUTTONS_PLANS_DIR" in os.environ:
            del os.environ["BUTTONS_PLANS_DIR"]
        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        with server.plan_execution_lock:
            server.plan_execution_state.clear()
            server.plan_execution_state.update({
                "status": "idle",
                "plan": None,
                "repo": None,
                "timestamp": None,
                "log_buffer": "",
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

    def test_ui_contains_audit_plans_drawer(self):
        status, html = self._get("/buttons/")
        self.assertEqual(status, 200)
        self.assertIn("Audit Plans &amp; Reports", html)
        self.assertIn('id="auditPlansDrawer"', html)
        self.assertIn('id="plansListContainer"', html)
        self.assertIn('id="planViewerCard"', html)
        self.assertIn('id="executePlanBtn"', html)
        self.assertIn('id="planExecStatusBadge"', html)
        self.assertIn('id="planMarkdownContainer"', html)

    def test_list_plans_empty_dir(self):
        status, data = self._get("/buttons/api/plans?repo=remote-server")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("repo"), "remote-server")
        self.assertIsInstance(data.get("plans"), list)

    def test_list_and_read_plan_content(self):
        repo_name = "remote-server"
        plan_filename = "2026-11-01-briefing-plan.md"
        plan_path = os.path.join(self.plans_dir, plan_filename)
        plan_content = (
            "# Audit and CI Execution Plan\n\n"
            "Execution plan generated from automated briefing.\n\n"
            "## Checklist Tasks\n"
            "- [ ] Fix failing CI in worker test\n"
            "- [x] Warm repository context\n"
            "- [ ] Verify syntax and type safety\n"
        )
        try:
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write(plan_content)

            # 1. Test listing
            status, data = self._get(f"/buttons/api/plans?repo={repo_name}")
            self.assertEqual(status, 200)
            plans = data.get("plans", [])
            found = [p for p in plans if p["filename"] == plan_filename]
            self.assertTrue(found, f"Plan {plan_filename} not found in plans list: {plans}")
            item = found[0]
            self.assertEqual(item["title"], "Audit and CI Execution Plan")
            self.assertEqual(item["total_tasks"], 3)
            self.assertEqual(item["completed_tasks"], 1)

            # 2. Test reading valid plan
            status, data = self._get(f"/buttons/api/plan?repo={repo_name}&plan={plan_filename}")
            self.assertEqual(status, 200)
            plan_obj = data.get("plan", {})
            self.assertEqual(plan_obj.get("filename"), plan_filename)
            self.assertEqual(plan_obj.get("title"), "Audit and CI Execution Plan")
            self.assertIn("# Audit and CI Execution Plan", plan_obj.get("content", ""))
            self.assertEqual(plan_obj.get("total_tasks"), 3)
            self.assertEqual(plan_obj.get("completed_tasks"), 1)

        finally:
            if os.path.exists(plan_path):
                os.remove(plan_path)

    def test_read_plan_validation_and_path_traversal(self):
        # Invalid plan names
        status, data = self._get("/buttons/api/plan?repo=remote-server&plan=../etc/passwd")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

        status, data = self._get("/buttons/api/plan?repo=remote-server&plan=malicious.sh")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

        # Non-existent plan
        status, data = self._get("/buttons/api/plan?repo=remote-server&plan=nonexistent-plan.md")
        self.assertEqual(status, 404)
        self.assertIn("error", data)

        # Invalid repo
        status, data = self._get("/buttons/api/plan?repo=unknown-repo-xyz&plan=test.md")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    @patch("buttons.server._trigger_plan_execution")
    def test_execute_plan_success(self, mock_trigger):
        mock_trigger.return_value = None
        repo_name = "remote-server"
        plan_filename = "test-exec-plan.md"
        plan_path = os.path.join(self.plans_dir, plan_filename)
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write("# Test Executable Plan\n- [ ] Task 1\n")

        try:
            status, data = self._post("/buttons/api/plan/execute", {
                "repo": repo_name,
                "plan": plan_filename
            })
            self.assertEqual(status, 200)
            self.assertTrue(data.get("ok"))
            self.assertEqual(data.get("plan"), plan_filename)
            self.assertEqual(data.get("status"), "running")
            mock_trigger.assert_called_once()
        finally:
            if os.path.exists(plan_path):
                os.remove(plan_path)

    def test_execute_plan_validation_failures(self):
        # Unknown repo
        status, data = self._post("/buttons/api/plan/execute", {
            "repo": "nonexistent-repo-999",
            "plan": "plan.md"
        })
        self.assertEqual(status, 400)
        self.assertIn("error", data)

        # Invalid plan name
        status, data = self._post("/buttons/api/plan/execute", {
            "repo": "remote-server",
            "plan": "../evil.md"
        })
        self.assertEqual(status, 400)
        self.assertIn("error", data)

        # Missing plan file
        status, data = self._post("/buttons/api/plan/execute", {
            "repo": "remote-server",
            "plan": "does-not-exist.md"
        })
        self.assertEqual(status, 404)
        self.assertIn("error", data)

    def test_plan_status_endpoint(self):
        with server.plan_execution_lock:
            server.plan_execution_state.update({
                "status": "running",
                "plan": "2026-11-01-briefing-plan.md",
                "repo": "remote-server",
                "timestamp": 123456789.0,
                "log_buffer": "Executing plan tasks...\n",
                "exit_code": None,
            })

        status, data = self._get("/buttons/api/plan/status")
        self.assertEqual(status, 200)
        self.assertEqual(data.get("status"), "running")
        self.assertEqual(data.get("plan"), "2026-11-01-briefing-plan.md")
        self.assertEqual(data.get("repo"), "remote-server")
        self.assertIn("Executing plan tasks...", data.get("log_buffer"))

    def test_plan_execution_thread_ledger_logging(self):
        repos = server._discover_repos()
        repo_name = "remote-server"
        plan_filename = "ledger-test-plan.md"
        plan_path = os.path.join(self.plans_dir, plan_filename)
        with open(plan_path, "w", encoding="utf-8") as f:
            f.write("# Ledger Plan\n- [ ] Task\n")

        with patch("subprocess.Popen") as mock_popen, \
             patch("buttons.server._ledger_append") as mock_ledger:
            mock_proc = MagicMock()
            mock_proc.stdout = ["Step 1 completed\n"]
            mock_proc.wait.return_value = 0
            mock_popen.return_value = mock_proc

            server._plan_execution_thread(repo_name, plan_filename, repos)

            with server.plan_execution_lock:
                self.assertEqual(server.plan_execution_state["status"], "done")
                self.assertEqual(server.plan_execution_state["exit_code"], 0)
                self.assertIn("Step 1 completed", server.plan_execution_state["log_buffer"])

            # Verify ledger logging occurred for start and finish
            self.assertTrue(mock_ledger.call_count >= 2)
            calls = [c[0][0] for c in mock_ledger.call_args_list]
            actions = [c.get("action") for c in calls]
            self.assertIn("execute-plan", actions)

        if os.path.exists(plan_path):
            os.remove(plan_path)


if __name__ == "__main__":
    unittest.main()
