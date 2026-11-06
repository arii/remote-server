"""Tests for the attention queue + confirm helper (stdlib unittest)."""

import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone

from backend.core import require_confirm
from backend.api.routers import attention


class RequireConfirmTest(unittest.TestCase):
    def test_accept(self):
        require_confirm({"intent": "x", "confirmed": True}, "x")

    def test_reject_wrong_intent(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            require_confirm({"intent": "y", "confirmed": True}, "x")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_reject_unconfirmed(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            require_confirm({"intent": "x", "confirmed": False}, "x")


class AttentionShapeTest(unittest.TestCase):
    def _patch(self, **kw):
        orig = {}
        for k, v in kw.items():
            orig[k] = getattr(attention, k)
            setattr(attention, k, v)
        return orig

    def _restore(self, orig):
        for k, v in orig.items():
            setattr(attention, k, v)

    def test_empty_is_all_clear(self):
        orig = self._patch(
            get_workflow_health=lambda repo: {"workflows": []},
            list_failed_jobs=lambda: [],
            list_failed_webhooks=lambda: [],
            list_skipped_webhooks=lambda: [],
            list_pending_approvals=lambda: [],
            ollama_up=lambda: True,
        )
        try:
            body = attention.attention("remote-server")
        finally:
            self._restore(orig)
        self.assertIn("updated_at", body)
        self.assertEqual(body["items"], [])

    def test_each_kind(self):
        orig = self._patch(
            get_workflow_health=lambda repo: {"workflows": [{
                "id": 1, "name": "CI", "diagnosed_cause": "boom"}]},
            list_failed_jobs=lambda: [{
                "id": "j1", "name": "n", "state": "failed",
                "created_at": 1.0}],
            list_failed_webhooks=lambda: [{
                "id": "w1", "ts": 2.0, "event": "delivery failed",
                "error": "timeout"}],
            list_skipped_webhooks=lambda: [{
                "id": "s1", "ts": 2.5, "ok": True, "advanced": False,
                "repo": "remote-server",
                "detail": "skipped: tree dirty"}],
            list_pending_approvals=lambda: [{
                "id": "a1", "ts": 3.0, "title": "Approve?",
                "event": "approval requested"}],
            ollama_up=lambda: True,
        )
        try:
            body = attention.attention("remote-server")
        finally:
            self._restore(orig)
        kinds = {i["kind"] for i in body["items"]}
        self.assertEqual(
            kinds, {"failing-run", "failed-job", "failed-webhook",
                    "skipped-webhook", "pending-approval"})
        for item in body["items"]:
            for f in ("id", "kind", "title", "detail", "repo",
                      "severity", "action", "created_at"):
                self.assertIn(f, item)
            self.assertIn(item["severity"], ("info", "warn", "crit"))
            for f in ("label", "method", "href", "destructive", "intent"):
                self.assertIn(f, item["action"])

    def test_bad_repo(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            attention.attention("a/b/c")

    def test_identical_failing_runs_collapse(self):
        wfs = [{"id": i, "name": "CI", "diagnosed_cause": "boom"}
               for i in (1, 2, 3)]
        orig = self._patch(
            get_workflow_health=lambda repo: {"workflows": wfs},
            list_failed_jobs=lambda: [],
            list_failed_webhooks=lambda: [],
            list_skipped_webhooks=lambda: [],
            list_pending_approvals=lambda: [],
            ollama_up=lambda: True,
        )
        try:
            body = attention.attention("remote-server")
        finally:
            self._restore(orig)
        runs = [i for i in body["items"] if i["kind"] == "failing-run"]
        self.assertEqual(len(runs), 1)
        self.assertIn("×3", runs[0]["title"])

    def test_resolved_webhook_failures_hidden(self):
        import backend.ledger as ledger_mod
        orig_read = ledger_mod.read
        ledger_mod.read = lambda kind="all", limit=50: [
            {"id": "w-ok", "ts": 3.0, "ok": True, "advanced": True,
             "repo": "arii/remote-server", "event": "pull ok"},
            {"id": "w-old", "ts": 1.0, "ok": False,
             "repo": "arii/remote-server", "error": "timeout"},
            {"id": "w-new", "ts": 5.0, "ok": False,
             "repo": "arii/remote-server", "error": "timeout"},
            {"id": "w-cross", "ts": 1.0, "ok": False,
             "repo": "arii/boomtick", "error": "timeout"},
        ]
        try:
            out = attention.list_failed_webhooks()
        finally:
            ledger_mod.read = orig_read
        # Same-repo old failure resolved; cross-repo failure kept:
        # a remote-server success says nothing about boomtick.
        self.assertEqual(
            sorted(e["id"] for e in out), ["w-cross", "w-new"])

    def test_identical_failed_webhooks_collapse(self):
        hooks = [
            {"id": "w1", "ts": 1.0, "error": "timeout"},
            {"id": "w2", "ts": 2.0, "error": "timeout"},
            {"id": "w3", "ts": 3.0, "error": "refused"},
        ]
        orig = self._patch(
            get_workflow_health=lambda repo: {"workflows": []},
            list_failed_jobs=lambda: [],
            list_failed_webhooks=lambda: hooks,
            list_skipped_webhooks=lambda: [],
            list_pending_approvals=lambda: [],
            ollama_up=lambda: True,
        )
        try:
            body = attention.attention("remote-server")
        finally:
            self._restore(orig)
        items = [i for i in body["items"]
                 if i["kind"] == "failed-webhook"]
        self.assertEqual(len(items), 2)
        by_detail = {i["detail"]: i for i in items}
        self.assertIn("×2", by_detail["timeout"]["title"])
        self.assertEqual(
            by_detail["refused"]["title"], "Failed webhook delivery")

    def test_webhook_detail_reads_writer_detail_key(self):
        hooks = [
            {"id": "w1", "ts": 1.0, "ok": False,
             "detail": "fatal: not possible to fast-forward"},
        ]
        orig = self._patch(
            get_workflow_health=lambda repo: {"workflows": []},
            list_failed_jobs=lambda: [],
            list_failed_webhooks=lambda: hooks,
            list_skipped_webhooks=lambda: [],
            list_pending_approvals=lambda: [],
            ollama_up=lambda: True,
        )
        try:
            body = attention.attention("remote-server")
        finally:
            self._restore(orig)
        items = [i for i in body["items"]
                 if i["kind"] == "failed-webhook"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["detail"],
                         "fatal: not possible to fast-forward")

    def test_skip_does_not_resolve_failure(self):
        # Regression: a skip (ok=True, advanced=False) after a
        # genuine failure must leave the failure incident open —
        # the checkout did not advance.
        import backend.ledger as ledger_mod
        orig_read = ledger_mod.read
        ledger_mod.read = lambda kind="all", limit=50: [
            {"id": "w-skip", "ts": 5.0, "ok": True,
             "advanced": False, "repo": "arii/remote-server",
             "detail": "skipped: tree dirty"},
            {"id": "w-fail", "ts": 1.0, "ok": False,
             "advanced": False, "repo": "arii/remote-server",
             "error": "fetch failed"},
        ]
        try:
            out = attention.list_failed_webhooks()
        finally:
            ledger_mod.read = orig_read
        self.assertEqual([e["id"] for e in out], ["w-fail"])

    def test_current_noop_does_not_resolve_failure(self):
        # An already-current pull records ok=True with
        # advanced=False; like a skip it proves nothing about the
        # earlier failure, so the incident stays open.
        import backend.ledger as ledger_mod
        orig_read = ledger_mod.read
        ledger_mod.read = lambda kind="all", limit=50: [
            {"id": "w-noop", "ts": 5.0, "ok": True,
             "advanced": False, "repo": "arii/remote-server",
             "detail": "up to date"},
            {"id": "w-fail", "ts": 1.0, "ok": False,
             "advanced": False, "repo": "arii/remote-server",
             "error": "fetch failed"},
        ]
        try:
            out = attention.list_failed_webhooks()
        finally:
            ledger_mod.read = orig_read
        self.assertEqual([e["id"] for e in out], ["w-fail"])

    def test_skipped_webhooks_newest_per_repo(self):
        import backend.ledger as ledger_mod
        orig_read = ledger_mod.read
        ledger_mod.read = lambda kind="all", limit=200: [
            {"id": "s-old", "ts": 1.0, "ok": True,
             "advanced": False, "repo": "r1",
             "detail": "skipped: tree dirty"},
            {"id": "s-new", "ts": 2.0, "ok": True,
             "advanced": False, "repo": "r1",
             "detail": "skipped: tree not ff-able"},
            {"id": "s-other", "ts": 3.0, "ok": True,
             "advanced": False, "repo": "r2",
             "detail": "skipped: tree dirty"},
            # Already-current no-op: ok without advance, but not a
            # skip — never surfaces as a skip item.
            {"id": "n-noop", "ts": 4.0, "ok": True,
             "advanced": False, "repo": "r3", "detail": "up to date"},
            # Advancing pull: clears r2's older skip.
            {"id": "w-ok", "ts": 5.0, "ok": True,
             "advanced": True, "repo": "r2", "detail": "merged"},
        ]
        try:
            out = attention.list_skipped_webhooks()
        finally:
            ledger_mod.read = orig_read
        by_repo = {e["repo"]: e for e in out}
        self.assertEqual(by_repo["r1"]["id"], "s-new")
        self.assertNotIn("r2", by_repo)
        self.assertNotIn("r3", by_repo)

    def test_skip_surfaces_as_info_item(self):
        skips = [
            {"id": "s1", "ts": 2.0, "ok": True, "advanced": False,
             "repo": "boomtick", "detail": "skipped: tree dirty"},
        ]
        orig = self._patch(
            get_workflow_health=lambda repo: {"workflows": []},
            list_failed_jobs=lambda: [],
            list_failed_webhooks=lambda: [],
            list_skipped_webhooks=lambda: skips,
            list_pending_approvals=lambda: [],
            ollama_up=lambda: True,
        )
        try:
            body = attention.attention("boomtick")
        finally:
            self._restore(orig)
        items = [i for i in body["items"]
                 if i["kind"] == "skipped-webhook"]
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["severity"], "info")
        self.assertEqual(item["repo"], "boomtick")
        self.assertIn("skipped: tree dirty", item["detail"])
        self.assertTrue(item["action"]["label"])
        self.assertTrue(item["action"]["href"])

    def test_single_repo_filters_global_sources(self):
        # Issue #723: a single-repo feed carries only its own
        # items — foreign failed jobs/webhooks/skips/approvals
        # stay out, in both repo spellings. Legacy records with
        # no repo stay visible under the queried repo.
        orig = self._patch(
            get_workflow_health=lambda repo: {"workflows": []},
            list_failed_jobs=lambda: [
                {"id": "own", "name": "n", "state": "failed",
                 "repo": "remote-server", "created_at": 1.0},
                {"id": "foreign", "name": "n", "state": "failed",
                 "repo": "boomtick", "created_at": 1.0},
                {"id": "legacy", "name": "n", "state": "failed",
                 "created_at": 1.0},
            ],
            list_failed_webhooks=lambda: [
                {"id": "w-own", "ts": 2.0, "ok": False,
                 "repo": "arii/remote-server", "error": "timeout"},
                {"id": "w-foreign", "ts": 2.0, "ok": False,
                 "repo": "arii/boomtick", "error": "timeout"},
            ],
            list_skipped_webhooks=lambda: [
                {"id": "s-own", "ts": 2.5, "ok": True,
                 "advanced": False, "repo": "remote-server",
                 "detail": "skipped: tree dirty"},
                {"id": "s-foreign", "ts": 2.5, "ok": True,
                 "advanced": False, "repo": "boomtick",
                 "detail": "skipped: tree dirty"},
            ],
            list_pending_approvals=lambda: [
                {"id": "a-own", "ts": 3.0, "title": "Approve?",
                 "repo": "remote-server",
                 "event": "approval requested"},
                {"id": "a-foreign", "ts": 3.0, "title": "Approve?",
                 "repo": "boomtick",
                 "event": "approval requested"},
            ],
            ollama_up=lambda: True,
        )
        try:
            body = attention.attention("remote-server")
        finally:
            self._restore(orig)
        items = body["items"]
        self.assertTrue(items)
        for item in items:
            self.assertEqual(item["repo"], "remote-server")
        ids = {i["id"] for i in items}
        self.assertIn("job-own", ids)
        self.assertIn("job-legacy", ids)
        self.assertNotIn("job-foreign", ids)
        self.assertTrue(any(i["kind"] == "failed-webhook"
                            for i in items))
        self.assertTrue(any(i["kind"] == "skipped-webhook"
                            for i in items))
        self.assertTrue(any(i["kind"] == "pending-approval"
                            for i in items))
        # Symmetric: the foreign repo sees its own rows only.
        orig = self._patch(
            get_workflow_health=lambda repo: {"workflows": []},
            list_failed_jobs=lambda: [
                {"id": "own", "name": "n", "state": "failed",
                 "repo": "remote-server", "created_at": 1.0},
                {"id": "foreign", "name": "n", "state": "failed",
                 "repo": "boomtick", "created_at": 1.0},
            ],
            list_failed_webhooks=lambda: [],
            list_skipped_webhooks=lambda: [
                {"id": "s-own", "ts": 2.5, "ok": True,
                 "advanced": False, "repo": "remote-server",
                 "detail": "skipped: tree dirty"},
                {"id": "s-foreign", "ts": 2.5, "ok": True,
                 "advanced": False, "repo": "boomtick",
                 "detail": "skipped: tree dirty"},
            ],
            list_pending_approvals=lambda: [],
            ollama_up=lambda: True,
        )
        try:
            body = attention.attention("boomtick")
        finally:
            self._restore(orig)
        ids = {i["id"] for i in body["items"]}
        self.assertIn("job-foreign", ids)
        self.assertNotIn("job-own", ids)
        self.assertEqual(
            {i["id"] for i in body["items"]
             if i["kind"] == "skipped-webhook"},
            {"webhook-skip-s-foreign"})


class DispatchTerminalTest(unittest.TestCase):
    """Terminal dispatch surfacing (issue #883): done rows info,
    failed rows warn, queued rows silent, foreign repos filtered."""

    def _patch(self, **kw):
        orig = {}
        for k, v in kw.items():
            orig[k] = getattr(attention, k)
            setattr(attention, k, v)
        return orig

    def _restore(self, orig):
        for k, v in orig.items():
            setattr(attention, k, v)

    def _quiet(self, **kw):
        base = dict(
            get_workflow_health=lambda repo: {"workflows": []},
            list_failed_jobs=lambda: [],
            list_failed_webhooks=lambda: [],
            list_skipped_webhooks=lambda: [],
            list_pending_approvals=lambda: [],
            ollama_up=lambda: True,
        )
        base.update(kw)
        return self._patch(**base)

    def test_done_info_failed_warn_queued_silent(self):
        orig = self._quiet(list_terminal_dispatches=lambda: [
            {"repo": "remote-server", "issue": 1, "state": "done",
             "title": "Fix it", "detail": "repair worker finished",
             "finished_at": 10.0},
            {"repo": "remote-server", "issue": 2, "state": "failed",
             "title": "", "detail": "repair worker failed (exit 3)",
             "finished_at": 11.0},
            {"repo": "remote-server", "issue": 3,
             "state": "queued"},
        ])
        try:
            body = attention.attention("remote-server")
        finally:
            self._restore(orig)
        by_id = {i["id"]: i for i in body["items"]}
        self.assertEqual(sorted(by_id),
                         ["dispatch-remote-server-1",
                          "dispatch-remote-server-2"])
        done = by_id["dispatch-remote-server-1"]
        self.assertEqual(done["kind"], "completed-dispatch")
        self.assertEqual(done["severity"], "info")
        self.assertEqual(done["title"],
                         "Dispatch done: remote-server#1 — Fix it")
        self.assertEqual(done["detail"], "repair worker finished")
        self.assertEqual(done["action"]["href"], "api/dispatch/queue")
        self.assertEqual(done["created_at"], 10.0)
        failed = by_id["dispatch-remote-server-2"]
        self.assertEqual(failed["kind"], "failed-dispatch")
        self.assertEqual(failed["severity"], "warn")
        self.assertEqual(failed["title"],
                         "Dispatch failed: remote-server#2")

    def test_foreign_repos_filtered(self):
        orig = self._quiet(list_terminal_dispatches=lambda: [
            {"repo": "tech-dancer", "issue": 9, "state": "done",
             "finished_at": 10.0},
        ])
        try:
            body = attention.attention("remote-server")
        finally:
            self._restore(orig)
        self.assertEqual(body["items"], [])

    def test_seam_reads_terminal_states_only(self):
        from unittest.mock import patch

        from backend import lane_dispatch

        rows = [
            {"repo": "r", "issue": 1, "state": "done"},
            {"repo": "r", "issue": 2, "state": "failed"},
            {"repo": "r", "issue": 3, "state": "queued"},
            {"repo": "r", "issue": 4, "state": "running"},
        ]
        with patch.object(lane_dispatch, "queue_list",
                          return_value=rows):
            out = attention.list_terminal_dispatches()
        self.assertEqual([i["issue"] for i in out], [1, 2])


class SnoozeAttentionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._old_path = os.environ.get("BUTTONS_SNOOZE_PATH")
        os.environ["BUTTONS_SNOOZE_PATH"] = os.path.join(
            self._tmp, "snooze.json")
        self._orig = {}
        for k, v in {
            "get_workflow_health": lambda repo: {"workflows": []},
            "list_failed_jobs": lambda: [
                {"id": "j1", "name": "n", "state": "failed",
                 "created_at": 1.0}],
            "list_failed_webhooks": lambda: [],
            "list_skipped_webhooks": lambda: [],
            "list_pending_approvals": lambda: [],
            "ollama_up": lambda: True,
        }.items():
            self._orig[k] = getattr(attention, k)
            setattr(attention, k, v)

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(attention, k, v)
        if self._old_path is None:
            os.environ.pop("BUTTONS_SNOOZE_PATH", None)
        else:
            os.environ["BUTTONS_SNOOZE_PATH"] = self._old_path

    def _ids(self):
        return [i["id"] for i in
                attention.attention("remote-server")["items"]]

    def _store_file(self):
        with open(os.environ["BUTTONS_SNOOZE_PATH"],
                  encoding="utf-8") as f:
            return json.load(f)

    def test_snooze_hides_immediately(self):
        self.assertEqual(self._ids(), ["job-j1"])
        res = attention.snooze_attention(
            attention.SnoozeRequest(id="job-j1", minutes=60))
        self.assertTrue(res["ok"])
        self.assertEqual(res["id"], "job-j1")
        self.assertGreater(res["snoozed_until"], time.time())
        self.assertEqual(self._ids(), [])

    def test_unknown_id_404s(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as ctx:
            attention.snooze_attention(
                attention.SnoozeRequest(id="job-nope", minutes=60))
        self.assertEqual(ctx.exception.status_code, 404)

    def test_invalid_minutes_400s(self):
        from fastapi import HTTPException
        for minutes in (0, 30, -60):
            with self.assertRaises(HTTPException) as ctx:
                attention.snooze_attention(
                    attention.SnoozeRequest(id="job-j1",
                                           minutes=minutes))
            self.assertEqual(ctx.exception.status_code, 400)
        # Both presets accepted.
        for minutes in (60, 480):
            res = attention.snooze_attention(
                attention.SnoozeRequest(id="job-j1", minutes=minutes))
            self.assertTrue(res["ok"])

    def test_unsnooze_restores_and_is_idempotent(self):
        attention.snooze_attention(
            attention.SnoozeRequest(id="job-j1", minutes=60))
        self.assertEqual(self._ids(), [])
        res = attention.unsnooze_attention(
            attention.UnsnoozeRequest(id="job-j1"))
        self.assertTrue(res["ok"])
        self.assertFalse(res["snoozed"])
        self.assertEqual(self._ids(), ["job-j1"])
        # Second unsnooze of a non-snoozed id still succeeds.
        res = attention.unsnooze_attention(
            attention.UnsnoozeRequest(id="job-j1"))
        self.assertTrue(res["ok"])
        self.assertEqual(self._ids(), ["job-j1"])

    def test_expiry_resurfaces_and_prunes(self):
        attention.snooze_attention(
            attention.SnoozeRequest(id="job-j1", minutes=60))
        self.assertEqual(self._ids(), [])
        # Time-travel: rewrite the entry as expired.
        with open(os.environ["BUTTONS_SNOOZE_PATH"], "w",
                  encoding="utf-8") as f:
            json.dump({"job-j1": {"until": time.time() - 1,
                                  "title": "t",
                                  "repo": "remote-server"}}, f)
        self.assertEqual(self._ids(), ["job-j1"])
        self.assertNotIn("job-j1", self._store_file())

    def test_store_survives_reread(self):
        until = time.time() + 3600
        with open(os.environ["BUTTONS_SNOOZE_PATH"], "w",
                  encoding="utf-8") as f:
            json.dump({"job-j1": {"until": until, "title": "t",
                                  "repo": "remote-server"}}, f)
        # A fresh read (as after a restart) still filters the id.
        self.assertEqual(self._ids(), [])

    def test_snooze_persists_title_for_stub(self):
        attention.snooze_attention(
            attention.SnoozeRequest(id="job-j1", minutes=60))
        row = self._store_file()["job-j1"]
        self.assertIsInstance(row, dict)
        self.assertGreater(row["until"], time.time())
        self.assertTrue(row["title"])
        self.assertEqual(row["repo"], "remote-server")

    def test_list_snoozed_round_trip(self):
        self.assertEqual(attention.list_snoozed(), {"snoozed": []})
        attention.snooze_attention(
            attention.SnoozeRequest(id="job-j1", minutes=60))
        rows = attention.list_snoozed()["snoozed"]
        self.assertEqual([r["id"] for r in rows], ["job-j1"])
        self.assertTrue(rows[0]["title"])
        self.assertGreater(rows[0]["until"], time.time())
        attention.unsnooze_attention(
            attention.UnsnoozeRequest(id="job-j1"))
        self.assertEqual(attention.list_snoozed(), {"snoozed": []})

    def test_list_snoozed_prunes_expired(self):
        with open(os.environ["BUTTONS_SNOOZE_PATH"], "w",
                  encoding="utf-8") as f:
            json.dump({"job-j1": {"until": time.time() - 1,
                                  "title": "t",
                                  "repo": "remote-server"}}, f)
        self.assertEqual(attention.list_snoozed(), {"snoozed": []})
        self.assertNotIn("job-j1", self._store_file())

    def test_malformed_rows_are_dropped(self):
        with open(os.environ["BUTTONS_SNOOZE_PATH"], "w",
                  encoding="utf-8") as f:
            json.dump({"job-j1": 9999999999.0,
                       "other": {"title": "no until"}}, f)
        # Neither a bare float nor an until-less dict may hide cards.
        self.assertEqual(self._ids(), ["job-j1"])
        self.assertEqual(attention.list_snoozed(), {"snoozed": []})

    def test_corrupt_store_reads_empty(self):
        with open(os.environ["BUTTONS_SNOOZE_PATH"], "w",
                  encoding="utf-8") as f:
            f.write("not json{{{")
        self.assertEqual(self._ids(), ["job-j1"])


class WorkflowHealthCacheTest(unittest.TestCase):
    def test_second_call_served_from_cache(self):
        calls = []
        orig = attention.ops.fetch_workflow_health
        attention.ops.fetch_workflow_health = (
            lambda repo, gh: calls.append((repo, gh)) or {"ok": True})
        attention._WF_CACHE.clear()
        try:
            first = attention.get_workflow_health("remote-server")
            second = attention.get_workflow_health("remote-server")
            self.assertEqual(first, {"ok": True})
            self.assertEqual(second, {"ok": True})
            self.assertEqual(len(calls), 1)
        finally:
            attention.ops.fetch_workflow_health = orig
            attention._WF_CACHE.clear()


class OllamaOutageTest(unittest.TestCase):
    def setUp(self):
        self._orig = {}
        for k, v in {
            "get_workflow_health": lambda repo: {"workflows": []},
            "list_failed_jobs": lambda: [],
            "list_failed_webhooks": lambda: [],
            "list_skipped_webhooks": lambda: [],
            "list_pending_approvals": lambda: [],
            "failover_budget": lambda: {
                "cap_usd": 5.0, "spent_usd": 0.0,
                "remaining_usd": 5.0, "blocked": False,
                "unknown": 0},
        }.items():
            self._orig[k] = getattr(attention, k)
            setattr(attention, k, v)
        self._orig_up = attention.ollama_up
        attention._OLLAMA_CACHE.clear()

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(attention, k, v)
        attention.ollama_up = self._orig_up
        attention._OLLAMA_CACHE.clear()

    def _outage_items(self):
        return [i for i in attention.attention("remote-server")["items"]
                if i["kind"] == "provider-outage"]

    def test_outage_item_visible_while_down(self):
        attention.ollama_up = lambda: False
        items = self._outage_items()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["id"], "ollama-outage")
        self.assertIn("metered", item["title"])
        self.assertIn("Ollama", item["detail"])
        self.assertIn("metered", item["detail"])
        self.assertEqual(item["severity"], "warn")
        self.assertEqual(item["repo"], "remote-server")
        for f in ("id", "kind", "title", "detail", "repo",
                  "severity", "action", "created_at"):
            self.assertIn(f, item)

    def test_recovery_clears_item(self):
        # Derived, not stored: one stable id while down, gone on
        # recovery — no nag spam, no manual clear.
        attention.ollama_up = lambda: False
        self.assertEqual(len(self._outage_items()), 1)
        attention.ollama_up = lambda: True
        self.assertEqual(self._outage_items(), [])

    def test_outage_item_shows_spend_and_cap(self):
        # #885: while failover is active the Now view shows live
        # metered spend against the cap.
        attention.ollama_up = lambda: False
        attention.failover_budget = lambda: {
            "cap_usd": 5.0, "spent_usd": 1.25,
            "remaining_usd": 3.75, "blocked": False,
            "unknown": 0}
        items = self._outage_items()
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(
            item["title"],
            "Ollama unreachable \u2014 on metered Nebius "
            "(spend $1.25 / cap $5.00)")
        self.assertIn("$1.2500", item["detail"])
        self.assertIn("$5.00", item["detail"])

    def test_outage_item_degrades_when_budget_fails(self):
        # A budget read failure degrades the numbers, never the
        # item: the outage still surfaces.
        attention.ollama_up = lambda: False
        attention.failover_budget = self._orig["failover_budget"]
        orig_budget = attention.agents.agent_budget

        def _boom():
            raise RuntimeError("budget bug")
        attention.agents.agent_budget = _boom
        try:
            items = self._outage_items()
        finally:
            attention.agents.agent_budget = orig_budget
        self.assertEqual(len(items), 1)
        self.assertIn("$0.00", items[0]["title"])
        self.assertIn("unlimited", items[0]["title"])

    def test_probe_error_hides_item(self):
        def _boom():
            raise RuntimeError("probe bug")
        attention.ollama_up = _boom
        self.assertEqual(self._outage_items(), [])

    def test_probe_cached(self):
        calls = []
        orig = attention.model_router.ollama_reachable
        attention.model_router.ollama_reachable = (
            lambda timeout=5: calls.append(timeout) or False)
        try:
            self.assertFalse(attention.ollama_up())
            self.assertFalse(attention.ollama_up())
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0], 2)
        finally:
            attention.model_router.ollama_reachable = orig
            attention._OLLAMA_CACHE.clear()


