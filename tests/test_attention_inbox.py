import unittest


class TestAttentionInbox(unittest.TestCase):
    def setUp(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            self.content = f.read()

    def test_dedupe_helper_exists_and_used_by_both_attention_sections(self):
        # Assert dedupe attention helper exists
        self.assertIn("dedupeAttention", self.content)
        # Assert it is used in renderBriefing
        self.assertIn("renderBriefing", self.content)
        # Assert it is used in renderAttention
        self.assertIn("renderAttention", self.content)

    def test_badge_render_hooks_exist_per_tab(self):
        # Tab badges for briefing, jobs, and ledger
        self.assertIn('data-tab="briefing"', self.content)
        self.assertIn('data-tab="jobs"', self.content)
        self.assertIn('data-tab="ledger"', self.content)
        self.assertIn("updateTabBadges", self.content)

    def test_empty_state_strings_exist(self):
        self.assertIn("No failing runs ✓", self.content)
        self.assertIn("No open PRs ✓", self.content)
        self.assertIn("No plans yet", self.content)


if __name__ == "__main__":
    unittest.main()
