"""Tests for the cross-repo lane dispatch queue (issue #853).

Covers the queue model (keyed by (repo, issue)), the live per-repo
readiness gates, dispatch through the repair worker, the monitor
transitions, and the dispatch router shapes. Subprocess/gh reads
are faked; nothing touches the network or docker.
"""

import json
import unittest
from unittest.mock import patch

from backend import lane_dispatch
from backend.api.routers import dispatch as dispatch_mod
from backend.api.routers import issues as issues_mod


class FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class FakeGh:
    """gh runner fake: argv snippet -> stdout payload or exception."""

    def __init__(self):
        self.calls = []
        self.routes = []

    def on(self, snippet, payload):
        self.routes.append((snippet, payload))
        return self

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        text = " ".join(str(a) for a in argv)
        for snippet, payload in self.routes:
            if snippet in text:
                if isinstance(payload, Exception):
                    raise payload
                return FakeProc(stdout=payload)
        raise AssertionError(f"unexpected gh argv: {argv}")


def _gh_ok():
    return (FakeGh()
            .on("repo view", json.dumps(
                {"defaultBranchRef": {"name": "main"}}))
            .on("/branches/main/protection", json.dumps(
                {"required_status_checks": {"contexts": ["ci", "lint"]}})))


def _clean_status(repo):
    return {"repo": repo, "branch": "main", "short": "", "clean": True}


def _verify_ok(issue, repo="remote-server"):
    return {"ok": True, "issue": issue, "command": "make test",
            "allowlisted": True, "error": ""}


class LaneQueueTest(unittest.TestCase):
    def setUp(self):
        lane_dispatch.reset_queue()

    def tearDown(self):
        lane_dispatch.reset_queue()

    def test_enqueue_roundtrip_keyed_by_repo_and_issue(self):
        item, err = lane_dispatch.enqueue("remote-server", "853")
        self.assertIsNone(err)
        self.assertEqual(item["repo"], "remote-server")
        self.assertEqual(item["issue"], 853)
        self.assertEqual(item["state"], "queued")
        # Same issue in another repo is a distinct item.
        other, err = lane_dispatch.enqueue("tech-dancer", 853)
        self.assertIsNone(err)
        self.assertEqual(len(lane_dispatch.queue_list()), 2)
        self.assertEqual(other["repo"], "tech-dancer")

    def test_enqueue_rejects_bad_repo_and_issue(self):
        item, err = lane_dispatch.enqueue("nope/..", 1)
        self.assertIsNone(item)
        self.assertEqual(err, "invalid repo")
        item, err = lane_dispatch.enqueue("remote-server", "abc")
        self.assertIsNone(item)
        self.assertEqual(err, "invalid issue")

    def test_enqueue_idempotent_until_terminal(self):
        first, _ = lane_dispatch.enqueue("remote-server", 1)
        second, err = lane_dispatch.enqueue("remote-server", "#1")
        self.assertIsNone(err)
        self.assertEqual(first, second)
        self.assertEqual(len(lane_dispatch.queue_list()), 1)

    def test_enqueue_terminal_requeues(self):
        lane_dispatch.enqueue("remote-server", 1)
        with lane_dispatch._lock:
            lane_dispatch._items[("remote-server", "1")]["state"] = \
                lane_dispatch.DONE
        item, err = lane_dispatch.enqueue("remote-server", 1)
        self.assertIsNone(err)
        self.assertEqual(item["state"], "queued")

    def test_cancel_queued_and_unknown(self):
        lane_dispatch.enqueue("remote-server", 1)
        removed, err = lane_dispatch.cancel("remote-server", 1)
        self.assertTrue(removed)
        self.assertIsNone(err)
        removed, err = lane_dispatch.cancel("remote-server", 1)
        self.assertFalse(removed)
        self.assertEqual(err, "unknown item")

    def test_cancel_running_refuses(self):
        lane_dispatch.enqueue("remote-server", 1)
        with lane_dispatch._lock:
            lane_dispatch._items[("remote-server", "1")]["state"] = \
                lane_dispatch.RUNNING
        removed, err = lane_dispatch.cancel("remote-server", 1)
        self.assertFalse(removed)
        self.assertEqual(err, "item is running")