class FailingRunTimestampTest(unittest.TestCase):
    """Failing-run items carry the run's own start time (#455)."""

    def _assemble(self, wfs, now):
        orig = {}
        seams = {
            "get_workflow_health": lambda repo: {"workflows": wfs},
            "list_failed_jobs": lambda: [],
            "list_failed_webhooks": lambda: [],
            "list_skipped_webhooks": lambda: [],
            "list_pending_approvals": lambda: [],
            "ollama_up": lambda: True,
        }
        for k, v in seams.items():
            orig[k] = getattr(attention, k)
            setattr(attention, k, v)
        try:
            return attention._assemble_items("remote-server", now)
        finally:
            for k, v in orig.items():
                setattr(attention, k, v)

    def _run_item(self, items):
        runs = [i for i in items if i["kind"] == "failing-run"]
        self.assertEqual(len(runs), 1)
        return runs[0]

    def test_item_carries_run_created_at(self):
        expected = datetime(2026, 11, 4, 10, 54, 51,
                            tzinfo=timezone.utc).timestamp()
        items = self._assemble(
            [{"id": 1, "name": "CI", "diagnosed_cause": "boom",
              "created_at": "2026-11-04T10:54:51Z",
              "updated_at": "2026-11-04T11:02:00Z"}],
            now=expected + 7200)
        self.assertEqual(self._run_item(items)["created_at"], expected)

    def test_restamp_stable_across_polls(self):
        wf = {"id": 1, "name": "CI", "diagnosed_cause": "boom",
              "created_at": "2026-11-04T10:54:51Z"}
        first = self._run_item(self._assemble([dict(wf)], now=1793610000.0))
        second = self._run_item(self._assemble([dict(wf)], now=1793613600.0))
        self.assertEqual(first["created_at"], second["created_at"])

    def test_camel_case_and_epoch_passthrough(self):
        items = self._assemble(
            [{"id": 1, "name": "CI", "diagnosed_cause": "boom",
              "createdAt": "2026-11-03T09:00:00Z"}],
            now=1793613600.0)
        expected = datetime(2026, 11, 3, 9, 0, 0,
                            tzinfo=timezone.utc).timestamp()
        self.assertEqual(self._run_item(items)["created_at"], expected)
        items = self._assemble(
            [{"id": 2, "name": "CI", "diagnosed_cause": "other",
              "created_at": 1793500000}],
            now=1793613600.0)
        self.assertEqual(self._run_item(items)["created_at"], 1793500000.0)

    def test_updated_fallback_then_now(self):
        expected = datetime(2026, 11, 4, 11, 2, 0,
                            tzinfo=timezone.utc).timestamp()
        items = self._assemble(
            [{"id": 1, "name": "CI", "diagnosed_cause": "boom",
              "updated_at": "2026-11-04T11:02:00Z"}],
            now=expected + 60)
        self.assertEqual(self._run_item(items)["created_at"], expected)
        now = 1793613600.0
        for wf in ({"id": 2, "name": "CI", "diagnosed_cause": "x"},
                   {"id": 3, "name": "CI", "diagnosed_cause": "y",
                    "created_at": "not-a-time",
                    "updated_at": "still-not-a-time"}):
            items = self._assemble([wf], now=now)
            self.assertEqual(self._run_item(items)["created_at"], now)


