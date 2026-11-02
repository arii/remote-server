#!/usr/bin/env python3
"""Tests for repo selection persistence, defaults, and multi-tab refresh."""

import re
import unittest


class TestRepoSelectionState(unittest.TestCase):
    def setUp(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            self.src = f.read()

    def test_repo_card_click_dispatches_change(self):
        """Card click handler dispatches change event on the picker."""
        self.assertIn(
            'picker.dispatchEvent(new Event("change"))',
            self.src,
            "Card click handler must dispatch change on picker",
        )

    def test_local_storage_keys_written_and_read(self):
        """localStorage keys s5.repo, s5.tab, s5.sections are written and read."""
        for key in ("s5.repo", "s5.tab", "s5.sections"):
            self.assertIn(
                f'localStorage.setItem("{key}"',
                self.src,
                f"localStorage must write {key}",
            )
            self.assertIn(
                f'localStorage.getItem("{key}")',
                self.src,
                f"localStorage must read {key}",
            )

    def test_default_repo_constant_exists(self):
        """DEFAULT_REPO constant is defined with value 'remote-server'."""
        self.assertIn(
            'const DEFAULT_REPO = "remote-server";',
            self.src,
            "DEFAULT_REPO constant must be defined in JS",
        )

    def test_no_tech_dancer_js_picker_fallback(self):
        """No 'tech-dancer' JS picker-fallback literal remains."""
        self.assertNotIn(
            ': "tech-dancer"',
            self.src,
            "No JS picker fallback to 'tech-dancer' should remain",
        )

    def test_no_auto_pull_string_remains(self):
        """No 'Auto-pull' string remains in buttons/server.py."""
        self.assertNotIn(
            "Auto-pull",
            self.src,
            "No Auto-pull string should remain in buttons/server.py",
        )


if __name__ == "__main__":
    unittest.main()