class RepoGatesTest(unittest.TestCase):
    def test_reads_base_and_required_checks(self):
        gates = lane_dispatch.read_repo_gates("tech-dancer",
                                              gh_runner=_gh_ok())
        self.assertTrue(gates["ok"])
        self.assertEqual(gates["base"], "main")
        self.assertEqual(gates["required_checks"], ["ci", "lint"])

    def test_bad_repo_name(self):
        gates = lane_dispatch.read_repo_gates("nope/..")
        self.assertFalse(gates["ok"])

    def test_default_branch_failure_blocks(self):
        gh = FakeGh().on("repo view", RuntimeError("boom"))
        gates = lane_dispatch.read_repo_gates("remote-server",
                                              gh_runner=gh)
        self.assertFalse(gates["ok"])
        self.assertIn("default branch", gates["error"])

    def test_unprotected_branch_is_an_empty_bar(self):
        gh = (FakeGh()
              .on("repo view", json.dumps(
                  {"defaultBranchRef": {"name": "main"}}))
              .on("/branches/main/protection",
                  RuntimeError("404 not found")))
        gates = lane_dispatch.read_repo_gates("remote-server",
                                              gh_runner=gh)
        self.assertTrue(gates["ok"])
        self.assertEqual(gates["required_checks"], [])


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        lane_dispatch.reset_queue()

    def tearDown(self):
        lane_dispatch.reset_queue()

    def _ready(self, **over):
        kw = {"repo_status": _clean_status,
              "verify_preview": _verify_ok,
              "worker_busy": lambda: False,
              "gh_runner": _gh_ok()}
        kw.update(over)
        return lane_dispatch.readiness("remote-server", 1, **kw)

    def test_all_gates_pass(self):
        ready = self._ready()
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["reasons"], [])
        self.assertEqual(ready["gates"]["protection"]["base"], "main")

    def test_dirty_checkout_blocks(self):
        def dirty(repo):
            return {"repo": repo, "branch": "main", "short": " M x",
                    "clean": False}

        ready = self._ready(repo_status=dirty)
        self.assertFalse(ready["ready"])
        self.assertTrue(any("uncommitted" in r for r in ready["reasons"]))

    def test_missing_checkout_blocks(self):
        def missing(repo):
            raise FileNotFoundError("nope")

        ready = self._ready(repo_status=missing)
        self.assertFalse(ready["ready"])
        self.assertTrue(any("clone" in r for r in ready["reasons"]))

    def test_repo_conflict_blocks(self):
        lane_dispatch.enqueue("remote-server", 2)
        with lane_dispatch._lock:
            lane_dispatch._items[("remote-server", "2")]["state"] = \
                lane_dispatch.RUNNING
        ready = self._ready()
        self.assertFalse(ready["ready"])
        self.assertTrue(any("already has" in r for r in ready["reasons"]))
        # Another repo is unaffected (per-repo conflict detection).
        other = lane_dispatch.readiness(
            "tech-dancer", 1, repo_status=_clean_status,
            verify_preview=_verify_ok, worker_busy=lambda: False,
            gh_runner=_gh_ok())
        self.assertTrue(other["ready"])

    def test_busy_worker_blocks(self):
        ready = self._ready(worker_busy=lambda: True)
        self.assertFalse(ready["ready"])
        self.assertTrue(any("busy" in r for r in ready["reasons"]))

    def test_rejected_verify_blocks_but_missing_command_passes(self):
        def rejected(issue, repo="remote-server"):
            return {"ok": True, "issue": issue, "command": "rm -rf /",
                    "allowlisted": False, "error": "not allowlisted"}

        ready = self._ready(verify_preview=rejected)
        self.assertFalse(ready["ready"])
        self.assertTrue(any("allowlisted" in r for r in ready["reasons"]))

        def missing(issue, repo="remote-server"):
            return {"ok": True, "issue": issue, "command": "",
                    "allowlisted": False, "error": "no spec"}

        ready = self._ready(verify_preview=missing)
        self.assertTrue(ready["ready"])

    def test_unreadable_gates_block(self):
        gh = FakeGh().on("repo view", RuntimeError("boom"))
        ready = self._ready(gh_runner=gh)
        self.assertFalse(ready["ready"])


