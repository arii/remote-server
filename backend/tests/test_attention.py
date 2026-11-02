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
