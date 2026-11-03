"""Tests for the briefing + ledger port (stdlib unittest only).

Uses a throwaway FastAPI app including the briefing router (server.py
is owned by another program slice, so registration there is out of
scope) and a tmp ledger dir via BUTTONS_LEDGER_PATH.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import briefing as briefing_router
    from backend import ledger

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class BriefingLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self.tmp, "ledger.jsonl")
        app = FastAPI()
        app.include_router(briefing_router.router)
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("BUTTONS_LEDGER_PATH", None)
        os.environ.pop("BUTTONS_FIXTURE_BRIEFING", None)

    def _write_fixture(self, payload):
        path = os.path.join(self.tmp, "briefing.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.environ["BUTTONS_FIXTURE_BRIEFING"] = path
        return path

    def test_briefing_shape(self):
        self._write_fixture({
            "repo": "other",
            "attention": ["#1 — CI failing"],
            "prs": [{"number": 1, "title": "t"}],
            "failing_runs": [{"name": "ci"}],
            "jules": {"items": [], "other": 0, "unscoped": 0,
                      "unavailable": None},
        })
        resp = self.client.get("/api/briefing",
                               params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["repo"], "remote-server")
        self.assertEqual(body["attention"], ["#1 — CI failing"])
        self.assertEqual(len(body["prs"]), 1)
        self.assertEqual(len(body["failing_runs"]), 1)

    def test_briefing_bad_repo(self):
        resp = self.client.get("/api/briefing", params={"repo": "a/b/c"})
        self.assertEqual(resp.status_code, 400)

    def _fake_run(self, returncode, stdout, stderr=""):
        proc = mock.Mock()
        proc.returncode = returncode
        proc.stdout = stdout
        proc.stderr = stderr
        return mock.patch.object(subprocess, "run",
                                 return_value=proc)

    def test_briefing_uses_json_despite_exit_1(self):
        payload = {"repo": "previous_workouts", "prs": [],
                   "runs_err": "nope"}
        with self._fake_run(1, json.dumps(payload)):
            resp = self.client.get("/api/briefing",
                                   params={"repo":
                                           "previous_workouts"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["runs_err"], "nope")

    def test_briefing_fails_without_usable_stdout(self):
        with self._fake_run(1, "", "boom"):
            resp = self.client.get("/api/briefing",
                                   params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 500)
        self.assertIn("boom", resp.json()["detail"])

    def test_status_shape(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIn("health", body)
        self.assertIn("runs", body)

    def test_ledger_round_trip(self):
        ledger.append({"kind": "run", "action": "briefing-td",
                       "repo": "remote-server"})
        ledger.append({"kind": "system", "event": "buttons started"})
        resp = self.client.get("/api/ledger")
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 2)
        # Newest first.
        self.assertEqual(entries[0]["kind"], "system")
        self.assertIn("ts", entries[0])
        # Same file layout on disk: one JSON object per line.
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            lines = fh.readlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertIsInstance(json.loads(line), dict)

    def test_ledger_kinds_filter(self):
        ledger.append({"kind": "run", "action": "x"})
        ledger.append({"kind": "pr-action", "action": "y"})
        resp = self.client.get("/api/ledger", params={"kind": "run"})
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "run")
        # Unknown kind -> 400, mirroring buttons.
        resp = self.client.get("/api/ledger", params={"kind": "nope"})
        self.assertEqual(resp.status_code, 400)
        # No Jules kind is served.
        resp = self.client.get("/api/ledger", params={"kind": "jules"})
        self.assertEqual(resp.status_code, 400)

    def test_ledger_clear_semantics(self):
        ledger.append({"kind": "run", "action": "x"})
        resp = self.client.post("/api/ledger/clear")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})
        # Only the tombstone remains.
        entries = self.client.get("/api/ledger").json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "system")
        self.assertEqual(entries[0]["event"], "ledger cleared")


class BriefingScriptSmokeTest(unittest.TestCase):
    """The router shells out to core_cli/briefing_summary.py; it must import.

    Regression: the buttons image shipped the script without its sibling
    context_filter.py, so /api/briefing 500'd in prod while the
    fixture-based router tests stayed green.
    """

    def test_script_imports_resolve(self):
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        script = os.path.join(root, "core_cli", "briefing_summary.py")
        # No args -> usage to stderr, exit 2 (the script has no --help).
        # Any ModuleNotFoundError would traceback and exit 1 instead.
        proc = subprocess.run(
            [sys.executable, script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=60, text=True)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 2, out[-500:])
        self.assertIn("usage:", out)
        self.assertNotIn("Traceback", out)

    @unittest.skipUnless(HAS_CLIENT, "briefing router not importable")
    def test_run_briefing_uses_stack_cwd(self):
        # td discovers project_config.json upward from cwd; the image
        # workdir (/app) has none, so the router must run the script
        # from the live checkout.
        from backend.exec.engine import stack_dir
        proc = mock.Mock(returncode=0, stdout='{"repo": "x"}', stderr="")
        with mock.patch.object(
                briefing_router.subprocess, "run",
                return_value=proc) as run_mock:
            out = briefing_router._run_briefing("x", "arii/x")
        self.assertEqual(out, {"repo": "x"})
        self.assertEqual(run_mock.call_args.kwargs.get("cwd"), stack_dir())
        self.assertTrue(
            run_mock.call_args.args[0][1].endswith("briefing_summary.py"))


if __name__ == "__main__":
    unittest.main()
