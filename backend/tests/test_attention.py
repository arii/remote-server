"""Tests for the attention queue + confirm helper (stdlib unittest)."""

import json
import os
import tempfile
import time
import unittest

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
            list_pending_approvals=lambda: [],
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
            list_pending_approvals=lambda: [{
                "id": "a1", "ts": 3.0, "title": "Approve?",
                "event": "approval requested"}],
        )
        try:
            body = attention.attention("remote-server")
        finally:
            self._restore(orig)
        kinds = {i["kind"] for i in body["items"]}
        self.assertEqual(
            kinds, {"failing-run", "failed-job", "failed-webhook",
                    "pending-approval"})
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
            list_pending_approvals=lambda: [],
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
            {"id": "w-ok", "ts": 3.0, "ok": True,
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
            list_pending_approvals=lambda: [],
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
            list_pending_approvals=lambda: [],
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
            "list_pending_approvals": lambda: [],
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
            json.dump({"job-j1": time.time() - 1}, f)
        self.assertEqual(self._ids(), ["job-j1"])
        self.assertNotIn("job-j1", self._store_file())

    def test_store_survives_reread(self):
        until = time.time() + 3600
        with open(os.environ["BUTTONS_SNOOZE_PATH"], "w",
                  encoding="utf-8") as f:
            json.dump({"job-j1": until}, f)
        # A fresh read (as after a restart) still filters the id.
        self.assertEqual(self._ids(), [])

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


if __name__ == "__main__":
    unittest.main()
