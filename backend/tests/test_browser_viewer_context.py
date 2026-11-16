"""Navigation invalidates stale learning selectors without taking snapshots."""

import unittest
from unittest import mock

from backend.api.routers import browser_view


class NavigationContextTest(unittest.TestCase):
    def test_main_frame_change_invalidates_snapshot_without_url_subscriber(self):
        with mock.patch.dict(browser_view._LAST_PAGE, {"page": None, "hash": None}):
            old = {
                "url": "https://example.com/old",
                "elements": [{"id": "old", "x": 5, "y": 5, "w": 10, "h": 10}],
            }
            browser_view.remember_page(old)
            self.assertEqual(browser_view._human_context(5, 5)["selector"], "#old")
            bridge = browser_view.CDPBridge("test")
            bridge.on_message(
                {
                    "method": "Page.frameNavigated",
                    "params": {
                        "frame": {"id": "main", "url": "https://example.com/new"}
                    },
                }
            )
            context = browser_view._human_context(5, 5)
            self.assertEqual(context["url"], "https://example.com/new")
            self.assertIsNone(context["selector"])
            self.assertIsNone(context["page_hash"])
            self.assertEqual(browser_view.last_page()[0]["elements"], [])
            fresh = {
                "url": context["url"],
                "elements": [{"id": "new", "x": 5, "y": 5, "w": 10, "h": 10}],
            }
            browser_view.remember_page(fresh)
            browser_view.remember_url(fresh["url"])
            self.assertEqual(browser_view._human_context(5, 5)["selector"], "#new")
            self.assertIsNotNone(browser_view._human_context()["page_hash"])
            bridge.on_message(
                {
                    "method": "Page.frameNavigated",
                    "params": {
                        "frame": {
                            "id": "child",
                            "parentId": "main",
                            "url": "https://ads.example/",
                        }
                    },
                }
            )
            self.assertEqual(browser_view._human_context()["url"], fresh["url"])
            bridge.on_message(
                {
                    "method": "Page.navigatedWithinDocument",
                    "params": {"frameId": "main", "url": fresh["url"] + "#next"},
                }
            )
            self.assertEqual(
                browser_view._human_context()["url"], fresh["url"] + "#next"
            )
            self.assertIsNone(browser_view._human_context(5, 5)["selector"])
            self.assertIsNone(browser_view._human_context()["page_hash"])

    def test_initial_url_and_empty_event_without_snapshot(self):
        with mock.patch.dict(browser_view._LAST_PAGE, {"page": None, "hash": None}):
            bridge = browser_view.CDPBridge("test")
            bridge.on_message(
                {
                    "method": "Page.frameNavigated",
                    "params": {"frame": {"id": "main", "url": ""}},
                }
            )
            self.assertIsNone(browser_view._human_context()["url"])
            browser_view.remember_url("about:blank")
            self.assertEqual(browser_view._human_context()["url"], "about:blank")
            self.assertIsNone(browser_view._human_context()["page_hash"])
