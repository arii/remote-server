"""Tests for the briefing + ledger port (stdlib unittest only).

Uses a throwaway FastAPI app including the briefing router (server.py
is owned by another program slice, so registration there is out of
scope) and a tmp ledger dir via BUTTONS_LEDGER_PATH.
"""

import ast
import json
import os
import subprocess
import sys
import tempfile
import time
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


class _SyncThread:
    """Thread fake: runs the target inline on start() for
    deterministic stale-while-revalidate tests (no real threads)."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _check_briefing_argv(case, argv, repo, gh):
    """Exact argv for the briefing script spawn, except the script
    dir prefix (checkout-dependent): interpreter, script name, and
    every flag pinned."""
    case.assertEqual(argv[0], "python3")
    case.assertTrue(argv[1].endswith("briefing_summary.py"))
    case.assertEqual(
        argv[2:],
        ["--briefing-json", "--repo", repo, "--gh", gh])


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class BriefingLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self.tmp, "ledger.jsonl")
        # Narrator off: these tests pin data-path behavior; prose has
        # its own suite (test_briefing_narrative.py).
        self._old_narrator = os.environ.get("BRIEFING_NARRATOR")
        os.environ["BRIEFING_NARRATOR"] = "off"
        # Fixture env isolated: ambient opt-ins must not leak into
        # stray/auto-discovery assertions (#453).
        self._old_auto = os.environ.get("BUTTONS_FIXTURE_AUTO_DISCOVER")
        self._old_repos = os.environ.get("BUTTONS_REPOS_DIR")
        os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)
        app = FastAPI()
        app.include_router(briefing_router.router)
        self.client = TestClient(app)
        briefing_router._BRIEFING_CACHE.clear()
        briefing_router._BRIEFING_REFRESHING.clear()

    def tearDown(self):
        os.environ.pop("BUTTONS_LEDGER_PATH", None)
        os.environ.pop("BUTTONS_FIXTURE_BRIEFING", None)
        if self._old_narrator is None:
            os.environ.pop("BRIEFING_NARRATOR", None)
        else:
            os.environ["BRIEFING_NARRATOR"] = self._old_narrator
        if self._old_auto is None:
            os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)
        else:
            os.environ["BUTTONS_FIXTURE_AUTO_DISCOVER"] = self._old_auto
        if self._old_repos is None:
            os.environ.pop("BUTTONS_REPOS_DIR", None)
        else:
            os.environ["BUTTONS_REPOS_DIR"] = self._old_repos
        briefing_router._BRIEFING_CACHE.clear()
        briefing_router._BRIEFING_REFRESHING.clear()

    def _write_fixture(self, payload):
        path = os.path.join(self.tmp, "briefing.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.environ["BUTTONS_FIXTURE_BRIEFING"] = path
        return path

    def test_briefing_shape(self):
        path = self._write_fixture({
            "repo": "other",
            "attention": ["#1 — CI failing"],
            "prs": [{"number": 1, "title": "t"}],
            "failing_runs": [{"name": "ci"}],
        })
        resp = self.client.get("/api/briefing",
                               params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["repo"], "remote-server")
        self.assertEqual(body["attention"], ["#1 — CI failing"])
        self.assertEqual(len(body["prs"]), 1)
        self.assertEqual(len(body["failing_runs"]), 1)
        # Fixture marker (#453): canned responses say so + name the
        # source file, so operators can tell fixture from live.
        self.assertTrue(body["fixture"])
        self.assertEqual(body["fixture_source"], path)

    def _write_stray(self, payload):
        """A fixture-briefing.json next to the checkouts with no
        explicit env var: the stray-file setup (#453)."""
        stray = os.path.join(self.tmp, "fixture-briefing.json")
        with open(stray, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.environ["BUTTONS_REPOS_DIR"] = self.tmp
        os.environ.pop("BUTTONS_FIXTURE_BRIEFING", None)
        os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)
        return stray

    def _warn_text(self, warn_mock):
        return " ".join(str(a) for call in warn_mock.call_args_list
                        for a in call[0])

    def test_fixture_serve_logs_warning(self):
        path = self._write_fixture({"repo": "other", "prs": []})
        with mock.patch.object(briefing_router.logger,
                               "warning") as warn:
            resp = self.client.get("/api/briefing",
                                   params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["fixture"])
        self.assertTrue(warn.called)
        logged = self._warn_text(warn)
        self.assertIn(path, logged)
        self.assertIn("remote-server", logged)

    def test_stray_fixture_ignored_without_opt_in(self):
        """A stray next-to-checkouts file alone cannot shadow live."""
        stray = self._write_stray({"repo": "other", "prs": [{"n": 1}],
                                   "attention": ["STRAY"]})
        live = {"repo": "remote-server", "prs": [], "attention": []}
        with mock.patch.object(briefing_router, "_run_briefing",
                               return_value=dict(live)) as run_mock, \
                mock.patch.object(briefing_router.logger,
                                  "warning") as warn:
            resp = self.client.get("/api/briefing",
                                   params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["prs"], [])
        self.assertEqual(body["attention"], [])
        self.assertFalse(body["fixture"])
        self.assertEqual(body["fixture_source"], "")
        run_mock.assert_called_once_with("remote-server",
                                         "arii/remote-server")
        self.assertTrue(warn.called)
        logged = self._warn_text(warn)
        self.assertIn("stray", logged)
        self.assertIn(stray, logged)

    def test_auto_discover_opt_in_serves_stray_fixture(self):
        stray = self._write_stray({"repo": "other",
                                   "attention": ["STRAY"]})
        os.environ["BUTTONS_FIXTURE_AUTO_DISCOVER"] = "1"
        with mock.patch.object(briefing_router, "_run_briefing") \
                as run_mock, \
                mock.patch.object(briefing_router.logger,
                                  "warning") as warn:
            resp = self.client.get("/api/briefing",
                                   params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["attention"], ["STRAY"])
        self.assertTrue(body["fixture"])
        self.assertEqual(body["fixture_source"], stray)
        run_mock.assert_not_called()
        self.assertIn("serving fixture", self._warn_text(warn))

    def test_missing_explicit_fixture_falls_through_to_live(self):
        os.environ["BUTTONS_FIXTURE_BRIEFING"] = os.path.join(
            self.tmp, "no-such-fixture.json")
        live = {"repo": "remote-server", "prs": []}
        with mock.patch.object(briefing_router, "_run_briefing",
                               return_value=dict(live)) as run_mock, \
                mock.patch.object(briefing_router.logger,
                                  "warning") as warn:
            resp = self.client.get("/api/briefing",
                                   params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["fixture"])
        run_mock.assert_called_once()
        self.assertIn("missing", self._warn_text(warn))

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
        with self._fake_run(1, json.dumps(payload)) as run_mock:
            resp = self.client.get("/api/briefing",
                                   params={"repo":
                                           "previous_workouts"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["runs_err"], "nope")
        _check_briefing_argv(self, run_mock.call_args[0][0],
                             "previous_workouts",
                             "arii/previous_workouts")

    def test_briefing_fails_without_usable_stdout(self):
        with self._fake_run(1, "", "boom") as run_mock:
            resp = self.client.get("/api/briefing",
                                   params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 500)
        self.assertIn("boom", resp.json()["detail"])
        _check_briefing_argv(self, run_mock.call_args[0][0],
                             "remote-server", "arii/remote-server")

    def test_briefing_caches_within_ttl(self):
        payload = {"repo": "remote-server", "prs": [],
                   "attention": []}
        with self._fake_run(0, json.dumps(payload)) as run_mock:
            first = self.client.get(
                "/api/briefing", params={"repo": "remote-server"})
            second = self.client.get(
                "/api/briefing", params={"repo": "remote-server"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(run_mock.call_count, 1)
        _check_briefing_argv(self, run_mock.call_args[0][0],
                             "remote-server", "arii/remote-server")
        self.assertFalse(first.json()["cached"])
        self.assertTrue(second.json()["cached"])
        self.assertFalse(second.json()["stale"])
        self.assertIn("generated_at", second.json())

    def test_briefing_serves_stale_and_revalidates(self):
        stale_payload = {"repo": "remote-server", "prs": [],
                         "attention": ["old"]}
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time() - 120, stale_payload)
        fresh = {"repo": "remote-server", "prs": [],
                 "attention": ["new"]}
        fake_threading = mock.Mock()
        fake_threading.Thread = _SyncThread
        with self._fake_run(0, json.dumps(fresh)), \
                mock.patch.object(briefing_router, "threading",
                                  fake_threading):
            resp = self.client.get(
                "/api/briefing", params={"repo": "remote-server"})
        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        # Served the stale entry immediately...
        self.assertTrue(body["cached"])
        self.assertTrue(body["stale"])
        self.assertEqual(body["attention"], ["old"])
        # ...and the background refresh stored the fresh payload.
        self.assertEqual(
            briefing_router._BRIEFING_CACHE[
                "remote-server"][1]["attention"],
            ["new"])
        self.assertNotIn("remote-server",
                         briefing_router._BRIEFING_REFRESHING)

    def test_briefing_recomputes_past_max_stale(self):
        ancient = {"repo": "remote-server", "prs": [],
                   "attention": ["old"]}
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time()
            - briefing_router.BRIEFING_CACHE_MAX_STALE - 1,
            ancient)
        fresh = {"repo": "remote-server", "prs": [],
                 "attention": ["new"]}
        with self._fake_run(0, json.dumps(fresh)) as run_mock:
            resp = self.client.get(
                "/api/briefing", params={"repo": "remote-server"})
        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(run_mock.call_count, 1)
        _check_briefing_argv(self, run_mock.call_args[0][0],
                             "remote-server", "arii/remote-server")
        self.assertFalse(body["cached"])
        self.assertFalse(body["stale"])
        self.assertEqual(body["attention"], ["new"])

    def test_briefing_refresh_failure_keeps_stale(self):
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time() - 120, {"attention": ["old"]})
        briefing_router._BRIEFING_REFRESHING.add("remote-server")
        with mock.patch.object(
                briefing_router, "_run_briefing",
                side_effect=RuntimeError("gh down")):
            briefing_router._refresh_briefing(
                "remote-server", "arii/remote-server")
        self.assertEqual(
            briefing_router._BRIEFING_CACHE[
                "remote-server"][1]["attention"],
            ["old"])
        self.assertNotIn("remote-server",
                         briefing_router._BRIEFING_REFRESHING)

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

    def test_ledger_agent_turn_kind_queryable(self):
        # Issue #457: agent turns write kind "agent-turn"; the
        # allowlist must serve it instead of 400ing.
        ledger.append({"kind": "agent-turn", "session_id": "s1"})
        ledger.append({"kind": "run", "action": "x"})
        resp = self.client.get(
            "/api/ledger", params={"kind": "agent-turn"})
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "agent-turn")
        self.assertEqual(entries[0]["session_id"], "s1")

    def test_ledger_since_filter(self):
        now = time.time()
        rows = [
            {"kind": "run", "event": "old", "ts": now - 90000},
            {"kind": "run", "event": "new", "ts": now - 10},
            {"kind": "run", "event": "ageless"},
        ]
        with open(os.environ["BUTTONS_LEDGER_PATH"], "w") as fh:
            for row in rows:
                fh.write(json.dumps(row) + "\n")
        resp = self.client.get("/api/ledger", params={"since": now - 3600})
        self.assertEqual(resp.status_code, 200)
        events = [e["event"] for e in resp.json()["entries"]]
        # Old entry dropped, newest first, ageless kept.
        self.assertEqual(events, ["ageless", "new"])
        # since=0 (and negative) disables the filter.
        resp = self.client.get("/api/ledger", params={"since": 0})
        self.assertEqual(len(resp.json()["entries"]), 3)
        resp = self.client.get("/api/ledger", params={"since": -5})
        self.assertEqual(len(resp.json()["entries"]), 3)

    def test_ledger_clear_semantics(self):
        ledger.append({"kind": "run", "action": "x"})
        resp = self.client.post(
            "/api/ledger/clear",
            json={"intent": "ledger-clear", "confirmed": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})
        # Only the tombstone remains.
        entries = self.client.get("/api/ledger").json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "system")
        self.assertEqual(entries[0]["event"], "ledger cleared")

    def test_ledger_clear_requires_confirm(self):
        # Same gate as jobs/clear (#454): no intent+confirmed, no
        # erasure — and the audit trail stays intact on rejection.
        ledger.append({"kind": "run", "action": "x"})
        gate = {"intent": "ledger-clear", "confirmed": True}
        for kwargs in ({"json": {}}, {"json": {"intent": "ledger-clear"}},
                       {"json": {"intent": "ledger-clear",
                                 "confirmed": False}},
                       {"json": {"intent": "jobs-clear",
                                 "confirmed": True}}):
            resp = self.client.post("/api/ledger/clear", **kwargs)
            self.assertEqual(resp.status_code, 400, kwargs)
        # Bare POST with no body hits the same 400 gate (not 422).
        resp = self.client.post("/api/ledger/clear")
        self.assertEqual(resp.status_code, 400)
        kept = self.client.get("/api/ledger").json()["entries"]
        self.assertEqual(
            [e.get("action") for e in kept], ["x"])
        # Confirmed POST clears + tombstones as before.
        resp = self.client.post("/api/ledger/clear", json=gate)
        self.assertEqual(resp.status_code, 200)
        entries = self.client.get("/api/ledger").json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "ledger cleared")


