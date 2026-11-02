"""Tests for automated (no-human-UI) refresh ledger entries."""

import unittest
from unittest.mock import patch, MagicMock

import buttons.server as srv


class TestAutoRefreshEntry(unittest.TestCase):
    def test_warm_entry_shape(self):
        e = srv._auto_refresh_entry("warm-context", "arii/tech-dancer",
                                    True, "context-warm done")
        self.assertEqual(e["kind"], "job")
        self.assertEqual(e["event"], "auto-warm-context")
        self.assertEqual(e["repo"], "arii/tech-dancer")
        self.assertTrue(e["ok"])
        self.assertEqual(e["trigger"], "webhook-post-pull")

    def test_briefing_entry_failure(self):
        e = srv._auto_refresh_entry("briefing", "arii/tech-dancer",
                                    False, "boom")
        self.assertEqual(e["event"], "auto-briefing")
        self.assertFalse(e["ok"])
        self.assertEqual(e["detail"], "boom")

    def test_detail_truncated(self):
        e = srv._auto_refresh_entry("briefing", "r", True, "x" * 500)
        self.assertLessEqual(len(e["detail"]), 200)

    def test_kind_in_allowlist(self):
        e = srv._auto_refresh_entry("warm-context", "r", True)
        self.assertIn(e["kind"], srv.LEDGER_KINDS)


class TestWebhookWarmAutomation(unittest.TestCase):
    def _info(self):
        return {"gh": "arii/tech-dancer", "td_cwd": "/srv/repos/tech-dancer",
                "host_path": "/tmp/nope"}

    def test_warm_ledgers_both_runs(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = '{"prs": []}'
        proc.stderr = ""
        with patch.object(srv.subprocess, "run", return_value=proc), \
                patch.object(srv, "_ledger_append") as la:
            srv._webhook_warm("a" * 40, "b" * 40, self._info())
        events = [c.args[0]["event"] for c in la.call_args_list]
        self.assertIn("auto-warm-context", events)
        self.assertIn("auto-briefing", events)
        for c in la.call_args_list:
            self.assertEqual(c.args[0]["kind"], "job")

    def test_warm_failure_still_ledgers(self):
        with patch.object(srv.subprocess, "run",
                          side_effect=RuntimeError("no docker")), \
                patch.object(srv, "_ledger_append") as la:
            srv._webhook_warm("a" * 40, "b" * 40, self._info())
        self.assertTrue(la.call_count >= 2)
        for c in la.call_args_list:
            self.assertFalse(c.args[0]["ok"])


if __name__ == "__main__":
    unittest.main()
