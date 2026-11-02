#!/usr/bin/env python3
"""Tests for test push dialog copy and notifications-off guidance."""

import unittest


class TestNotificationsCopy(unittest.TestCase):
    def setUp(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            self.content = f.read()

    def test_pre_confirm_modal_copy_present(self):
        # Assert pre-confirm copy asking what will happen before triggering push test
        self.assertIn(
            'confirmDialog("Notification Test", "Send a test notification via the configured ntfy/webhook?", false)',
            self.content,
        )

    def test_pre_confirm_does_not_claim_sent_before_action(self):
        # Ensure testNotifyBtn.onclick does not fetch before confirmDialog
        btn_block = self.content.split('const testNotifyBtn = document.getElementById("testNotifyBtn");')[1].split(
            'const digestNotifyBtn ='
        )[0]
        confirm_pos = btn_block.find('confirmDialog("Notification Test", "Send a test notification')
        fetch_pos = btn_block.find('fetchJson("/buttons/api/notifications/test"')
        self.assertNotEqual(confirm_pos, -1, "Pre-confirm dialog should be present")
        self.assertNotEqual(fetch_pos, -1, "Fetch call should be present")
        self.assertLess(confirm_pos, fetch_pos, "Confirm dialog must appear before fetch call")

    def test_notifications_off_guidance_present(self):
        # Assert notifications-off guidance mentions NTFY_* and .env and docs/buttons.md
        self.assertIn("NTFY_* in .env", self.content)
        self.assertIn("docs/buttons.md", self.content)


if __name__ == "__main__":
    unittest.main()