class AttentionFailedRunsTest(unittest.TestCase):
    """#451(2): failed runs surface as failed-job attention items.

    The registry seam is real here (a fresh RunRegistry); only the
    unrelated sources are stubbed."""

    def setUp(self):
        from backend.exec.engine import RunRegistry

        self.reg = RunRegistry()
        self._orig = {}
        for k in ("registry", "get_workflow_health",
                  "list_failed_webhooks", "list_skipped_webhooks",
                  "list_pending_approvals", "ollama_up"):
            self._orig[k] = getattr(attention, k)
        attention.registry = self.reg
        attention.get_workflow_health = lambda repo: {"workflows": []}
        attention.list_failed_webhooks = lambda: []
        attention.list_skipped_webhooks = lambda: []
        attention.list_pending_approvals = lambda: []
        attention.ollama_up = lambda: True

    def tearDown(self):
        for k, v in self._orig.items():
            setattr(attention, k, v)

    def test_failed_run_appears_in_attention(self):
        rid, err = self.reg.start_run("logs")
        self.assertIsNone(err)
        self.reg.append(rid, "bang: no disk\n")
        self.reg.finish_run(rid, 1)
        self.assertEqual(
            [j["id"] for j in attention.list_failed_jobs()], [rid])
        body = attention.attention("remote-server")
        failed = [i for i in body["items"] if i["kind"] == "failed-job"]
        self.assertEqual(len(failed), 1)
        item = failed[0]
        self.assertEqual(item["id"], f"job-{rid}")
        self.assertEqual(item["repo"], "remote-server")
        self.assertIn("logs", item["title"])
        self.assertIn("bang: no disk", item["detail"])

    def test_ok_and_running_runs_stay_quiet(self):
        ok, err = self.reg.start_run("logs")
        self.assertIsNone(err)
        self.reg.finish_run(ok, 0)
        active, err = self.reg.start_run("logs")
        self.assertIsNone(err)
        self.assertEqual(attention.list_failed_jobs(), [])
        body = attention.attention("remote-server")
        self.assertEqual(body["items"], [])


if __name__ == "__main__":
    unittest.main()
