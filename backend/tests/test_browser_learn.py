"""Tests for the automatic browser learn pass (no live Grid needed)."""

import os
import tempfile
import unittest

from backend.api.routers import browser_view


class LearnPassTest(unittest.TestCase):
    def setUp(self):
        # Redirect the notebook and snapshot module globals; the
        # activity ring is process-global, so save and restore it.
        tmp = tempfile.NamedTemporaryFile(suffix=".md", delete=False)
        tmp.close()
        os.unlink(tmp.name)
        self.notes_path = tmp.name
        self.old_notes = os.environ.get("BROWSER_NOTES_FILE")
        os.environ["BROWSER_NOTES_FILE"] = self.notes_path
        self.saved_ring = list(browser_view._ACTIVITY)
        self.saved_total = browser_view._LEARN_TOTAL[0]
        self.saved_state = dict(browser_view._LEARN_STATE)
        browser_view._ACTIVITY.clear()
        browser_view._LEARN_TOTAL[0] = 0
        browser_view._LEARN_STATE.update({"covered": 0, "last_learn_at": 0.0})

    def tearDown(self):
        if self.old_notes is None:
            os.environ.pop("BROWSER_NOTES_FILE", None)
        else:
            os.environ["BROWSER_NOTES_FILE"] = self.old_notes
        try:
            os.unlink(self.notes_path)
        except OSError:
            pass
        browser_view._ACTIVITY.clear()
        browser_view._ACTIVITY.extend(self.saved_ring)
        browser_view._LEARN_TOTAL[0] = self.saved_total
        browser_view._LEARN_STATE.update(self.saved_state)

    def test_summarizer_groups_dests_and_sides(self):
        events = [
            {"t": 1.0, "kind": "go", "detail": "https://a.example/x", "via": "human"},
            {"t": 2.0, "kind": "click", "detail": "10,20", "via": "human"},
            {
                "t": 3.0,
                "kind": "drive",
                "detail": "navigate https://b.example/",
                "via": "agent",
            },
        ]
        bullets = browser_view.summarize_events(events)
        self.assertTrue(any("a.example" in b for b in bullets))
        self.assertTrue(any("human did: click x1, go x1" in b for b in bullets))
        self.assertTrue(any("agent did: drive x1" in b for b in bullets))

    def test_volume_trigger_fires_automatically(self):
        for i in range(browser_view.LEARN_THRESHOLD):
            browser_view._log_activity("click", f"{i},{i}")
        with open(self.notes_path, encoding="utf-8") as fh:
            text = fh.read()
        # One automatic pass: header plus one dated section.
        self.assertIn("# Browser notebook", text)
        self.assertEqual(text.count("## Auto-notes"), 1)
        self.assertIn("click x25", text)
        status = browser_view.learn_status()
        self.assertEqual(status["events_since_learn"], 0)

    def test_cooldown_collapses_bursts(self):
        for i in range(browser_view.LEARN_THRESHOLD):
            browser_view._log_activity("click", f"{i},{i}")
        for i in range(browser_view.LEARN_THRESHOLD):
            browser_view._log_activity("click", f"9,{i}")
        with open(self.notes_path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertEqual(text.count("## Auto-notes"), 1)

    def test_quiet_stretches_write_nothing(self):
        # Idleness is not a trigger: a handful of events and no
        # manual request leaves the notebook unwritten.
        for i in range(3):
            browser_view._log_activity("click", f"{i},{i}")
        self.assertFalse(os.path.exists(self.notes_path))
        result = browser_view.run_learn_pass()
        self.assertFalse(result["wrote"])

    def test_manual_pass_covers_since_last(self):
        for i in range(3):
            browser_view._log_activity("click", f"{i},{i}")
        first = browser_view.run_learn_pass(manual=True)
        self.assertTrue(first["wrote"])
        self.assertEqual(first["events_covered"], 3)
        second = browser_view.run_learn_pass(manual=True)
        self.assertFalse(second["wrote"])
        self.assertEqual(second["reason"], "nothing new")

    def test_learn_marker_does_not_feed_trigger(self):
        for i in range(3):
            browser_view._log_activity("click", f"{i},{i}")
        browser_view.run_learn_pass(manual=True)
        status = browser_view.learn_status()
        self.assertEqual(status["events_since_learn"], 0)

    def test_read_notes_missing_file(self):
        result = browser_view.read_notes()
        self.assertEqual(result, {"notes": "", "truncated": False})


if __name__ == "__main__":
    unittest.main()
