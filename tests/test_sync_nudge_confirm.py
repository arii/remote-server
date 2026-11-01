#!/usr/bin/env python3
import unittest


class TestSyncNudgeConfirm(unittest.TestCase):
    def test_sync_nudge_confirm_body_in_start_run(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Check that startRun includes the step summary naming the write step
        self.assertIn('Step 2 posts feedback to stalled Jules sessions.', content)
        self.assertIn('confirmDialog("Run action", confirmBody', content)


if __name__ == "__main__":
    unittest.main()
