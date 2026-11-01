#!/usr/bin/env python3
"""Consolidated tests for buttons/server.py UI markup and client-side logic."""

import unittest


class TestButtonsUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            cls.content = f.read()

    def test_attention_retry_button_in_server(self):
        # Verify that when briefing fails, attentionBody renders a Retry button
        self.assertIn('span.textContent = "Briefing unavailable — ";', self.content)
        self.assertIn('const rb = document.createElement("button");', self.content)
        self.assertIn('rb.className = "btn-ghost btn-retry";', self.content)
        self.assertIn('rb.textContent = "Retry";', self.content)
        self.assertIn('rb.onclick = () => refreshBriefing();', self.content)

    def test_pi_session_list_element_and_js(self):
        # 1. HTML container must be hidden by default
        self.assertIn('<div id="piSessionList" style="display:none;"></div>', self.content)

        # 2. Loading state does not set textContent inside piSessionListEl
        self.assertNotIn('piSessionListEl.textContent = "Loading sessions…";', self.content)
        self.assertIn('piSessionErrEl.textContent = "Loading sessions…";', self.content)

        # 3. renderSessionList hides the list element when no sessions match/exist
        self.assertIn('piSessionListEl.style.display = "none";', self.content)
        self.assertIn('piSessionListEl.style.display = "";', self.content)

    def test_sync_nudge_confirm_body_in_start_run(self):
        # Check that startRun includes the step summary naming the write step
        self.assertIn('Step 2 posts feedback to stalled Jules sessions.', self.content)
        self.assertIn('confirmDialog("Run action", confirmBody', self.content)


if __name__ == "__main__":
    unittest.main()
