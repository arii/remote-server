"""POST /api/briefing/refresh + boot precache (#929)."""

import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import briefing as briefing_router

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False


class _SyncThread:
    """Thread fake: runs the target inline on start()."""

    def __init__(self, target=None, args=(), daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class BriefingRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._old_narrator = os.environ.get("BRIEFING_NARRATOR")
        os.environ["BRIEFING_NARRATOR"] = "off"
        app = FastAPI()
        app.include_router(briefing_router.router)
        self.client = TestClient(app)
        briefing_router._BRIEFING_CACHE.clear()
        briefing_router._BRIEFING_REFRESHING.clear()

    def tearDown(self):
        if self._old_narrator is None:
            os.environ.pop("BRIEFING_NARRATOR", None)
        else:
            os.environ["BRIEFING_NARRATOR"] = self._old_narrator
        briefing_router._BRIEFING_CACHE.clear()
        briefing_router._BRIEFING_REFRESHING.clear()

    def _fake_run(self, stdout):
        proc = mock.Mock()
        proc.returncode = 0
        proc.stdout = stdout
        proc.stderr = ""
        return mock.patch.object(subprocess, "run", return_value=proc)

    def _sync_threads(self):
        fake_threading = mock.Mock()
        fake_threading.Thread = _SyncThread
        return mock.patch.object(briefing_router, "threading",
                                 fake_threading)

    def test_refresh_starts_background_regeneration(self):
        fresh = {"repo": "remote-server", "prs": [],
                 "attention": ["new"]}
        with self._fake_run(json.dumps(fresh)), self._sync_threads():
            resp = self.client.post("/api/briefing/refresh",
                                    json={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["repo"], "remote-server")
        self.assertTrue(body["started"])
        cached = briefing_router._BRIEFING_CACHE["remote-server"][1]
        self.assertEqual(cached["attention"], ["new"])
        self.assertNotIn("remote-server",
                         briefing_router._BRIEFING_REFRESHING)

    def test_refresh_dedupes_in_flight(self):
        briefing_router._BRIEFING_REFRESHING.add("remote-server")
        with self._sync_threads():
            resp = self.client.post("/api/briefing/refresh",
                                    json={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["started"])

    def test_refresh_rejects_bad_repo(self):
        resp = self.client.post("/api/briefing/refresh",
                                json={"repo": "a/b/c"})
        self.assertEqual(resp.status_code, 400)

    def test_warm_populates_cold_cache(self):
        fresh = {"repo": "remote-server", "prs": []}
        with self._fake_run(json.dumps(fresh)):
            warmed = briefing_router.warm_briefing_cache(
                ["remote-server"])
        self.assertEqual(warmed, ["remote-server"])
        hit = briefing_router._BRIEFING_CACHE["remote-server"]
        self.assertLess(time.time() - hit[0], 60)

    def test_warm_skips_fresh_entries(self):
        briefing_router._BRIEFING_CACHE["remote-server"] = (
            time.time(), {"repo": "remote-server", "prs": []})
        with mock.patch.object(
                briefing_router, "_run_briefing",
                side_effect=RuntimeError("boom")) as run_mock:
            warmed = briefing_router.warm_briefing_cache(
                ["remote-server"])
        run_mock.assert_not_called()
        self.assertEqual(warmed, ["remote-server"])

    def test_warm_tolerates_generation_failure(self):
        with mock.patch.object(
                briefing_router, "_run_briefing",
                side_effect=RuntimeError("boom")):
            warmed = briefing_router.warm_briefing_cache(
                ["oldschema"])
        self.assertEqual(warmed, [])
        self.assertNotIn("oldschema", briefing_router._BRIEFING_CACHE)

    def test_warm_skips_invalid_names(self):
        self.assertEqual(
            briefing_router.warm_briefing_cache(["a/b/c"]), [])


if __name__ == "__main__":
    unittest.main()