class DispatchTest(unittest.TestCase):
    def setUp(self):
        lane_dispatch.reset_queue()

    def tearDown(self):
        lane_dispatch.reset_queue()

    def _seams(self, **over):
        kw = {"repo_status": _clean_status,
              "verify_preview": _verify_ok,
              "worker_busy": lambda: False,
              "gh_runner": _gh_ok()}
        kw.update(over)
        return kw

    def test_blocked_dispatch_changes_nothing(self):
        launched = []
        item, err = lane_dispatch.dispatch(
            "remote-server", 1,
            launcher=lambda i, r: launched.append((i, r)),
            monitor=lambda r, i: None,
            **self._seams(worker_busy=lambda: True))
        self.assertIsNone(item)
        self.assertIn("busy", err)
        self.assertEqual(launched, [])
        stored = lane_dispatch.get_item("remote-server", 1)
        self.assertEqual(stored["state"], "queued")

    def test_success_launches_and_marks_running(self):
        launched = []
        watched = []
        item, err = lane_dispatch.dispatch(
            "tech-dancer", 4848,
            launcher=lambda i, r: launched.append((i, r)),
            monitor=lambda r, i: watched.append((r, i)),
            **self._seams())
        self.assertIsNone(err)
        self.assertEqual(item["state"], "running")
        self.assertEqual(launched, [(4848, "tech-dancer")])
        self.assertEqual(watched, [("tech-dancer", 4848)])
        stored = lane_dispatch.get_item("tech-dancer", 4848)
        self.assertEqual(stored["state"], "running")

    def test_launch_failure_reverts_to_queued(self):
        def fail(i, r):
            raise RuntimeError("spawn boom")

        item, err = lane_dispatch.dispatch(
            "remote-server", 1, launcher=fail,
            monitor=lambda r, i: None, **self._seams())
        self.assertIsNone(item)
        self.assertIn("launch failed", err)
        stored = lane_dispatch.get_item("remote-server", 1)
        self.assertEqual(stored["state"], "queued")

    def test_running_item_refuses_second_dispatch(self):
        lane_dispatch.dispatch(
            "remote-server", 1, launcher=lambda i, r: None,
            monitor=lambda r, i: None, **self._seams())
        item, err = lane_dispatch.dispatch(
            "remote-server", 1, launcher=lambda i, r: None,
            monitor=lambda r, i: None, **self._seams())
        self.assertIsNone(item)
        self.assertEqual(err, "item is already running")

    def test_invalid_inputs(self):
        item, err = lane_dispatch.dispatch(
            "nope/..", 1, launcher=lambda i, r: None,
            monitor=lambda r, i: None)
        self.assertIsNone(item)
        self.assertEqual(err, "invalid repo")


class MonitorTest(unittest.TestCase):
    def setUp(self):
        lane_dispatch.reset_queue()
        with issues_mod.repair_state_lock:
            self._saved = dict(issues_mod.repair_state)

    def tearDown(self):
        with issues_mod.repair_state_lock:
            issues_mod.repair_state.clear()
            issues_mod.repair_state.update(self._saved)
        lane_dispatch.reset_queue()

    def _running_item(self):
        lane_dispatch.enqueue("remote-server", 1)
        with lane_dispatch._lock:
            lane_dispatch._items[("remote-server", "1")]["state"] = \
                lane_dispatch.RUNNING

    def _worker(self, status, issue=1, repo="remote-server",
                exit_code=None):
        with issues_mod.repair_state_lock:
            issues_mod.repair_state.update(
                {"status": status, "issue": issue, "repo": repo,
                 "exit_code": exit_code})

    def test_done_worker_marks_done(self):
        self._running_item()
        self._worker("done")
        lane_dispatch._monitor_item("remote-server", 1)
        stored = lane_dispatch.get_item("remote-server", 1)
        self.assertEqual(stored["state"], "done")

    def test_aborted_worker_marks_failed(self):
        self._running_item()
        self._worker("aborted")
        lane_dispatch._monitor_item("remote-server", 1)
        stored = lane_dispatch.get_item("remote-server", 1)
        self.assertEqual(stored["state"], "failed")
        self.assertIn("aborted", stored["detail"])

    def test_failed_worker_carries_exit_code(self):
        self._running_item()
        self._worker("failed", exit_code=3)
        lane_dispatch._monitor_item("remote-server", 1)
        stored = lane_dispatch.get_item("remote-server", 1)
        self.assertEqual(stored["state"], "failed")
        self.assertIn("exit 3", stored["detail"])

    def test_worker_moved_on_marks_failed(self):
        self._running_item()
        self._worker("running", issue=2)
        lane_dispatch._monitor_item("remote-server", 1)
        stored = lane_dispatch.get_item("remote-server", 1)
        self.assertEqual(stored["state"], "failed")
        self.assertIn("moved on", stored["detail"])

    def test_timeout_marks_failed_without_sleeping(self):
        self._running_item()
        self._worker("running")
        lane_dispatch._monitor_item("remote-server", 1, timeout=0)
        stored = lane_dispatch.get_item("remote-server", 1)
        self.assertEqual(stored["state"], "failed")
        self.assertIn("timed out", stored["detail"])


