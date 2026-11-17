"""Browser task execution and artifacts without a real Grid, extension or LLM."""

import asyncio
import base64
from collections import deque
import contextlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routers import (
    agent_control,
    browser_drive,
    browser_ext,
    browser_tasks,
    browser_view,
    jobs,
)
from backend.exec import agent_turn, engine

PNG = b"\x89PNG\r\n\x1a\nfixture"
OBSERVATION = {
    "url": "https://example.com/",
    "title": "Example",
    "observed_text": "[redacted]\nContinue",
    "landed_on_login": False,
}


class FakeCDP:
    instances = []
    page = {
        "url": "https://example.com/",
        "title": "Example",
        "els": [{"text": "[redacted]"}],
    }
    password = False

    def __init__(self, *args):
        self.calls = []
        self.instances.append(self)

    async def connect(self):
        pass

    async def close(self):
        pass

    async def reattach(self):
        pass

    async def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "Page.navigate":
            return {"loaderId": "new-document"}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"loaderId": "new-document"}}}
        if method == "Page.captureScreenshot":
            return {"data": base64.b64encode(PNG).decode()}
        if method == "Runtime.evaluate":
            expression = params["expression"]
            if expression == browser_drive.SNAPSHOT_JS:
                return {"result": {"value": self.page}}
            return {
                "result": {
                    "value": "complete"
                    if expression == "document.readyState"
                    else self.password
                }
            }
        raise AssertionError(method)


class BrowserTasksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "tasks"
        self.registry = engine.RunRegistry()
        for patch in (
            mock.patch.dict(
                os.environ,
                {
                    "BROWSER_TASKS_DIR": str(self.root),
                    "BROWSER_ACTIVITY_FILE": str(
                        Path(self.tmp.name) / "activity.jsonl"
                    ),
                    "AGENT_CONTROL_STATE_FILE": str(
                        Path(self.tmp.name) / "control.json"
                    ),
                },
            ),
            mock.patch.object(jobs, "registry", self.registry),
            mock.patch.object(browser_view, "_grid_live_session", return_value="grid"),
            mock.patch.object(browser_view, "CDPBridge", FakeCDP),
            mock.patch.object(browser_view, "_maybe_auto_learn"),
            mock.patch.object(browser_view, "_ACTIVITY", deque(maxlen=200)),
            mock.patch.object(
                browser_view, "remember_page", side_effect=lambda page, **kwargs: page
            ),
            mock.patch.object(browser_tasks, "POLL_SECONDS", 0.01),
            mock.patch.object(FakeCDP, "instances", []),
            mock.patch.object(FakeCDP, "password", False),
            mock.patch.object(FakeCDP, "page", dict(FakeCDP.page)),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        app = FastAPI()
        app.include_router(browser_tasks.router)
        app.include_router(jobs.router)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def submit(self, **kwargs):
        response = self.client.post(
            "/api/browser/tasks",
            json={
                "instructions": "Inspect",
                "target": "grid",
                "mode": "check",
                **kwargs,
            },
        )
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def wait(self, task_id, status="done"):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = self.client.get(f"/api/browser/tasks/{task_id}")
            self.assertEqual(response.status_code, 200, response.text)
            report = response.json()
            if report["status"] == status:
                return report
            if report["status"] in ("done", "failed", "cancelled", "paused_by_user"):
                self.fail(f"expected {status}: {report}")
            time.sleep(0.01)
        self.fail(f"task did not finish: {report}")

    def until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("condition did not become true")

    def test_check_mode_capture_shot_and_capped_masked_text_without_llm(self):
        FakeCDP.page["els"] = [{"text": "[redacted]"}] + [{"text": "a" * 80}] * 100
        with mock.patch.object(agent_turn.manager, "run_turn") as turn:
            task = self.submit(start_url="https://example.com/")
            report = self.wait(task["id"])
        turn.assert_not_called()
        self.assertEqual(report["url"], "https://example.com/")
        self.assertEqual(report["title"], "Example")
        self.assertTrue(report["observed_text"].startswith("[redacted]"))
        self.assertEqual(len(report["observed_text"]), 8000)
        self.assertFalse(report["landed_on_login"])
        self.assertEqual(len(report["captures"]), 1)
        self.assertIn(
            ("Page.navigate", {"url": "https://example.com/"}),
            FakeCDP.instances[0].calls,
        )
        path = self.root / task["id"] / "report.json"
        self.assertEqual(json.loads(path.read_text())["status"], "done")
        shot = self.client.get(f"/api/browser/tasks/{task['id']}/shots/0.png")
        self.assertEqual(shot.status_code, 200)
        self.assertEqual(shot.headers["content-type"], "image/png")
        self.assertEqual(shot.content, PNG)
        self.assertEqual(self.registry.get_run(task["id"])["exit_code"], 0)

    def test_login_detection_host_path_and_password_field(self):
        for url, password, expected in (
            ("https://github.com/login?return_to=x", False, True),
            ("https://github.com/login/oauth/authorize", False, True),
            ("https://github.com/", False, False),
            ("https://github.com.evil.test/login", False, False),
            ("https://example.com/", True, True),
        ):
            with self.subTest(url=url, password=password):
                FakeCDP.page["url"] = url
                FakeCDP.password = password
                report = self.wait(self.submit()["id"])
                self.assertEqual(report["landed_on_login"], expected)

    def test_current_page_does_not_navigate_and_empty_observations_are_valid(self):
        FakeCDP.page["els"] = []
        report = self.wait(self.submit()["id"])
        self.assertEqual(report["observed_text"], "")
        self.assertNotIn(
            "Page.navigate", [method for method, _params in FakeCDP.instances[0].calls]
        )

    def test_fifo_serialized_per_target_without_a_second_queue(self):
        entered = []
        release = threading.Event()

        async def capture(target, url):
            entered.append((target, url))
            while url == "https://first.test/" and not release.is_set():
                await asyncio.sleep(0.01)
            return dict(OBSERVATION), PNG

        with (
            mock.patch.object(browser_drive, "capture", side_effect=capture),
            mock.patch.object(browser_ext, "paired_bridge"),
            mock.patch.object(engine, "MAX_CONCURRENT_RUNS", 3),
        ):
            first = self.submit(start_url="https://first.test/")
            self.until(lambda: len(entered) == 1)
            second = self.submit(start_url="https://second.test/")
            third = self.submit(start_url="https://third.test/")
            other = self.submit(target="paired", start_url="https://other.test/")
            self.until(lambda: len(entered) == 2)
            self.assertTrue(self.registry.get_run(second["id"])["queued"])
            self.assertEqual(
                entered,
                [("grid", "https://first.test/"), ("paired", "https://other.test/")],
            )
            release.set()
            for task in (first, second, third, other):
                self.wait(task["id"])
        self.assertEqual(
            [url for target, url in entered if target == "grid"],
            ["https://first.test/", "https://second.test/", "https://third.test/"],
        )
        self.assertEqual(self.registry.get_run(first["id"])["action"], "browser-task")
        rows = self.client.get("/api/browser/tasks").json()["tasks"]
        self.assertEqual(
            [row["id"] for row in rows],
            [other["id"], third["id"], second["id"], first["id"]],
        )

    def test_agent_captures_before_and_after_and_passes_empty_repo_and_target(self):
        calls = []

        async def run(*args, **kwargs):
            calls.append((args, kwargs))
            yield "Final "
            yield agent_turn.TurnProgress("working")
            yield "summary"

        with (
            mock.patch.object(
                browser_drive, "capture", return_value=(OBSERVATION, PNG)
            ) as capture,
            mock.patch.object(browser_ext, "paired_bridge"),
            mock.patch.object(agent_turn.manager, "run_turn", side_effect=run),
            mock.patch.object(
                agent_turn.manager, "pop_result", return_value={"exit": 0}
            ),
        ):
            task = self.submit(
                target="paired", mode="agent", instructions="Inspect the page"
            )
            report = self.wait(task["id"])
        self.assertEqual(report["summary"], "Final summary")
        self.assertEqual(len(report["captures"]), 2)
        self.assertEqual(capture.await_count, 2)
        args, kwargs = calls[0]
        self.assertEqual(args[1:3], ("", agent_turn.TERMINAL_STACK))
        self.assertEqual(args[4:6], ("Inspect the page", False))
        uuid.UUID(args[3])
        self.assertEqual(kwargs, {"browser_target": "paired"})

    def test_human_pause_stops_agent_and_does_not_continue_or_capture_after(self):
        entered = threading.Event()

        async def run(*args, **kwargs):
            entered.set()
            while True:
                await asyncio.sleep(0.01)
                yield "working"

        with (
            mock.patch.object(
                browser_drive, "capture", return_value=(OBSERVATION, PNG)
            ) as capture,
            mock.patch.object(agent_turn.manager, "run_turn", side_effect=run),
            mock.patch.object(agent_turn.manager, "abort", return_value=True) as abort,
        ):
            task = self.submit(mode="agent")
            self.assertTrue(entered.wait(5))
            agent_control.pause_agent()
            report = self.wait(task["id"], "paused_by_user")
            self.assertEqual(capture.await_count, 1)
            self.assertEqual(report["status"], "paused_by_user")
            agent_control.resume_agent()
            self.assertEqual(
                self.client.get(f"/api/browser/tasks/{task['id']}").json()["status"],
                "paused_by_user",
            )
            self.until(lambda: self.registry.get_run(task["id"])["done"])
            # The manager also observes pause; either stream or monitor can stop first.
            self.assertLessEqual(abort.await_count, 1)

    def test_cancel_active_and_queued_runs(self):
        entered = threading.Event()

        async def capture(*args):
            entered.set()
            await asyncio.Event().wait()

        with mock.patch.object(browser_drive, "capture", side_effect=capture):
            first = self.submit()
            self.assertTrue(entered.wait(5))
            queued = self.submit()
            self.assertEqual(
                self.client.post(
                    f"/api/browser/tasks/{queued['id']}/cancel"
                ).status_code,
                202,
            )
            self.wait(queued["id"], "cancelled")
            self.assertEqual(
                self.client.post(
                    f"/api/browser/tasks/{first['id']}/cancel"
                ).status_code,
                202,
            )
            self.wait(first["id"], "cancelled")
        self.assertEqual(self.registry.active_ids, set())

    def test_retention_deletes_old_dirs_and_cannot_resurrect_pruned_tasks(self):
        for i in range(55):
            report = {
                "id": f"{i:012x}",
                "created_at": i,
                "status": "done",
                "captures": [],
            }
            (self.root / report["id"]).mkdir(parents=True)
            browser_tasks._save(report)
        browser_tasks.prune_reports()
        self.assertEqual(len(list(self.root.iterdir())), 50)
        self.assertFalse((self.root / "000000000000").exists())
        browser_tasks._save({"id": "000000000000", "status": "done"})
        self.assertFalse((self.root / "000000000000").exists())
        self.assertEqual(len(self.client.get("/api/browser/tasks").json()["tasks"]), 50)

    def test_contract_c_retention_keeps_live_reports_and_screenshots(self):
        """PR 1334 c: retention must never remove an active task's artifacts."""
        live = []
        for index in range(53):
            task_id = f"{index:012x}"
            status = ("queued", "running")[index] if index < 2 else "done"
            directory = self.root / task_id
            directory.mkdir(parents=True)
            browser_tasks._save(
                {"id": task_id, "created_at": index, "status": status, "captures": []}
            )
            (directory / "0.png").write_bytes(PNG)
            if index < 2:
                live.append(directory)
        with mock.patch.object(
            self.registry, "get_run", return_value={"done": False, "queued": True}
        ):
            browser_tasks.prune_reports()
        for directory in live:
            self.assertTrue((directory / "report.json").is_file(), str(directory))
            self.assertEqual((directory / "0.png").read_bytes(), PNG)
        self.assertFalse((self.root / "000000000002").exists())

    def test_unpaired_target_returns_409_before_queuing(self):
        with mock.patch.object(browser_ext, "_paired", None):
            response = self.client.post(
                "/api/browser/tasks",
                json={"instructions": "Inspect", "target": "paired"},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "no paired Chrome")
        self.assertEqual(self.registry.list_jobs(), [])

    def test_contract_d_finished_or_missing_run_reconciles_cancelling(self):
        """PR 1334 d: a persisted cancellation cannot remain pending after restart."""
        for run in (
            None,
            {"done": True, "exit_code": 0},
            {"done": True, "exit_code": 130},
        ):
            with self.subTest(run=run):
                task_id = "abcdef123456"
                (self.root / task_id).mkdir(parents=True, exist_ok=True)
                browser_tasks._save(
                    {
                        "id": task_id,
                        "created_at": 1,
                        "status": "cancelling",
                        "captures": [],
                    }
                )
                with mock.patch.object(self.registry, "get_run", return_value=run):
                    response = self.client.get(f"/api/browser/tasks/{task_id}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "cancelled")

    def test_capture_failure_finishes_job_and_releases_slot(self):
        with mock.patch.object(
            browser_drive, "capture", side_effect=RuntimeError("capture rejected")
        ):
            report = self.wait(self.submit()["id"], "failed")
        self.assertIn("capture rejected", report["error"])
        self.assertEqual(self.registry.active_ids, set())

    def test_failed_agent_still_captures_after_and_retains_final_text(self):
        async def run(*args, **kwargs):
            yield "Could not finish"

        with (
            mock.patch.object(
                browser_drive, "capture", return_value=(OBSERVATION, PNG)
            ),
            mock.patch.object(agent_turn.manager, "run_turn", side_effect=run),
            mock.patch.object(
                agent_turn.manager,
                "pop_result",
                return_value={"exit": 1, "stderr": "provider unavailable"},
            ),
        ):
            report = self.wait(self.submit(mode="agent")["id"], "failed")
        self.assertEqual(len(report["captures"]), 2)
        self.assertEqual(report["summary"], "Could not finish")
        self.assertIn("provider unavailable", report["error"])

    def test_empty_list_and_missing_or_finished_task_cancellation(self):
        self.assertEqual(self.client.get("/api/browser/tasks").json(), {"tasks": []})
        self.assertEqual(
            self.client.get("/api/browser/tasks/not-an-id").status_code, 404
        )
        self.assertEqual(
            self.client.post("/api/browser/tasks/000000000000/cancel").status_code, 404
        )
        task_id = self.wait(self.submit()["id"])["id"]
        self.assertEqual(
            self.client.post(f"/api/browser/tasks/{task_id}/cancel").status_code, 409
        )

    def test_interrupted_run_report_survives_registry_restart(self):
        task_id, _ = self.registry.start_run("browser-task", repo="")
        report = {"id": task_id, "created_at": 1, "status": "running", "captures": []}
        (self.root / task_id).mkdir(parents=True)
        browser_tasks._save(report)
        self.registry.finish_run(task_id, None)
        response = self.client.get(f"/api/browser/tasks/{task_id}").json()
        self.assertEqual(response["status"], "failed")
        self.assertIn("interrupted", response["error"])

    def test_missing_shots_traversal_and_symlinks_are_rejected(self):
        task_id = self.wait(self.submit()["id"])["id"]
        for suffix in ("shots/-1.png", "shots/99.png", "shots/..%2Freport.json.png"):
            response = self.client.get(f"/api/browser/tasks/{task_id}/{suffix}")
            self.assertIn(response.status_code, (404, 422))
        for task in ("not-an-id", "%2e%2e%2f", "000000000000"):
            self.assertIn(
                self.client.get(f"/api/browser/tasks/{task}/shots/0.png").status_code,
                (404, 422),
            )
        path = self.root / task_id / "0.png"
        path.unlink()
        path.symlink_to(self.root / task_id / "report.json")
        self.assertEqual(
            self.client.get(f"/api/browser/tasks/{task_id}/shots/0.png").status_code,
            404,
        )

    def test_activity_instructions_are_capped_and_invalid_requests_rejected(self):
        instructions = "x" * 300
        task = self.submit(instructions=instructions)
        self.wait(task["id"])
        events = [
            row for row in browser_view.recent_activity(100) if row["kind"] == "task"
        ]
        self.assertEqual(events[-1]["detail"], "x" * 200)
        for patch in (
            {"instructions": " "},
            {"target": "other"},
            {"mode": "other"},
            {"start_url": "file:///etc/passwd"},
        ):
            response = self.client.post(
                "/api/browser/tasks",
                json={"instructions": "Inspect", "target": "grid", **patch},
            )
            self.assertEqual(response.status_code, 422)


class CaptureTargetTest(unittest.IsolatedAsyncioTestCase):
    async def test_contract_b_capture_rebinds_after_cross_origin_target_swap(self):
        """PR 1334 b: capture must observe the new Grid document after navigation."""
        bridge = FakeCDP()
        original = bridge.call
        rebound = False

        async def reattach():
            nonlocal rebound
            rebound = True

        async def command(method, params=None):
            if method == "Page.getFrameTree" and not rebound:
                raise RuntimeError("old CDP target detached")
            return await original(method, params)

        @contextlib.asynccontextmanager
        async def session(target):
            yield bridge, "grid"

        bridge.reattach = reattach
        with (
            mock.patch.object(browser_drive, "_drive_session", session),
            mock.patch.object(browser_drive, "_require_human_not_driving"),
            mock.patch.object(bridge, "call", side_effect=command),
        ):
            observation, png = await browser_drive.capture(
                "grid", "https://other.test/"
            )
        self.assertEqual(png, PNG)
        self.assertEqual(observation["title"], "Example")

    async def test_same_capture_surface_supports_paired(self):
        bridge = FakeCDP()

        @contextlib.asynccontextmanager
        async def session(target):
            self.assertEqual(target, "paired")
            yield bridge, "paired"

        with (
            mock.patch.object(browser_drive, "_drive_session", session),
            mock.patch.object(browser_drive, "_require_human_not_driving"),
            mock.patch.object(browser_ext, "PairedBridge", FakeCDP),
        ):
            observation, png = await browser_drive.capture("paired")
        self.assertEqual(png, PNG)
        self.assertEqual(observation["url"], FakeCDP.page["url"])

    async def test_load_waits_for_navigation_commit_and_complete(self):
        bridge = FakeCDP()
        call = bridge.call
        commits = iter(["old-document", "new-document"])

        async def command(method, params=None):
            if method == "Page.getFrameTree":
                return {"frameTree": {"frame": {"loaderId": next(commits)}}}
            return await call(method, params)

        @contextlib.asynccontextmanager
        async def session(target):
            yield bridge, "grid"

        with (
            mock.patch.object(browser_drive, "_drive_session", session),
            mock.patch.object(browser_drive, "_require_human_not_driving"),
            mock.patch.object(bridge, "call", side_effect=command),
            mock.patch.object(
                browser_drive.asyncio, "sleep", new_callable=mock.AsyncMock
            ) as sleep,
        ):
            await browser_drive.capture(start_url="https://example.com/")
        self.assertEqual(sleep.await_count, 1)

    async def test_bad_png_navigation_error_and_load_timeout_are_loud(self):
        for failure in ("png", "navigation", "timeout"):
            with self.subTest(failure=failure):
                bridge = FakeCDP()
                call = bridge.call

                async def command(method, params=None):
                    if failure == "png" and method == "Page.captureScreenshot":
                        return {"data": base64.b64encode(b"not png").decode()}
                    if failure == "navigation" and method == "Page.navigate":
                        return {"errorText": "net::ERR_NAME_NOT_RESOLVED"}
                    if failure == "timeout" and method == "Runtime.evaluate":
                        return {"result": {"value": "loading"}}
                    return await call(method, params)

                @contextlib.asynccontextmanager
                async def session(target):
                    yield bridge, "grid"

                with (
                    mock.patch.object(browser_drive, "_drive_session", session),
                    mock.patch.object(browser_drive, "_require_human_not_driving"),
                    mock.patch.object(bridge, "call", side_effect=command),
                    mock.patch.object(
                        browser_drive,
                        "time",
                        mock.Mock(monotonic=mock.Mock(side_effect=[0, 100])),
                    ),
                ):
                    with self.assertRaises((ValueError, RuntimeError, TimeoutError)):
                        await browser_drive.capture(start_url="https://example.com/")


if __name__ == "__main__":
    unittest.main()
