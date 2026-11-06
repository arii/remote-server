"""End-to-end co-browsing tests (follow_up Issue 12).

Pure helper checks run everywhere. Live stages (browserless,
backend API, td) run only with E2E_COBROWSING=1 and skip when the
stack is unreachable, so CI stays green without a live stack::

    E2E_COBROWSING=1 python3 -m unittest tests.test_co_browsing_e2e
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from unittest import mock

_SCRIPT = os.path.join(os.path.dirname(__file__), "..", "scripts",
                       "verify-co-browsing-preflight.py")
_spec = importlib.util.spec_from_file_location("verify_cobrowsing", _SCRIPT)
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

LIVE = os.environ.get("E2E_COBROWSING") == "1"


def _png_bytes(extra=b"0" * 2048):
    return (b"\x89PNG\r\n\x1a\n" + b"fake-chunks" + extra)


class HelperTest(unittest.TestCase):
    def test_cdp_ws_url_plain(self):
        with mock.patch.object(preflight, "BROWSERLESS_HTTP",
                               "http://browserless:3000"), \
             mock.patch.object(preflight, "BROWSERLESS_TOKEN", ""):
            self.assertEqual(preflight.cdp_ws_url(), "ws://browserless:3000")

    def test_cdp_ws_url_appends_token(self):
        with mock.patch.object(preflight, "BROWSERLESS_HTTP",
                               "http://h:3000"), \
             mock.patch.object(preflight, "BROWSERLESS_TOKEN", "s3"):
            self.assertEqual(preflight.cdp_ws_url(),
                             "http://h:3000?token=s3".replace("http", "ws"))

    def test_screenshot_integrity_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            shot = os.path.join(tmp, "s.png")
            with open(shot, "wb") as handle:
                handle.write(_png_bytes())
            self.assertGreater(
                preflight.stage_screenshot_integrity(shot)["bytes"], 1024)

    def test_screenshot_integrity_rejects_non_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            shot = os.path.join(tmp, "s.png")
            with open(shot, "wb") as handle:
                handle.write(b"not a png" + b"0" * 2048)
            with self.assertRaises(preflight.CheckFailed):
                preflight.stage_screenshot_integrity(shot)

    def test_td_probe_reports_stale_td(self):
        with mock.patch.object(preflight, "TD_BIN", "td-definitely-missing"):
            with self.assertRaises(preflight.MissingTool):
                preflight._td_supports_preview()


@unittest.skipUnless(LIVE, "needs E2E_COBROWSING=1 with a live stack")
class LivePreflightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            preflight.stage_pressure()
        except Exception as exc:
            raise unittest.SkipTest(
                f"browserless unreachable: {exc}")

    def test_pressure(self):
        self.assertIn("max_concurrent", preflight.stage_pressure())

    def test_cdp_snapshot_and_integrity(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                snap = preflight.stage_cdp_snapshot(tmp)
            except preflight.MissingTool as exc:
                self.skipTest(str(exc))
            self.assertGreater(snap["dom_node_count"], 0)
            self.assertGreater(
                preflight.stage_screenshot_integrity(
                    snap["screenshot"])["bytes"], 1024)

    def test_pause_resume_roundtrip(self):
        try:
            paused = preflight.stage_pause_halts()
        except preflight.MissingTool as exc:
            self.skipTest(str(exc))
        self.assertEqual(paused["state"], "PAUSED_BY_USER")
        try:
            resumed = preflight.stage_resume_refresh()
        finally:
            # Never leave the shared head paused on failure paths.
            try:
                preflight.stage_resume_refresh()
            except Exception:
                pass
        self.assertEqual(resumed["state"], "RUNNING")

    def test_td_impact_visual_diff(self):
        repo = os.environ.get("REPO", "/srv/remote-server")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                result = preflight.stage_td_impact(repo, tmp)
            except preflight.MissingTool as exc:
                self.skipTest(str(exc))
            self.assertTrue(result["dom_node_count"] > 0)


if __name__ == "__main__":
    unittest.main()