class DispatchRouterTest(unittest.TestCase):
    def setUp(self):
        lane_dispatch.reset_queue()

    def tearDown(self):
        lane_dispatch.reset_queue()

    def test_health(self):
        self.assertEqual(dispatch_mod.health(), {"ok": True})

    def test_queue_lists_items(self):
        lane_dispatch.enqueue("remote-server", 1)
        lane_dispatch.enqueue("tech-dancer", 2)
        body = dispatch_mod.queue()
        self.assertEqual(len(body["items"]), 2)

    def test_enqueue_ok_and_bad(self):
        row = dispatch_mod.enqueue(
            dispatch_mod.EnqueueRequest(repo="remote-server",
                                       issue="12"))
        self.assertEqual(row["issue"], 12)
        with self.assertRaises(Exception) as ctx:
            dispatch_mod.enqueue(
                dispatch_mod.EnqueueRequest(repo="remote-server",
                                            issue="abc"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_gates_ok_and_bad_inputs(self):
        with patch.object(
                lane_dispatch, "readiness",
                return_value={"ready": True, "reasons": [],
                              "gates": {}}) as ready:
            body = dispatch_mod.gates(repo="tech-dancer", issue="48")
        ready.assert_called_once_with("tech-dancer", 48)
        self.assertTrue(body["ready"])
        with self.assertRaises(Exception) as ctx:
            dispatch_mod.gates(repo="nope/..", issue="1")
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(Exception) as ctx:
            dispatch_mod.gates(repo="remote-server", issue="abc")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_start_requires_confirm(self):
        with self.assertRaises(Exception) as ctx:
            dispatch_mod.start(dispatch_mod.StartRequest(
                repo="remote-server", issue=1))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_start_ok_blocked_and_bad(self):
        with patch.object(lane_dispatch, "dispatch",
                          return_value=({"repo": "remote-server",
                                         "issue": 1}, None)):
            body = dispatch_mod.start(dispatch_mod.StartRequest(
                repo="remote-server", issue=1,
                intent="dispatch-start", confirmed=True))
        self.assertEqual(body["status"], "running")
        with patch.object(lane_dispatch, "dispatch",
                          return_value=(None, "worker busy")):
            with self.assertRaises(Exception) as ctx:
                dispatch_mod.start(dispatch_mod.StartRequest(
                    repo="remote-server", issue=1,
                    intent="dispatch-start", confirmed=True))
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(Exception) as ctx:
            dispatch_mod.start(dispatch_mod.StartRequest(
                repo="remote-server", issue="abc",
                intent="dispatch-start", confirmed=True))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_cancel_ok_running_and_unknown(self):
        with patch.object(lane_dispatch, "cancel",
                          return_value=(True, None)):
            body = dispatch_mod.cancel(dispatch_mod.CancelRequest(
                repo="remote-server", issue=1))
        self.assertTrue(body["removed"])
        with patch.object(lane_dispatch, "cancel",
                          return_value=(False, "item is running")):
            with self.assertRaises(Exception) as ctx:
                dispatch_mod.cancel(dispatch_mod.CancelRequest(
                    repo="remote-server", issue=1))
        self.assertEqual(ctx.exception.status_code, 409)
        with patch.object(lane_dispatch, "cancel",
                          return_value=(False, "unknown item")):
            with self.assertRaises(Exception) as ctx:
                dispatch_mod.cancel(dispatch_mod.CancelRequest(
                    repo="remote-server", issue=1))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_router_registered_in_server(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        server_src = (root / "server.py").read_text()
        self.assertIn("dispatch", server_src)
        router_src = (root / "api" / "routers" / "dispatch.py") \
            .read_text()
        self.assertIn("/api/dispatch", router_src)
        self.assertIn("/health", router_src)
        self.assertIn('"ok"', router_src)


if __name__ == "__main__":
    unittest.main()
