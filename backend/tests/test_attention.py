"""Tests for the attention queue + confirm helper (stdlib unittest)."""

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