class FixtureResolveTest(unittest.TestCase):
    """Resolver rules for the briefing fixture override (#453)."""

    def test_explicit_env_wins_without_opt_in(self):
        from backend.core import resolve_briefing_fixture_path

        with mock.patch.dict(os.environ, {
                "BUTTONS_FIXTURE_BRIEFING": " /x/y.json ",
        }):
            os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)
            # Stripped, verbatim, no existence check here — the
            # caller falls through to live with a warning.
            self.assertEqual(resolve_briefing_fixture_path(),
                             "/x/y.json")

    def test_candidate_gated_behind_opt_in(self):
        from backend.core import (
            briefing_fixture_candidate,
            resolve_briefing_fixture_path,
        )

        with tempfile.TemporaryDirectory() as tmp:
            stray = os.path.join(tmp, "fixture-briefing.json")
            with open(stray, "w", encoding="utf-8") as fh:
                json.dump({"prs": []}, fh)
            with mock.patch.dict(os.environ,
                                 {"BUTTONS_REPOS_DIR": tmp}):
                os.environ.pop("BUTTONS_FIXTURE_BRIEFING", None)
                os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)
                self.assertEqual(briefing_fixture_candidate(), stray)
                self.assertEqual(resolve_briefing_fixture_path(), "")
                os.environ["BUTTONS_FIXTURE_AUTO_DISCOVER"] = "true"
                self.assertEqual(resolve_briefing_fixture_path(),
                                 stray)

    def test_opt_in_truthy_values(self):
        from backend.core import fixture_auto_discover_enabled

        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            with mock.patch.dict(os.environ, {
                    "BUTTONS_FIXTURE_AUTO_DISCOVER": value}):
                self.assertTrue(fixture_auto_discover_enabled(),
                                value)
        for value in ("", "0", "false", "no", "off", "maybe"):
            with mock.patch.dict(os.environ, {
                    "BUTTONS_FIXTURE_AUTO_DISCOVER": value}):
                self.assertFalse(fixture_auto_discover_enabled(),
                                 value)

    def test_nothing_configured_resolves_empty(self):
        from backend.core import resolve_briefing_fixture_path

        with mock.patch.dict(os.environ, {}):
            os.environ.pop("BUTTONS_FIXTURE_BRIEFING", None)
            os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)
            os.environ["BUTTONS_REPOS_DIR"] = os.path.join(
                tempfile.mkdtemp(), "no-such-dir")
            self.assertEqual(resolve_briefing_fixture_path(), "")


