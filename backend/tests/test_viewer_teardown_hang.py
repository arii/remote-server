"""Viewer teardown under a wedged Grid connection (issue #1202).

Live-proven leak: the viewer-owned Grid session survived tab close
and held the single browser slot. Two defects, both in the
disconnect path:

1. The websocket finally awaited an unbounded bridge.close() before
   the refcounted teardown. close() handshakes on the same Grid
   connection that just died, so a wedged slot starved the Grid
   DELETE forever. close() is now bounded (5s, suppressed) and the
   teardown always runs.
2. _viewer_claim probed Grid liveness while holding the registry
   lock; every connect behind a wedged Grid stalled disconnect
   teardown (which needs the same lock) behind the probe.

Run: PYTHONPATH=./deep-agent python3 -m unittest \
    backend.tests.test_viewer_teardown_hang
"""

import asyncio
import json
import threading
import time
import unittest
from unittest import mock

from backend.api.routers import browser_view


class HangBridge:
    """CDP bridge whose close() hangs (wedged Grid slot)."""

    CLOSE_HANG = 30

    def __init__(self, session_id, viewport=None, mobile=False):
        self.session_id = session_id
        self.viewport = (
            dict(viewport) if viewport is not None else dict(browser_view.VIEWPORT)
        )
        self.mobile = mobile
        self.dsf = browser_view.DSF

    async def connect(self):
        pass

    async def start_screencast(self, on_frame):
        pass

    async def navigate(self, url):
        pass

    async def reattach(self):
        pass

    async def close(self):
        await asyncio.sleep(self.CLOSE_HANG)


class DisconnectTeardownTest(unittest.TestCase):
    def setUp(self):
        self._sid = browser_view._viewer_session_id
        self._ref = browser_view._viewer_refcount
        self._vp = browser_view._viewer_viewport
        self._exp = browser_view._viewer_expires_at
        browser_view._viewer_session_id = None
        browser_view._viewer_refcount = 0
        browser_view._viewer_viewport = None
        browser_view._viewer_expires_at = None
        self.addCleanup(self._restore)

    def _restore(self):
        browser_view._viewer_session_id = self._sid
        browser_view._viewer_refcount = self._ref
        browser_view._viewer_viewport = self._vp
        browser_view._viewer_expires_at = self._exp

    def test_disconnect_teardown_survives_hung_bridge_close(self):
        # The core #1202 regression: tab closes while the Grid
        # connection is wedged (close() hangs 30s). The bounded
        # close (5s) must give way to the refcounted teardown. The
        # teardown arms the refresh grace window instead of
        # deleting: the Grid DELETE fires only when a later claim
        # finds the record expired, so the slot is still freed
        # without stranding a refresh at the start page. Pre-fix
        # this awaited the hanging close() unbounded and the
        # session leaked.
        #
        # The raw ASGI app is driven directly (not via TestClient):
        # the test portal tears app tasks down on socket close,
        # which would cancel the in-flight finally before the
        # bounded close resolves — production uvicorn lets the
        # disconnect finally run to completion.
        app = browser_view.create_app()
        sent = []
        pending = [{"type": "websocket.disconnect"}]

        async def receive():
            if pending:
                return pending.pop(0)
            await asyncio.sleep(3600)
            return {"type": "websocket.disconnect"}

        async def send(message):
            sent.append(message)

        scope = {"type": "websocket", "path": "/ws", "headers": []}
        with (
            mock.patch.object(browser_view, "CDPBridge", HangBridge),
            mock.patch.object(browser_view, "_grid_live_session", return_value=None),
            mock.patch.object(browser_view, "_grid_session_create", return_value="AAA"),
            mock.patch.object(browser_view, "_grid_session_alive", return_value=True),
            mock.patch.object(browser_view, "_grid_session_delete") as delete,
        ):
            start = time.monotonic()
            asyncio.run(app(scope, receive, send))
            elapsed = time.monotonic() - start
            kinds = [
                json.loads(m["text"])["type"]
                for m in sent
                if m["type"] == "websocket.send"
            ]
            self.assertIn("attached", kinds)
            # No immediate delete: the session stays warm for a
            # refresh re-dial.
            self.assertEqual(delete.call_count, 0)
            self.assertEqual(browser_view._viewer_session_id, "AAA")
            self.assertEqual(browser_view._viewer_refcount, 0)
            self.assertIsNotNone(browser_view._viewer_expires_at)
            # Bounded close (~5s) + teardown, not the 30s hang.
            self.assertLess(elapsed, 25, "disconnect blocked on close()")


class ClaimLockOrderingTest(unittest.TestCase):
    def setUp(self):
        self._sid = browser_view._viewer_session_id
        self._ref = browser_view._viewer_refcount
        self._vp = browser_view._viewer_viewport
        self._exp = browser_view._viewer_expires_at
        browser_view._viewer_session_id = "AAA"
        browser_view._viewer_refcount = 1
        browser_view._viewer_viewport = None
        browser_view._viewer_expires_at = None
        self.addCleanup(self._restore)

    def _restore(self):
        browser_view._viewer_session_id = self._sid
        browser_view._viewer_refcount = self._ref
        browser_view._viewer_viewport = self._vp
        browser_view._viewer_expires_at = self._exp

    def test_liveness_probe_does_not_hold_registry_lock(self):
        # A wedged Grid probe (3s) must not serialize disconnect
        # teardown behind it: _viewer_claim validates outside the
        # registry lock, so _viewer_release completes promptly.
        # Pre-fix the claim held the lock across the probe and the
        # release below blocked for the full probe duration. The
        # release arms the refresh grace window (returns False:
        # nothing to delete); the in-flight claim then revives the
        # warm record to the same live session.
        entered = threading.Event()

        def slow_probe(session_id):
            entered.set()
            time.sleep(3)
            return True

        result = {}
        with mock.patch.object(
            browser_view, "_grid_session_alive", side_effect=slow_probe
        ):
            worker = threading.Thread(
                target=lambda: result.setdefault("claim", browser_view._viewer_claim())
            )
            worker.start()
            try:
                self.assertTrue(
                    entered.wait(timeout=5), "claim never reached the probe"
                )
                start = time.monotonic()
                released = browser_view._viewer_release("AAA")
                elapsed = time.monotonic() - start
                self.assertFalse(released)
                self.assertLess(
                    elapsed,
                    2.0,
                    "teardown waited on the Grid probe under the lock",
                )
            finally:
                worker.join(timeout=10)
            self.assertFalse(worker.is_alive(), "claim thread stuck")
            self.assertEqual(result.get("claim"), "AAA")


if __name__ == "__main__":
    unittest.main()
