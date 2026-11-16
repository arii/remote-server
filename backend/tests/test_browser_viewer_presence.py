"""Regression inputs from the viewer review."""

import io
import json
import unittest
import urllib.error
from unittest import mock

from backend.api.routers import browser_view


class GridPresenceTest(unittest.TestCase):
    def test_grid_presence_not_page_command_success(self):
        for sessions, expected in [(["live"], True), (["other"], False), ([], False)]:
            payload = {
                "value": {
                    "nodes": [
                        {"slots": [{"session": {"sessionId": sid}} for sid in sessions]}
                    ]
                }
            }
            with mock.patch.object(
                browser_view.urllib.request,
                "urlopen",
                return_value=io.BytesIO(json.dumps(payload).encode()),
            ) as get:
                self.assertEqual(browser_view._grid_session_alive("live"), expected)
            self.assertEqual(get.call_args.args[0], browser_view.GRID_BASE + "/status")

    def test_unknown_liveness_does_not_delete_session(self):
        for error in [
            urllib.error.HTTPError(
                "grid", 500, "unexpected alert open", {}, io.BytesIO()
            ),
            urllib.error.URLError("offline"),
        ]:
            with mock.patch.object(
                browser_view.urllib.request, "urlopen", side_effect=error
            ):
                self.assertTrue(browser_view._grid_session_alive("live"))
        with mock.patch.object(
            browser_view.urllib.request, "urlopen", return_value=io.BytesIO(b"{}")
        ):
            self.assertTrue(browser_view._grid_session_alive("live"))
