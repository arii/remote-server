"""End-to-end co-browsing tests (follow_up Issue 12).

Pure helper checks run everywhere. Live stages (headed browser,
backend API, td) run only with E2E_COBROWSING=1 and skip when the
stack is unreachable, so CI stays green without a live stack::

    E2E_COBROWSING=1 python3 -m unittest tests.test_co_browsing_e2e
"""

import importlib.util
import os
import tempfile
import unittest
from unittest import mock

_SCRIPT = os.path.join("scripts", "verify-co-browsing-preflight.py")
_spec = importlib.util.spec_from_file_location("verify_cobrowsing", _SCRIPT)
preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(preflight)

LIVE = os.environ.get("E2E_COBROWSING") == "1"


def _png_bytes(extra=b"0" * 2048):
    return b"\x89PNG\r\n\x1a\n" + b"fake-chunks" + extra


class HelperTest(unittest.TestCase):
    def test_grid_cdp_ws_url_plain(self):
        with mock.patch.object(preflight, "BROWSER_HTTP", "http://browser:4444"):
            self.assertEqual(
                preflight.grid_cdp_ws_url("abc"), "ws://browser:4444/session/abc/se/cdp"
            )

    def test_grid_cdp_ws_url_https_and_trailing_slash(self):
        with mock.patch.object(preflight, "BROWSER_HTTP", "https://h:4444/"):
            self.assertEqual(
                preflight.grid_cdp_ws_url("s1"), "wss://h:4444/session/s1/se/cdp"
            )

    def test_screenshot_integrity_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            shot = os.path.join(tmp, "s.png")
            with open(shot, "wb") as handle:
                handle.write(_png_bytes())
            self.assertGreater(
                preflight.stage_screenshot_integrity(shot)["bytes"], 1024
            )

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

    def test_browser_base_strips_agent_suffix(self):
        with mock.patch.object(preflight, "AGENT_API", "http://buttons:8000/api/agent"):
            self.assertEqual(
                preflight._browser_base(), "http://buttons:8000/api/browser"
            )

    def test_chat_drive_stage_round_trip(self):
        calls = []

        def fake_get(url, timeout=10):
            calls.append(("GET", url))
            if url.endswith("/state"):
                return (
                    200,
                    b'{"ok": true, "agent_state": "IDLE", "human_driving": false}',
                )
            return 200, (
                b'{"ok": true, "kind": "viewer-shared", '
                b'"url": "https://x/", "title": "X", "elements": []}'
            )

        def fake_post(url, payload, timeout=25):
            calls.append(("POST", url, payload))
            return 200, (
                b'{"ok": true, "op": "navigate", "kind": "viewer-shared", '
                b'"url": "https://x/", "title": "X", "elements": []}'
            )

        def fake_state():
            return {"state": "IDLE", "current_step": "Chat drive: navigate https://x/"}

        with (
            mock.patch.object(preflight, "http_get", fake_get),
            mock.patch.object(preflight, "http_post_json", fake_post),
            mock.patch.object(preflight, "_agent_state", fake_state),
        ):
            result = preflight.stage_chat_drive()
        self.assertEqual(result["url"], "https://x/")
        self.assertEqual([c for c in calls if c[0] == "POST"][0][2]["op"], "navigate")

    def test_chat_drive_stage_missing_session_is_env_missing(self):
        def fake_get(url, timeout=10):
            if url.endswith("/state"):
                return (
                    200,
                    b'{"ok": true, "agent_state": "IDLE", "human_driving": false}',
                )
            return 409, b'{"detail": "browser busy"}'

        with mock.patch.object(preflight, "http_get", fake_get):
            with self.assertRaises(preflight.MissingTool):
                preflight.stage_chat_drive()


@unittest.skipUnless(LIVE, "needs E2E_COBROWSING=1 with a live stack")
class LivePreflightTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            preflight.stage_grid_ready()
        except Exception as exc:
            raise unittest.SkipTest(f"headed browser unreachable: {exc}")

    def test_grid_ready(self):
        self.assertIn("free_slots", preflight.stage_grid_ready())

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
                preflight.stage_screenshot_integrity(snap["screenshot"])["bytes"], 1024
            )

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

    def test_chat_drive_round_trip(self):
        try:
            result = preflight.stage_chat_drive()
        except preflight.MissingTool as exc:
            self.skipTest(str(exc))
        self.assertIn("url", result)
        self.assertIn("elements", result)


if __name__ == "__main__":
    unittest.main()
