"""Tests for skill-learner step 1: full action events + JSONL log.

Issue #1252: every drive and snapshot stores action, selector, x/y,
URL, timestamp, via, and a page-state hash; events persist to JSONL,
not only the capped in-memory ring. Acceptance: a 10-step demo
produces 10 ordered events with selectors, replayable from the log.
"""

import json
import os
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routers import browser_drive, browser_view


CANNED_RAW = {
    "url": "https://example.com/form",
    "title": "Contact form",
    "els": [
        {
            "i": 0,
            "tag": "INPUT",
            "text": "",
            "x": 100,
            "y": 200,
            "w": 200,
            "h": 30,
            "tid": "name",
            "id": None,
            "name": "name",
            "role": None,
        },
        {
            "i": 1,
            "tag": "BUTTON",
            "text": "Submit",
            "x": 100,
            "y": 300,
            "w": 80,
            "h": 30,
            "tid": "submit",
            "id": None,
            "name": None,
            "role": None,
        },
    ],
}


class FakeBridge:
    """Stand-in for CDPBridge: records ops, serves the canned page."""

    def __init__(self, session_id):
        self.calls = []

    async def connect(self):
        pass

    async def navigate(self, url):
        self.calls.append(("navigate", url))

    async def reattach(self):
        pass

    async def click(self, x, y):
        self.calls.append(("click", x, y))

    async def type_text(self, text):
        self.calls.append(("type", text))

    async def key(self, key, code):
        self.calls.append(("key", key, code))

    async def wheel(self, x, y, dx, dy):
        self.calls.append(("wheel", x, y, dx, dy))

    async def call(self, method, params=None, timeout=15):
        if method == "Runtime.evaluate":
            return {"result": {"value": dict(CANNED_RAW)}}
        return {}

    async def close(self):
        pass


def drive_client():
    app = FastAPI()
    app.include_router(browser_drive.router)
    return TestClient(app, raise_server_exceptions=False)


def live_session():
    return (
        mock.patch.object(browser_view, "_grid_live_session", return_value="sess-1"),
        mock.patch.object(browser_view, "CDPBridge", FakeBridge),
    )


class SkillRecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for key, name in (
            ("BROWSER_ACTIVITY_FILE", "activity.jsonl"),
            ("BROWSER_NOTES_FILE", "notes.md"),
            ("AGENT_CONTROL_STATE_FILE", "control.json"),
        ):
            path = os.path.join(self.tmp.name, name)
            os.environ[key] = path
            self.addCleanup(os.environ.pop, key)
        self.saved_ring = list(browser_view._ACTIVITY)
        self.saved_total = browser_view._LEARN_TOTAL[0]
        self.saved_state = dict(browser_view._LEARN_STATE)
        self.saved_page = dict(browser_view._LAST_PAGE)
        browser_view._ACTIVITY.clear()
        browser_view._LEARN_TOTAL[0] = 0
        browser_view._LEARN_STATE.update({"covered": 0, "last_learn_at": 0.0})
        browser_view._LAST_PAGE.update({"page": None, "hash": None})
        self.addCleanup(self._restore)

    def _restore(self):
        browser_view._ACTIVITY.clear()
        browser_view._ACTIVITY.extend(self.saved_ring)
        browser_view._LEARN_TOTAL[0] = self.saved_total
        browser_view._LEARN_STATE.update(self.saved_state)
        browser_view._LAST_PAGE.update(self.saved_page)

    def jsonl_events(self):
        path = os.path.join(self.tmp.name, "activity.jsonl")
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_event_schema_has_replay_fields(self):
        browser_view._log_activity(
            "click",
            "10,20",
            via="human",
            action="click",
            selector="[data-testid='x']",
            x=10,
            y=20,
            url="https://example.com/",
            page_hash="abc123",
        )
        (event,) = browser_view.recent_activity(1)
        for field in (
            "action",
            "selector",
            "x",
            "y",
            "url",
            "page_hash",
            "t",
            "kind",
            "detail",
            "via",
        ):
            self.assertIn(field, event)
        (logged,) = self.jsonl_events()
        self.assertEqual(logged["selector"], "[data-testid='x']")
        self.assertEqual(logged["page_hash"], "abc123")

    def test_ten_step_demo_replays_from_log(self):
        # The #1252 acceptance: 10 ordered drive events, each
        # replayable from the log line alone.
        ops = [
            {"op": "navigate", "url": "https://example.com/form"},
            {"op": "click", "x": 100, "y": 200},
            {"op": "type", "text": "Ariel Anders"},
            {"op": "key", "key": "Tab"},
            {"op": "click", "x": 100, "y": 300},
            {"op": "wheel", "x": 50, "y": 50, "dx": 0, "dy": 100},
            {"op": "click", "x": 500, "y": 500},
            {"op": "type", "text": "x"},
            {"op": "key", "key": "Enter"},
            {"op": "click", "x": 100, "y": 200},
        ]
        grid, bridge = live_session()
        with grid, bridge:
            client = drive_client()
            for body in ops:
                resp = client.post("/api/browser/drive", json=body)
                self.assertEqual(resp.status_code, 200, body)
        drives = [e for e in self.jsonl_events() if e["kind"] == "drive"]
        self.assertEqual(len(drives), 10)
        self.assertEqual([e["action"] for e in drives], [o["op"] for o in ops])
        for event, body in zip(drives, ops):
            if body["op"] == "navigate":
                self.assertEqual(event["url"], body["url"])
            elif body["op"] in ("click", "wheel"):
                self.assertEqual((event["x"], event["y"]), (body["x"], body["y"]))
            else:
                self.assertTrue(event["url"])
            self.assertTrue(event["page_hash"] or body["op"] == "navigate")
        named = [e for e in drives if e["selector"]]
        self.assertGreaterEqual(len(named), 3)
        self.assertIn("[data-testid='name']", [e["selector"] for e in named])
        self.assertIn("[data-testid='submit']", [e["selector"] for e in named])

    def test_snapshot_logs_url_and_hash(self):
        grid, bridge = live_session()
        with grid, bridge:
            resp = drive_client().get("/api/browser/snapshot")
        self.assertEqual(resp.status_code, 200)
        snaps = [e for e in browser_view.recent_activity(5) if e["kind"] == "snapshot"]
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["url"], "https://example.com/form")
        self.assertRegex(snaps[0]["page_hash"], r"^[0-9a-f]{12}$")

    def test_selector_priority_no_xpath_no_text(self):
        self.assertEqual(
            browser_view.selector_for({"tag": "INPUT", "tid": "name"}),
            "[data-testid='name']",
        )
        self.assertEqual(
            browser_view.selector_for({"tag": "DIV", "id": "main"}), "#main"
        )
        self.assertEqual(
            browser_view.selector_for({"tag": "INPUT", "name": "q"}), "input[name='q']"
        )
        self.assertEqual(
            browser_view.selector_for({"tag": "DIV", "role": "button"}),
            "div[role='button']",
        )
        # Attribute values are case-sensitive: only the tag folds.
        self.assertEqual(
            browser_view.selector_for({"tag": "INPUT", "name": "UserEmail"}),
            "input[name='UserEmail']",
        )
        self.assertEqual(
            browser_view.selector_for({"tag": "DIV", "role": "mainNav"}),
            "div[role='mainNav']",
        )
        self.assertIsNone(browser_view.selector_for({"tag": "A"}))

    def test_match_target_contains_then_nearest(self):
        page = {"elements": CANNED_RAW["els"]}
        hit = browser_view.match_target(page, 100, 200)
        self.assertEqual(hit["tid"], "name")
        # Just outside the submit rect but center within 48px:
        # nearest wins.
        near = browser_view.match_target(page, 145, 310)
        self.assertEqual(near["tid"], "submit")
        self.assertIsNone(browser_view.match_target(page, 900, 900))
        self.assertIsNone(browser_view.match_target(None, 1, 1))

    def test_second_navigate_pairs_no_stale_hash(self):
        # A navigate's destination hash is unknown until its
        # post-op snapshot: two navigates must both log the new URL
        # with no hash, never the previous page's.
        grid, bridge = live_session()
        with grid, bridge:
            client = drive_client()
            for url in ("https://example.com/form", "https://example.com/other"):
                resp = client.post(
                    "/api/browser/drive", json={"op": "navigate", "url": url}
                )
                self.assertEqual(resp.status_code, 200)
        navs = [e for e in self.jsonl_events() if e["action"] == "navigate"]
        self.assertEqual(
            [e["url"] for e in navs],
            ["https://example.com/form", "https://example.com/other"],
        )
        self.assertTrue(all(e["page_hash"] is None for e in navs))

    def test_snapshot_does_not_feed_learn_trigger(self):
        before = browser_view.learn_status()["events_since_learn"]
        browser_view._log_activity(
            "snapshot", "https://example.com/", via="agent", action="snapshot"
        )
        after = browser_view.learn_status()["events_since_learn"]
        self.assertEqual(before, after)

    def test_page_hash_stable_and_sensitive(self):
        page = {
            "url": "https://example.com/form",
            "title": "T",
            "elements": CANNED_RAW["els"],
        }
        first = browser_view.page_hash_of(page)
        self.assertRegex(first, r"^[0-9a-f]{12}$")
        self.assertEqual(first, browser_view.page_hash_of(page))
        moved = {
            "url": page["url"],
            "title": page["title"],
            "elements": [{**CANNED_RAW["els"][0], "x": 101}, CANNED_RAW["els"][1]],
        }
        self.assertNotEqual(first, browser_view.page_hash_of(moved))


if __name__ == "__main__":
    unittest.main()
