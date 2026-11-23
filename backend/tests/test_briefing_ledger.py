"""Tests for the briefing + ledger port (stdlib unittest only).

Uses a throwaway FastAPI app including the briefing router (server.py
is owned by another program slice, so registration there is out of
scope) and a tmp ledger dir via BUTTONS_LEDGER_PATH.
"""

import ast
import json
import os
import pathlib
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
    from backend.shared.cli import repo_root

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False


# Approve refuses blank required fields (#1585); these tests are about duplicates.
APPROVABLE_FIELDS = {"problem": "p", "scope": "s", "done_when": "d"}


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
    case.assertEqual(argv[2:], ["--briefing-json", "--repo", repo, "--gh", gh])


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class BriefingLedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp, "ledger.jsonl")
        self._old_eval_ledger = os.environ.get("EVAL_LEDGER")
        os.environ["EVAL_LEDGER"] = os.path.join(self.tmp, "cli-ledger.jsonl")
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
        if self._old_eval_ledger is None:
            os.environ.pop("EVAL_LEDGER", None)
        else:
            os.environ["EVAL_LEDGER"] = self._old_eval_ledger
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
        path = self._write_fixture(
            {
                "repo": "other",
                "attention": ["#1 — CI failing"],
                "prs": [{"number": 1, "title": "t"}],
                "failing_runs": [{"name": "ci"}],
            }
        )
        resp = self.client.get("/api/briefing", params={"repo": "remote-server"})
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
        return " ".join(str(a) for call in warn_mock.call_args_list for a in call[0])

    def test_fixture_serve_logs_warning(self):
        path = self._write_fixture({"repo": "other", "prs": []})
        with mock.patch.object(briefing_router.logger, "warning") as warn:
            resp = self.client.get("/api/briefing", params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["fixture"])
        self.assertTrue(warn.called)
        logged = self._warn_text(warn)
        self.assertIn(path, logged)
        self.assertIn("remote-server", logged)

    def test_stray_fixture_ignored_without_opt_in(self):
        """A stray next-to-checkouts file alone cannot shadow live."""
        stray = self._write_stray(
            {"repo": "other", "prs": [{"n": 1}], "attention": ["STRAY"]}
        )
        live = {"repo": "remote-server", "prs": [], "attention": []}
        with (
            mock.patch.object(
                briefing_router, "_run_briefing", return_value=dict(live)
            ) as run_mock,
            mock.patch.object(briefing_router.logger, "warning") as warn,
        ):
            resp = self.client.get("/api/briefing", params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["prs"], [])
        self.assertEqual(body["attention"], [])
        self.assertFalse(body["fixture"])
        self.assertEqual(body["fixture_source"], "")
        run_mock.assert_called_once_with("remote-server", "arii/remote-server")
        self.assertTrue(warn.called)
        logged = self._warn_text(warn)
        self.assertIn("stray", logged)
        self.assertIn(stray, logged)

    def test_auto_discover_opt_in_serves_stray_fixture(self):
        stray = self._write_stray({"repo": "other", "attention": ["STRAY"]})
        os.environ["BUTTONS_FIXTURE_AUTO_DISCOVER"] = "1"
        with (
            mock.patch.object(briefing_router, "_run_briefing") as run_mock,
            mock.patch.object(briefing_router.logger, "warning") as warn,
        ):
            resp = self.client.get("/api/briefing", params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["attention"], ["STRAY"])
        self.assertTrue(body["fixture"])
        self.assertEqual(body["fixture_source"], stray)
        run_mock.assert_not_called()
        self.assertIn("serving fixture", self._warn_text(warn))

    def test_missing_explicit_fixture_falls_through_to_live(self):
        os.environ["BUTTONS_FIXTURE_BRIEFING"] = os.path.join(
            self.tmp, "no-such-fixture.json"
        )
        live = {"repo": "remote-server", "prs": []}
        with (
            mock.patch.object(
                briefing_router, "_run_briefing", return_value=dict(live)
            ) as run_mock,
            mock.patch.object(briefing_router.logger, "warning") as warn,
        ):
            resp = self.client.get("/api/briefing", params={"repo": "remote-server"})
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
        return mock.patch.object(subprocess, "run", return_value=proc)

    def test_briefing_uses_json_despite_exit_1(self):
        payload = {"repo": "previous_workouts", "prs": [], "runs_err": "nope"}
        with self._fake_run(1, json.dumps(payload)) as run_mock:
            resp = self.client.get(
                "/api/briefing", params={"repo": "previous_workouts"}
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["runs_err"], "nope")
        _check_briefing_argv(
            self,
            run_mock.call_args[0][0],
            "previous_workouts",
            "arii/previous_workouts",
        )

    def test_briefing_fails_without_usable_stdout(self):
        with self._fake_run(1, "", "boom") as run_mock:
            resp = self.client.get("/api/briefing", params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 500)
        self.assertIn("boom", resp.json()["detail"])
        _check_briefing_argv(
            self, run_mock.call_args[0][0], "remote-server", "arii/remote-server"
        )

    def test_briefing_caches_within_ttl(self):
        payload = {"repo": "remote-server", "prs": [], "attention": []}
        with self._fake_run(0, json.dumps(payload)) as run_mock:
            first = self.client.get("/api/briefing", params={"repo": "remote-server"})
            second = self.client.get("/api/briefing", params={"repo": "remote-server"})
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(run_mock.call_count, 1)
        _check_briefing_argv(
            self, run_mock.call_args[0][0], "remote-server", "arii/remote-server"
        )
        self.assertFalse(first.json()["cached"])
        self.assertTrue(second.json()["cached"])
        self.assertFalse(second.json()["stale"])
        self.assertIn("generated_at", second.json())

    def test_briefing_serves_stale_and_revalidates(self):
        stale_payload = {"repo": "remote-server", "prs": [], "attention": ["old"]}
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time() - 120,
            stale_payload,
        )
        fresh = {"repo": "remote-server", "prs": [], "attention": ["new"]}
        fake_threading = mock.Mock()
        fake_threading.Thread = _SyncThread
        with (
            self._fake_run(0, json.dumps(fresh)),
            mock.patch.object(briefing_router, "threading", fake_threading),
        ):
            resp = self.client.get("/api/briefing", params={"repo": "remote-server"})
        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        # Served the stale entry immediately...
        self.assertTrue(body["cached"])
        self.assertTrue(body["stale"])
        self.assertEqual(body["attention"], ["old"])
        # ...and the background refresh stored the fresh payload.
        self.assertEqual(
            briefing_router._BRIEFING_CACHE["remote-server"][1]["attention"], ["new"]
        )
        self.assertNotIn("remote-server", briefing_router._BRIEFING_REFRESHING)

    def test_briefing_recomputes_past_max_stale(self):
        ancient = {"repo": "remote-server", "prs": [], "attention": ["old"]}
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time() - briefing_router.BRIEFING_CACHE_MAX_STALE - 1,
            ancient,
        )
        fresh = {"repo": "remote-server", "prs": [], "attention": ["new"]}
        with self._fake_run(0, json.dumps(fresh)) as run_mock:
            resp = self.client.get("/api/briefing", params={"repo": "remote-server"})
        body = resp.json()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(run_mock.call_count, 1)
        _check_briefing_argv(
            self, run_mock.call_args[0][0], "remote-server", "arii/remote-server"
        )
        self.assertFalse(body["cached"])
        self.assertFalse(body["stale"])
        self.assertEqual(body["attention"], ["new"])

    def test_briefing_refresh_failure_keeps_stale(self):
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time() - 120,
            {"attention": ["old"]},
        )
        briefing_router._BRIEFING_REFRESHING.add("remote-server")
        with mock.patch.object(
            briefing_router, "_run_briefing", side_effect=RuntimeError("gh down")
        ):
            briefing_router._refresh_briefing("remote-server", "arii/remote-server")
        self.assertEqual(
            briefing_router._BRIEFING_CACHE["remote-server"][1]["attention"], ["old"]
        )
        self.assertNotIn("remote-server", briefing_router._BRIEFING_REFRESHING)

    def test_status_shape(self):
        resp = self.client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIn("health", body)
        self.assertIn("runs", body)

    def test_ledger_round_trip(self):
        ledger.append({"kind": "run", "action": "briefing-td", "repo": "remote-server"})
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
        resp = self.client.get("/api/ledger", params={"kind": "agent-turn"})
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "agent-turn")
        self.assertEqual(entries[0]["session_id"], "s1")

    def test_ledger_intake_returns_only_latest_pending_row(self):
        ledger.append(
            {
                "kind": "ledger-intake",
                "canonical_intake_id": "intake-1",
                "status": "NEW",
                "title": "First",
            }
        )
        ledger.append(
            {
                "kind": "ledger-intake",
                "canonical_intake_id": "intake-1",
                "status": "READY",
                "title": "Approved",
            }
        )
        ledger.append(
            {
                "kind": "ledger-intake",
                "canonical_intake_id": "intake-2",
                "status": "NEEDS_INFO",
                "missing_fields": ["scope"],
            }
        )
        response = self.client.get("/api/ledger-intake")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["canonical_intake_id"] for row in response.json()["rows"]],
            ["intake-2"],
        )

    def test_ledger_intake_approval_appends_ready_row(self):
        ledger.append(
            {
                "kind": "ledger-intake",
                "canonical_intake_id": "intake-1",
                "status": "NEEDS_INFO",
                "missing_fields": ["scope"],
                "title": "Needs scope",
                "repo": "arii/remote-server",
                "problem": "p",
                "done_when": "d",
            }
        )
        response = self.client.post(
            "/api/ledger-intake/intake-1/approve",
            json={"fields": {"scope": "backend"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["row"]["status"], "READY")
        self.assertEqual(self.client.get("/api/ledger-intake").json()["rows"], [])

    def _intake_row(self, **overrides):
        row = {
            "kind": "ledger-intake",
            "canonical_intake_id": "intake-1",
            "status": "NEEDS_INFO",
            "title": "T",
            "repo": "arii/remote-server",
            "problem": "p",
            "scope": "s",
            "done_when": "d",
        }
        row.update(overrides)
        return row

    def _ledger_count(self):
        return len(ledger.read(kind="ledger-intake", limit=100))

    def test_approve_blank_required_field_returns_400_and_appends_nothing(self):
        for name in ("title", "repo", "problem", "scope", "done_when"):
            for missing in ([name], []):
                with self.subTest(name=name, missing_fields=missing):
                    ledger.clear()
                    ledger.append(
                        self._intake_row(**{name: "", "missing_fields": missing})
                    )
                    before = self._ledger_count()
                    response = self.client.post(
                        "/api/ledger-intake/intake-1/approve", json={"fields": {}}
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn(name, response.json()["detail"])
                    self.assertEqual(self._ledger_count(), before)
                    rows = self.client.get("/api/ledger-intake").json()["rows"]
                    self.assertEqual([r["status"] for r in rows], ["NEEDS_INFO"])

    def test_approve_with_all_required_fields_is_ready(self):
        ledger.append(self._intake_row())
        response = self.client.post(
            "/api/ledger-intake/intake-1/approve", json={"fields": {}}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["row"]["status"], "READY")

    def test_approve_accepts_human_supplied_blank_fields(self):
        ledger.append(self._intake_row(repo="", scope="", done_when=""))
        response = self.client.post(
            "/api/ledger-intake/intake-1/approve",
            json={"fields": {"repo": "arii/x", "scope": "a", "done_when": "b"}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["row"]["repo"], "arii/x")

    def test_intake_required_fields_mirror_scripts(self):
        from scripts import ledger_intake

        self.assertEqual(
            briefing_router.INTAKE_REQUIRED_FIELDS, ledger_intake.REQUIRED_FIELDS
        )

    def _flagged_row(self, **overrides):
        row = {
            "kind": "ledger-intake",
            "canonical_intake_id": "intake-risk",
            "status": "NEW",
            "title": "Deploy the backend",
            "repo": "arii/remote-server",
            "problem": "ship it",
            "scope": "backend",
            "done_when": "live",
            **overrides,
        }
        ledger.append(row)
        return row

    def test_ledger_intake_lists_needs_approval_with_categories(self):
        self._flagged_row(status="NEEDS_APPROVAL")
        self._flagged_row(
            canonical_intake_id="intake-new", status="NEW", title="Email customers"
        )
        rows = {
            row["canonical_intake_id"]: row
            for row in self.client.get("/api/ledger-intake").json()["rows"]
        }
        self.assertEqual(
            rows["intake-risk"]["risk_categories"], ["production_mutation"]
        )
        self.assertEqual(rows["intake-new"]["risk_categories"], ["external_comms"])

    def test_high_risk_approve_without_confirm_is_400_and_stays_pending(self):
        self._flagged_row(status="NEEDS_APPROVAL")
        for body in (
            {"fields": {}},
            {"fields": {}, "intent": "ledger-intake-approve-high-risk"},
            {"fields": {}, "intent": "wrong", "confirmed": True},
        ):
            response = self.client.post(
                "/api/ledger-intake/intake-risk/approve", json=body
            )
            self.assertEqual(response.status_code, 400, body)
        rows = self.client.get("/api/ledger-intake").json()["rows"]
        self.assertEqual([r["status"] for r in rows], ["NEEDS_APPROVAL"])

    def test_high_risk_approve_with_confirm_marks_ready(self):
        self._flagged_row(status="NEEDS_APPROVAL")
        response = self.client.post(
            "/api/ledger-intake/intake-risk/approve",
            json={
                "fields": {},
                "intent": "ledger-intake-approve-high-risk",
                "confirmed": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["row"]["status"], "READY")
        self.assertEqual(self.client.get("/api/ledger-intake").json()["rows"], [])

    def test_hook_written_new_row_is_gated_by_server_side_rederivation(self):
        self._flagged_row(status="NEW")
        response = self.client.post(
            "/api/ledger-intake/intake-risk/approve", json={"fields": {}}
        )
        self.assertEqual(response.status_code, 400)

    def test_high_risk_approve_still_refuses_blank_required_fields(self):
        self._flagged_row(status="NEEDS_APPROVAL", scope="")
        response = self.client.post(
            "/api/ledger-intake/intake-risk/approve",
            json={
                "fields": {},
                "intent": "ledger-intake-approve-high-risk",
                "confirmed": True,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("scope", response.json()["detail"])

    def test_unflagged_approval_needs_no_confirm(self):
        self._flagged_row(status="NEW", title="Tidy docs")
        response = self.client.post(
            "/api/ledger-intake/intake-risk/approve", json={"fields": {}}
        )
        self.assertEqual(response.status_code, 200)

    def test_ledger_intake_shows_candidate_and_merge_links_duplicate(self):
        ledger.append(
            {
                "kind": "ledger-intake",
                "intake_id": "intake-new",
                "canonical_intake_id": "intake-new",
                "status": "NEW",
                "title": "Retry policy",
                "repo": "o/r",
                "dedupe_hash": "same",
            }
        )
        ledger.append(
            {
                "kind": "ledger-intake",
                "intake_id": "intake-old",
                "canonical_intake_id": "intake-old",
                "status": "READY",
                "title": "Retry policy",
                "repo": "o/r",
                "dedupe_hash": "same",
                "ts": time.time() - 60,
            }
        )
        rows = self.client.get("/api/ledger-intake").json()["rows"]
        self.assertEqual(rows[0]["duplicate_candidate"]["title"], "Retry policy")
        response = self.client.post(
            "/api/ledger-intake/intake-new/merge",
            json={
                "candidate_id": "intake-old",
                "notes": "Same scope.",
                "intent": "ledger-intake-merge",
                "confirmed": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        merged = response.json()["row"]
        self.assertEqual(merged["status"], "DUPLICATE")
        self.assertEqual(merged["canonical_intake_id"], "intake-old")
        self.assertEqual(merged["notes"], "Same scope.")
        self.assertEqual(self.client.get("/api/ledger-intake").json()["rows"], [])

    def _seed_pair(self, new_hash="same", old_hash="same", new_title="Retry policy"):
        ledger.append(
            {
                "kind": "ledger-intake",
                "intake_id": "intake-new",
                "canonical_intake_id": "intake-new",
                "status": "NEW",
                "title": new_title,
                "repo": "o/r",
                "dedupe_hash": new_hash,
            }
        )
        ledger.append(
            {
                "kind": "ledger-intake",
                "intake_id": "intake-old",
                "canonical_intake_id": "intake-old",
                "status": "READY",
                "title": "Retry policy",
                "repo": "o/r",
                "dedupe_hash": old_hash,
                "ts": time.time() - 60,
            }
        )

    def _intake_count(self):
        return len([r for r in ledger.read(500) if r.get("kind") == "ledger-intake"])

    def test_intake_merge_requires_confirm_and_appends_nothing(self):
        # exact-hash and fuzzy candidates are both gated (#1584)
        for new_hash, old_hash in (("same", "same"), ("a", "b")):
            ledger.clear()
            self._seed_pair(new_hash, old_hash)
            before = self._intake_count()
            for body in (
                None,
                {"candidate_id": "intake-old"},
                {
                    "candidate_id": "intake-old",
                    "intent": "ledger-clear",
                    "confirmed": True,
                },
                {"candidate_id": "intake-old", "intent": "ledger-intake-merge"},
                {
                    "candidate_id": "intake-old",
                    "intent": "ledger-intake-merge",
                    "confirmed": False,
                },
            ):
                kwargs = {} if body is None else {"json": body}
                response = self.client.post(
                    "/api/ledger-intake/intake-new/merge", **kwargs
                )
                self.assertEqual(response.status_code, 400, (new_hash, body))
            self.assertEqual(self._intake_count(), before)

    def test_fuzzy_candidate_listing_never_appends(self):
        self._seed_pair("a", "b")
        before = self._intake_count()
        rows = self.client.get("/api/ledger-intake").json()["rows"]
        self.assertEqual(rows[0]["duplicate_candidate"]["title"], "Retry policy")
        self.client.get("/api/ledger-intake")
        self.assertEqual(self._intake_count(), before)

    def test_intake_distinct_gated_resets_new_and_is_not_reflagged(self):
        self._seed_pair()
        before = self._intake_count()
        for body in (
            None,
            {"candidate_id": "intake-old"},
            {
                "candidate_id": "intake-old",
                "intent": "ledger-intake-merge",
                "confirmed": True,
            },
        ):
            kwargs = {} if body is None else {"json": body}
            response = self.client.post(
                "/api/ledger-intake/intake-new/distinct", **kwargs
            )
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self._intake_count(), before)
        response = self.client.post(
            "/api/ledger-intake/intake-new/distinct",
            json={
                "candidate_id": "intake-old",
                "intent": "ledger-intake-distinct",
                "confirmed": True,
            },
        )
        self.assertEqual(response.status_code, 200)
        row = response.json()["row"]
        self.assertEqual(row["status"], "NEW")
        self.assertNotEqual(row["status"], "READY")
        self.assertEqual(row["duplicate_of"], "")
        self.assertEqual(row["canonical_intake_id"], "intake-new")
        rows = self.client.get("/api/ledger-intake").json()["rows"]
        self.assertEqual([r["intake_id"] for r in rows], ["intake-new"])
        self.assertNotIn("duplicate_candidate", rows[0])

    def test_approve_rejects_quarantined_duplicate_row(self):
        self._seed_pair()
        ledger.append(
            {
                "kind": "ledger-intake",
                "intake_id": "intake-new",
                "canonical_intake_id": "intake-new",
                "status": "DUPLICATE",
                "title": "Retry policy",
                "repo": "o/r",
                "dedupe_hash": "same",
            }
        )
        before = self._intake_count()
        response = self.client.post(
            "/api/ledger-intake/intake-new/approve", json={"fields": APPROVABLE_FIELDS}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._intake_count(), before)
        rows = self.client.get("/api/ledger-intake").json()["rows"]
        self.assertEqual(rows[0]["status"], "DUPLICATE")
        # after a human marks it distinct it is NEW and approvable
        self.client.post(
            "/api/ledger-intake/intake-new/distinct",
            json={
                "candidate_id": "intake-old",
                "intent": "ledger-intake-distinct",
                "confirmed": True,
            },
        )
        response = self.client.post(
            "/api/ledger-intake/intake-new/approve", json={"fields": APPROVABLE_FIELDS}
        )
        self.assertEqual(response.status_code, 200)

    def test_approve_does_not_scan_queue_for_duplicates(self):
        self._seed_pair()
        ledger.append(
            {
                "kind": "ledger-intake",
                "intake_id": "intake-other",
                "canonical_intake_id": "intake-other",
                "status": "NEW",
                "title": "Completely unrelated zebra",
                "repo": "x/y",
                "dedupe_hash": "zzz",
            }
        )
        with mock.patch(
            "backend.api.routers.briefing._duplicate_candidate"
        ) as candidate:
            response = self.client.post(
                "/api/ledger-intake/intake-other/approve",
                json={"fields": APPROVABLE_FIELDS},
            )
            missing = self.client.post(
                "/api/ledger-intake/nope/approve", json={"fields": APPROVABLE_FIELDS}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(missing.status_code, 404)
        candidate.assert_not_called()
        self.assertNotIn("duplicate_candidate", response.json()["row"])

    def test_distinct_rejects_candidate_that_is_not_detected(self):
        self._seed_pair()
        ledger.append(
            {
                "kind": "ledger-intake",
                "intake_id": "intake-other",
                "canonical_intake_id": "intake-other",
                "status": "NEW",
                "title": "Completely unrelated zebra",
                "repo": "x/y",
                "dedupe_hash": "zzz",
            }
        )
        before = self._intake_count()
        response = self.client.post(
            "/api/ledger-intake/intake-new/distinct",
            json={
                "candidate_id": "intake-other",
                "intent": "ledger-intake-distinct",
                "confirmed": True,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._intake_count(), before)

    def test_hook_marked_duplicate_row_visible_until_human_links_it(self):
        self._seed_pair()
        ledger.append(
            {
                "kind": "ledger-intake",
                "intake_id": "intake-new",
                "canonical_intake_id": "intake-new",
                "status": "DUPLICATE",
                "title": "Retry policy",
                "repo": "o/r",
                "dedupe_hash": "same",
            }
        )
        rows = self.client.get("/api/ledger-intake").json()["rows"]
        self.assertEqual([r["intake_id"] for r in rows], ["intake-new"])
        self.client.post(
            "/api/ledger-intake/intake-new/merge",
            json={
                "candidate_id": "intake-old",
                "intent": "ledger-intake-merge",
                "confirmed": True,
            },
        )
        self.assertEqual(self.client.get("/api/ledger-intake").json()["rows"], [])

    def test_ledger_intake_no_delete_path(self):
        root = str(repo_root())
        banned = {
            "clear",
            "delete",
            "remove",
            "unlink",
            "truncate",
            "rmtree",
            "tombstone",
        }
        sources = [
            os.path.join(root, "scripts", n)
            for n in os.listdir(os.path.join(root, "scripts"))
            if n.startswith("ledger_intake") and n.endswith(".py")
        ]
        for path in sources:
            tree = ast.parse(pathlib.Path(path).read_text())
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    fn = node.func
                    name = (
                        fn.attr
                        if isinstance(fn, ast.Attribute)
                        else getattr(fn, "id", "")
                    )
                    self.assertNotIn(name.lower(), banned, f"{path}:{node.lineno}")
        # In the router only ledger_clear_route may reach ledger clear,
        # and it is gated by require_confirm('ledger-clear').
        router_src = pathlib.Path(
            root, "backend", "api", "routers", "briefing.py"
        ).read_text()
        router = ast.parse(router_src)
        for fn in [n for n in ast.walk(router) if isinstance(n, ast.FunctionDef)]:
            src = ast.get_source_segment(router_src, fn)
            if "ledger-intake" in src or "intake" in fn.name:
                for node in ast.walk(fn):
                    if isinstance(node, ast.Call):
                        f = node.func
                        name = (
                            f.attr
                            if isinstance(f, ast.Attribute)
                            else getattr(f, "id", "")
                        )
                        self.assertNotIn(
                            name.lower(), banned | {"ledger_clear"}, fn.name
                        )
        # Direct file writes in the intake scripts would bypass the ledger.
        write_calls = {"write_text", "write_bytes", "writelines"}
        for path in sources:
            tree = ast.parse(pathlib.Path(path).read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (
                    fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                )
                self.assertNotIn(name, write_calls, f"{path}:{node.lineno}")
                if name == "open":
                    modes = [
                        a.value for a in node.args[1:2] if isinstance(a, ast.Constant)
                    ]
                    modes += [
                        k.value.value
                        for k in node.keywords
                        if k.arg == "mode" and isinstance(k.value, ast.Constant)
                    ]
                    for mode in modes:
                        self.assertFalse(
                            set(str(mode)) & set("wax+"), f"{path}:{node.lineno}"
                        )
        route = next(
            n
            for n in ast.walk(router)
            if isinstance(n, ast.FunctionDef) and n.name == "ledger_clear_route"
        )
        self.assertIn("ledger-clear", ast.get_source_segment(router_src, route))

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
            "/api/ledger/clear", json={"intent": "ledger-clear", "confirmed": True}
        )
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
        for kwargs in (
            {"json": {}},
            {"json": {"intent": "ledger-clear"}},
            {"json": {"intent": "ledger-clear", "confirmed": False}},
            {"json": {"intent": "jobs-clear", "confirmed": True}},
        ):
            resp = self.client.post("/api/ledger/clear", **kwargs)
            self.assertEqual(resp.status_code, 400, kwargs)
        # Bare POST with no body hits the same 400 gate (not 422).
        resp = self.client.post("/api/ledger/clear")
        self.assertEqual(resp.status_code, 400)
        kept = self.client.get("/api/ledger").json()["entries"]
        self.assertEqual([e.get("action") for e in kept], ["x"])
        # Confirmed POST clears + tombstones as before.
        resp = self.client.post("/api/ledger/clear", json=gate)
        self.assertEqual(resp.status_code, 200)
        entries = self.client.get("/api/ledger").json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["event"], "ledger cleared")


class FixtureResolveTest(unittest.TestCase):
    """Resolver rules for the briefing fixture override (#453)."""

    def test_explicit_env_wins_without_opt_in(self):
        from backend.core.env_config import resolve_briefing_fixture_path

        with mock.patch.dict(
            os.environ,
            {
                "BUTTONS_FIXTURE_BRIEFING": " /x/y.json ",
            },
        ):
            os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)
            # Stripped, verbatim, no existence check here — the
            # caller falls through to live with a warning.
            self.assertEqual(resolve_briefing_fixture_path(), "/x/y.json")

    def test_candidate_gated_behind_opt_in(self):
        from backend.core.env_config import (
            briefing_fixture_candidate,
            resolve_briefing_fixture_path,
        )

        with tempfile.TemporaryDirectory() as tmp:
            stray = os.path.join(tmp, "fixture-briefing.json")
            with open(stray, "w", encoding="utf-8") as fh:
                json.dump({"prs": []}, fh)
            with mock.patch.dict(os.environ, {"BUTTONS_REPOS_DIR": tmp}):
                os.environ.pop("BUTTONS_FIXTURE_BRIEFING", None)
                os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)
                self.assertEqual(briefing_fixture_candidate(), stray)
                self.assertEqual(resolve_briefing_fixture_path(), "")
                os.environ["BUTTONS_FIXTURE_AUTO_DISCOVER"] = "true"
                self.assertEqual(resolve_briefing_fixture_path(), stray)

    def test_opt_in_truthy_values(self):
        from backend.core.env_config import fixture_auto_discover_enabled

        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            with mock.patch.dict(os.environ, {"BUTTONS_FIXTURE_AUTO_DISCOVER": value}):
                self.assertTrue(fixture_auto_discover_enabled(), value)
        for value in ("", "0", "false", "no", "off", "maybe"):
            with mock.patch.dict(os.environ, {"BUTTONS_FIXTURE_AUTO_DISCOVER": value}):
                self.assertFalse(fixture_auto_discover_enabled(), value)

    def test_nothing_configured_resolves_empty(self):
        from backend.core.env_config import resolve_briefing_fixture_path

        with mock.patch.dict(os.environ, {}):
            os.environ.pop("BUTTONS_FIXTURE_BRIEFING", None)
            os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)
            os.environ["BUTTONS_REPOS_DIR"] = os.path.join(
                tempfile.mkdtemp(), "no-such-dir"
            )
            self.assertEqual(resolve_briefing_fixture_path(), "")


class BriefingScriptSmokeTest(unittest.TestCase):
    """The router shells out to backend/briefing_summary.py; it must import.

    Regression: the buttons image shipped the script without its sibling
    context_filter.py, so /api/briefing 500'd in prod while the
    fixture-based router tests stayed green.
    """

    def test_script_imports_resolve(self):
        script = os.path.join("backend", "briefing_summary.py")
        # No args -> usage to stderr, exit 2 (the script has no --help).
        # Any ModuleNotFoundError would traceback and exit 1 instead.
        proc = subprocess.run(
            [sys.executable, script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
            text=True,
        )
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
            briefing_router.subprocess, "run", return_value=proc
        ) as run_mock:
            out = briefing_router._run_briefing("x", "arii/x")
        self.assertEqual(out, {"repo": "x"})
        self.assertEqual(run_mock.call_args.kwargs.get("cwd"), stack_dir())
        _check_briefing_argv(self, run_mock.call_args.args[0], "x", "arii/x")


CLI_TURN = {
    "kind": "deep-agent-run",
    "ts": 1700000000.0,
    "script": "ask",
    "model": "qwen",
    "thread_id": "t1",
    "duration_s": 12.5,
    "tool_calls": [{"name": "shell", "args": "ls"}],
    "tool_results": [],
    "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150},
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
        self._old = {
            k: os.environ.get(k)
            for k in ("BUTTONS_LEDGER_PATH", "EVAL_LEDGER", "DEEP_AGENT_STATE_DIR")
        }
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp, "ledger.jsonl")
        os.environ["EVAL_LEDGER"] = os.path.join(self.tmp, "cli-ledger.jsonl")
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
                fh.write(row if isinstance(row, str) else json.dumps(row) + "\n")

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
            fh.write(
                json.dumps({"kind": "run", "event": "old", "ts": 1600000000.0}) + "\n"
            )
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
        resp = self.client.get("/api/ledger", params={"source": "console"})
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
        resp = self.client.get("/api/ledger", params={"kind": "cli-turn"})
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
        self._write_cli(
            ["{not json\n", CLI_TURN, "[1, 2]\n", json.dumps({"kind": "other"}) + "\n"]
        )
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
        resp = self.client.get("/api/ledger", params={"since": now - 3600})
        self.assertEqual(resp.status_code, 200)
        entries = resp.json()["entries"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["ts"], now - 10)

    def test_cli_path_parity_with_deep_agent(self):
        # backend/ledger duplicates backend/shared/ledger.py's path
        # resolution; pin the two to each other.
        from backend.shared import ledger as mod

        def cli_resolve():
            return os.environ.get("EVAL_LEDGER") or os.path.join(
                mod.state_dir(), "ledger.jsonl"
            )

        cases = [
            {"EVAL_LEDGER": "/tmp/eval.jsonl", "DEEP_AGENT_STATE_DIR": "/tmp/state"},
            {"DEEP_AGENT_STATE_DIR": "/tmp/state"},
            {},
        ]
        for case in cases:
            for k in ("EVAL_LEDGER", "DEEP_AGENT_STATE_DIR"):
                os.environ.pop(k, None)
            os.environ.update(case)
            self.assertEqual(
                ledger.cli_ledger_path(), cli_resolve(), f"path drift for {case}"
            )


def _kind_of_dict(node):
    """The "kind" string of a Dict literal, or None when absent or
    not a plain string."""
    if not isinstance(node, ast.Dict):
        return None
    for key, value in zip(node.keys, node.values):
        if (
            isinstance(key, ast.Constant)
            and key.value == "kind"
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
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
            if (
                os.path.basename(path) == "__init__.py"
                and os.path.basename(os.path.dirname(path)) == "ledger"
            ):
                append_names.add("append")

            def visit(node, assigns):
                if isinstance(
                    node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)
                ):
                    assigns = dict(assigns)
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Dict)
                ):
                    assigns.setdefault(node.targets[0].id, []).append(node.value)
                if isinstance(node, ast.Call):
                    func = node.func
                    is_ledger = (
                        isinstance(func, ast.Name) and func.id in append_names
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
        kinds, unresolved = _written_ledger_kinds("backend")
        self.assertEqual(
            unresolved, [], f"unresolvable ledger.append kinds: {unresolved}"
        )
        self.assertIn("agent-turn", kinds, "coverage scan missed the agent-turn writer")
        for kind in sorted(kinds):
            self.assertIn(
                kind, ledger.LEDGER_KINDS, f"written kind {kind!r} not in LEDGER_KINDS"
            )


if __name__ == "__main__":
    unittest.main()
