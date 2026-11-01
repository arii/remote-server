import unittest


class TestAttentionRetryButton(unittest.TestCase):
    def test_attention_retry_button_in_server(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        # Verify that when briefing fails, attentionBody renders a Retry button
        self.assertIn('span.textContent = "Briefing unavailable — ";', content)
        self.assertIn('const rb = document.createElement("button");', content)
        self.assertIn('rb.className = "btn-ghost btn-retry";', content)
        self.assertIn('rb.textContent = "Retry";', content)
        self.assertIn('rb.onclick = () => refreshBriefing();', content)


if __name__ == "__main__":
    unittest.main()
