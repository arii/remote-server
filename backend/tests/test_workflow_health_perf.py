"""Tests for workflow-health fan-out bounds + endpoint cache (#432)."""

import json
import threading
import time
import unittest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import ops

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False

import backend.exec.engine as engine


def _runs(*run_ids):
    runs = []
    for rid in run_ids:
        runs.append({"id": rid, "name": "CI", "head_branch": "main",
                     "event": "push", "conclusion": "failure",
                     "html_url": f"https://example.invalid/runs/{rid}"})
    runs.append({"id": 999, "name": "CI", "head_branch": "main",
                 "event": "push", "conclusion": "success",
                 "html_url": "https://example.invalid/runs/999"})
    return json.dumps({"workflow_runs": runs})


def _jobs(job_id, name="test-e2e", failed_steps=("run playwright test",)):
    return json.dumps({"jobs": [
        {"id": job_id, "name": name, "conclusion": "failure",
         "steps": [{"name": s, "conclusion": "failure"}
                   for s in failed_steps]},
        {"id": job_id + 1000, "name": "setup", "conclusion": "success",
         "steps": []},
    ]})


def _annotations(message="boom", path="tests/test_ui.py", line=42):
    return json.dumps([{
        "path": path, "start_line": line,
        "annotation_level": "failure", "message": message,
        "title": "Failure"}])


class FetchParallelTest(unittest.TestCase):
    def setUp(self):
        self._orig = engine.td_run

    def tearDown(self):
        engine.td_run = self._orig

    def test_content_and_order_preserved(self):
        def fake(cmd, timeout=None):
            if "workflow-runs" in cmd:
                return _runs(101, 102)
            if "/actions/runs/101/jobs" in cmd:
                return _jobs(201)
            if "/actions/runs/102/jobs" in cmd:
                return _jobs(202, name="lint", failed_steps=())
            if "/check-runs/201/annotations" in cmd:
                return _annotations()
            if "/check-runs/202/annotations" in cmd:
                return json.dumps([])
            raise AssertionError(f"unexpected cmd: {cmd}")

        engine.td_run = fake
        data = ops.fetch_workflow_health("remote-server",
                                         "arii/remote-server")
        self.assertEqual(data["status"], "failing")
        self.assertEqual(data["total_failing"], 2)
        self.assertEqual([w["id"] for w in data["workflows"]], [101, 102])
        first, second = data["workflows"]
        self.assertEqual(first["diagnosed_cause"],
                         "[tests/test_ui.py:42] boom")
        self.assertEqual(first["jobs"],
                         [{"name": "test-e2e", "conclusion": "failure",
                           "failed_steps": ["run playwright test"]}])
        self.assertEqual(len(first["annotations"]), 1)
        self.assertEqual(second["diagnosed_cause"],
                         "Job 'lint' failed.")
        self.assertEqual(second["annotations"], [])

    def test_jobs_fan_out_overlaps(self):
        arrived = []
        gate = threading.Event()
        lock = threading.Lock()

        def fake(cmd, timeout=None):
            if "workflow-runs" in cmd:
                return _runs(101, 102)
            if "/jobs" in cmd:
                with lock:
                    arrived.append(threading.get_ident())
                    if len(arrived) >= 2:
                        gate.set()
                if not gate.wait(timeout=10):
                    raise AssertionError(
                        "per-run jobs fetches did not overlap")
                return _jobs(201)
            return json.dumps([])

        engine.td_run = fake
        data = ops.fetch_workflow_health("remote-server",
                                         "arii/remote-server")
        self.assertEqual(data["total_failing"], 2)
        self.assertEqual(len(arrived), 2)
        self.assertNotEqual(arrived[0], arrived[1])

    def test_jobs_failure_isolated_per_run(self):
        def fake(cmd, timeout=None):
            if "workflow-runs" in cmd:
                return _runs(101, 102)
            if "/actions/runs/101/jobs" in cmd:
                raise RuntimeError("gh exploded")
            if "/actions/runs/102/jobs" in cmd:
                return _jobs(202, name="lint", failed_steps=())
            return json.dumps([])

        engine.td_run = fake
        data = ops.fetch_workflow_health("remote-server",
                                         "arii/remote-server")
        self.assertEqual(data["total_failing"], 2)
        first, second = data["workflows"]
        self.assertEqual(first["jobs"], [])
        self.assertEqual(first["annotations"], [])
        self.assertEqual(first["diagnosed_cause"], "Workflow failed.")
        self.assertEqual(second["diagnosed_cause"],
                         "Job 'lint' failed.")

    def test_annotation_failure_falls_back_to_job(self):
        def fake(cmd, timeout=None):
            if "workflow-runs" in cmd:
                return _runs(101)
            if "/jobs" in cmd:
                return _jobs(201)
            if "/annotations" in cmd:
                raise RuntimeError("annotations down")
            raise AssertionError(f"unexpected cmd: {cmd}")

        engine.td_run = fake
        data = ops.fetch_workflow_health("remote-server",
                                         "arii/remote-server")
        (flow,) = data["workflows"]
        self.assertEqual(flow["annotations"], [])
        self.assertEqual(
            flow["diagnosed_cause"],
            "Job 'test-e2e' failed: run playwright test.")


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class EndpointCacheTest(unittest.TestCase):
    def setUp(self):
        import backend.core as core

        self._orig_status = core.get_repo_status
        self._orig_fetch = ops.fetch_workflow_health
        self.calls = []
        core.get_repo_status = lambda name: {"repo": name}  # noqa: E731
        ops.fetch_workflow_health = self._fetch
        ops._WORKFLOW_HEALTH_CACHE.clear()
        app = FastAPI()
        app.include_router(ops.router)
        self.client = TestClient(app)

    def _fetch(self, repo, gh):
        self.calls.append((repo, gh))
        return {"repo": repo, "status": "healthy", "total_failing": 0,
                "workflows": []}

    def tearDown(self):
        import backend.core as core

        core.get_repo_status = self._orig_status
        ops.fetch_workflow_health = self._orig_fetch
        ops._WORKFLOW_HEALTH_CACHE.clear()

    def test_repeat_request_served_from_cache(self):
        first = self.client.get("/api/workflow-health",
                                params={"repo": "remote-server"})
        second = self.client.get("/api/workflow-health",
                                 params={"repo": "remote-server"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.calls), 1)

    def test_stale_entry_refetched(self):
        ops._WORKFLOW_HEALTH_CACHE["remote-server"] = (
            time.time() - ops.WORKFLOW_HEALTH_CACHE_TTL - 1,
            {"repo": "remote-server", "status": "stale",
             "total_failing": 0, "workflows": []})
        resp = self.client.get("/api/workflow-health",
                               params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "healthy")
        self.assertEqual(len(self.calls), 1)

    def test_error_not_cached(self):
        ops.fetch_workflow_health = self._fail_once
        resp = self.client.get("/api/workflow-health",
                               params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 500)
        resp = self.client.get("/api/workflow-health",
                               params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        resp = self.client.get("/api/workflow-health",
                               params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(self.calls), 1)

    def _fail_once(self, repo, gh):
        if not getattr(self, "_failed", False):
            self._failed = True
            raise RuntimeError("gh down")
        return self._fetch(repo, gh)


if __name__ == "__main__":
    unittest.main()
