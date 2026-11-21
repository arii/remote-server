"""GET /api/status reports the last webhook pull per repo (#1489)."""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routers import briefing as briefing_router
from backend.notify import webhook as notify_webhook


class StatusWebhooksTest(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(briefing_router.router)
        self.client = TestClient(app)
        with notify_webhook.webhook_state_lock:
            self._saved = dict(notify_webhook.webhook_state)
            notify_webhook.webhook_state.clear()

    def tearDown(self):
        with notify_webhook.webhook_state_lock:
            notify_webhook.webhook_state.clear()
            notify_webhook.webhook_state.update(self._saved)

    def test_empty_when_no_pull_since_start(self):
        self.assertEqual(self.client.get("/api/status").json()["webhooks"], {})

    def test_last_pull_per_repo_without_observed_blob(self):
        with notify_webhook.webhook_state_lock:
            notify_webhook.webhook_state["arii/remote-server"] = {
                "at": 100.0,
                "ok": True,
                "advanced": False,
                "detail": "skipped: tree dirty",
                "skip_reason": "tree dirty",
                "observed": {"internal": "not exposed"},
            }
            notify_webhook.webhook_state["arii/other"] = {
                "at": 90.0,
                "ok": False,
                "advanced": False,
                "detail": "fetch failed",
            }
        hooks = self.client.get("/api/status").json()["webhooks"]
        skipped = hooks["arii/remote-server"]
        self.assertTrue(skipped["ok"])
        self.assertFalse(skipped["advanced"])
        self.assertEqual(skipped["skip_reason"], "tree dirty")
        self.assertNotIn("observed", skipped)
        self.assertFalse(hooks["arii/other"]["ok"])
        self.assertEqual(hooks["arii/other"]["detail"], "fetch failed")


if __name__ == "__main__":
    unittest.main()