class BriefingScriptSmokeTest(unittest.TestCase):
    """The router shells out to backend/briefing_summary.py; it must import.

    Regression: the buttons image shipped the script without its sibling
    context_filter.py, so /api/briefing 500'd in prod while the
    fixture-based router tests stayed green.
    """

    def test_script_imports_resolve(self):
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        script = os.path.join(root, "backend", "briefing_summary.py")
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
        _check_briefing_argv(self, run_mock.call_args.args[0],
                             "x", "arii/x")


CLI_TURN = {
    "kind": "deep-agent-run",
    "ts": 1700000000.0,
    "script": "ask",
    "model": "qwen",
    "thread_id": "t1",
    "duration_s": 12.5,
    "tool_calls": [{"name": "shell", "args": "ls"}],
    "tool_results": [],
    "usage": {"input_tokens": 100, "output_tokens": 50,
              "total_tokens": 150},
    "cost_usd": 0.0012,
    "metered": False,
    "final_preview": "done",
    "extra": {},
}


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class CliLedgerUnionTest(unittest.TestCase):
    """CLI turns surface in console history (issue #590)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old = {k: os.environ.get(k) for k in
                     ("BUTTONS_LEDGER_PATH", "EVAL_LEDGER",
                      "DEEP_AGENT_STATE_DIR")}
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self.tmp, "ledger.jsonl")
        os.environ["EVAL_LEDGER"] = os.path.join(
            self.tmp, "cli-ledger.jsonl")
        os.environ.pop("DEEP_AGENT_STATE_DIR", None)
        app = FastAPI()
        app.include_router(briefing_router.router)
        self.client = TestClient(app)

    def tearDown(self):
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _write_cli(self, rows):
        with open(os.environ["EVAL_LEDGER"], "w") as fh:
            for row in rows:
                fh.write(row if isinstance(row, str)
                         else json.dumps(row) + "\n")

    def test_cli_turn_normalizes_to_cli_turn_kind(self):
        self._write_cli([CLI_TURN])
        resp = self.client.get("/api/ledger")
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        row = entries[0]
        self.assertEqual(row["kind"], "cli-turn")
        self.assertEqual(row["ts"], 1700000000.0)
        self.assertEqual(row["script"], "ask")
        self.assertEqual(row["model"], "qwen")
        self.assertEqual(row["thread_id"], "t1")
        self.assertEqual(row["duration_s"], 12.5)
        self.assertEqual(row["usage"]["total_tokens"], 150)
        self.assertEqual(row["cost_usd"], 0.0012)

    def test_union_merges_newest_first_by_ts(self):
        with open(os.environ["BUTTONS_LEDGER_PATH"], "w") as fh:
            fh.write(json.dumps({"kind": "run", "event": "old",
                                 "ts": 1600000000.0}) + "\n")
        self._write_cli([CLI_TURN])
        resp = self.client.get("/api/ledger")
        kinds = [e["kind"] for e in resp.json()["entries"]]
        self.assertEqual(kinds, ["cli-turn", "run"])

    def test_limit_applies_after_merge(self):
        with open(os.environ["BUTTONS_LEDGER_PATH"], "w") as fh:
            for ts in (1600000001.0, 1600000002.0):
                fh.write(json.dumps({"kind": "run", "ts": ts}) + "\n")
        self._write_cli([CLI_TURN])
        resp = self.client.get("/api/ledger", params={"limit": 2})
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["kind"], "cli-turn")

    def test_source_console_excludes_cli(self):
        self._write_cli([CLI_TURN])
        ledger.append({"kind": "run", "action": "x"})
        resp = self.client.get("/api/ledger",
                               params={"source": "console"})
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "run")

    def test_source_cli_returns_only_cli(self):
        self._write_cli([CLI_TURN])
        ledger.append({"kind": "run", "action": "x"})
        resp = self.client.get("/api/ledger", params={"source": "cli"})
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "cli-turn")

    def test_bad_source_is_400(self):
        resp = self.client.get("/api/ledger", params={"source": "nope"})
        self.assertEqual(resp.status_code, 400)

    def test_kind_cli_turn_is_allowlisted(self):
        self._write_cli([CLI_TURN])
        ledger.append({"kind": "run", "action": "x"})
        resp = self.client.get("/api/ledger",
                               params={"kind": "cli-turn"})
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "cli-turn")

    def test_missing_cli_file_yields_console_only(self):
        ledger.append({"kind": "run", "action": "x"})
        resp = self.client.get("/api/ledger")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["entries"]), 1)

    def test_corrupt_cli_lines_are_skipped(self):
        self._write_cli(["{not json\n", CLI_TURN, "[1, 2]\n",
                         json.dumps({"kind": "other"}) + "\n"])
        resp = self.client.get("/api/ledger")
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "cli-turn")

    def test_cli_since_filter(self):
        now = time.time()
        old = dict(CLI_TURN, ts=now - 90000)
        new = dict(CLI_TURN, ts=now - 10)
        self._write_cli([old, new])
        resp = self.client.get("/api/ledger",
                               params={"since": now - 3600})
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ts"], now - 10)

    def test_cli_path_parity_with_deep_agent(self):
        # backend/ledger duplicates deep-agent/ledger.py's path
        # resolution (hyphenated dir + sys.path games make the import
        # awkward); pin the two to each other.
        import importlib.util

        here = os.path.abspath(ledger.__file__)
        root = os.path.dirname(os.path.dirname(os.path.dirname(here)))
        deep_agent = os.path.join(root, "deep-agent")
        sys.path.insert(0, deep_agent)
        try:
            spec = importlib.util.spec_from_file_location(
                "cli_ledger_parity",
                os.path.join(deep_agent, "ledger.py"))
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.remove(deep_agent)

        def cli_resolve():
            return (os.environ.get("EVAL_LEDGER") or os.path.join(
                mod.state_dir(), "ledger.jsonl"))

        cases = [
            {"EVAL_LEDGER": "/tmp/eval.jsonl",
             "DEEP_AGENT_STATE_DIR": "/tmp/state"},
            {"DEEP_AGENT_STATE_DIR": "/tmp/state"},
            {},
        ]
        for case in cases:
            for k in ("EVAL_LEDGER", "DEEP_AGENT_STATE_DIR"):
                os.environ.pop(k, None)
            os.environ.update(case)
            # Re-exec so LEDGER rebinds (module constant); state_dir
            # reads env dynamically.
            spec.loader.exec_module(mod)
            self.assertEqual(ledger.cli_ledger_path(), cli_resolve(),
                             f"path drift for {case}")


def _kind_of_dict(node):
    """The "kind" string of a Dict literal, or None when absent or
    not a plain string."""
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values):
        if isinstance(key, ast.Constant) and key.value == "kind" \
                and isinstance(value, ast.Constant) \
                and isinstance(value.value, str):
            return value.value
    return None


def _written_ledger_kinds(backend_dir):
    """Every kind string passed to ledger.append across backend/
    (tests excluded), resolved statically: calls through names
    imported from backend.ledger (ledger_append, ledger.append)
    plus the defining module's own bare append (clear()'s
    tombstone). Record vars (record = {...}; ledger_append(record))
    resolve via same-scope Dict assignments. Returns
    (kinds, unresolved) with unresolved as file:line strings for
    calls whose kind is not a string literal."""
    kinds = set()
    unresolved = []
    for root, _dirs, files in os.walk(backend_dir):
        if os.path.basename(root) == "tests":
            continue
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
            append_names = set()
            has_ledger_module = False
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.module == "backend.ledger":
                    for alias in node.names:
                        if alias.name == "append":
                            append_names.add(alias.asname or alias.name)
                elif node.module == "backend":
                    for alias in node.names:
                        if alias.name == "ledger":
                            has_ledger_module = True
            if os.path.basename(path) == "__init__.py" and \
                    os.path.basename(os.path.dirname(path)) == "ledger":
                append_names.add("append")

            def visit(node, assigns):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                     ast.Module)):
                    assigns = dict(assigns)
                if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                        and isinstance(node.targets[0], ast.Name) \
                        and isinstance(node.value, ast.Dict):
                    assigns.setdefault(node.targets[0].id, []).append(
                        node.value)
                if isinstance(node, ast.Call):
                    func = node.func
                    is_ledger = (
                        isinstance(func, ast.Name)
                        and func.id in append_names
                    ) or (
                        isinstance(func, ast.Attribute)
                        and func.attr == "append"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "ledger"
                        and has_ledger_module
                    )
                    if is_ledger and node.args:
                        found = []
                        arg = node.args[0]
                        if isinstance(arg, ast.Dict):
                            found = [arg]
                        elif isinstance(arg, ast.Name):
                            found = assigns.get(arg.id, [])
                        kind = None
                        for cand in found:
                            kind = _kind_of_dict(cand) or kind
                        if kind is not None:
                            kinds.add(kind)
                        else:
                            unresolved.append(f"{path}:{node.lineno}")
                for child in ast.iter_child_nodes(node):
                    visit(child, assigns)

            visit(tree, {})
    return kinds, unresolved


class LedgerKindCoverageTest(unittest.TestCase):
    def test_every_written_kind_is_allowlisted(self):
        # Issue #457: the allowlist drifted from the writers
        # ("agent-turn" written, "agent" listed). Every kind string
        # passed to ledger.append must be queryable via LEDGER_KINDS.
        backend_dir = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        kinds, unresolved = _written_ledger_kinds(backend_dir)
        self.assertEqual(unresolved, [],
                         f"unresolvable ledger.append kinds: {unresolved}")
        self.assertIn("agent-turn", kinds,
                      "coverage scan missed the agent-turn writer")
        for kind in sorted(kinds):
            self.assertIn(kind, ledger.LEDGER_KINDS,
                          f"written kind {kind!r} not in LEDGER_KINDS")


if __name__ == "__main__":
    unittest.main()
