import unittest


class TestPiSessionListVisibility(unittest.TestCase):
    def test_pi_session_list_element_and_js(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        # 1. HTML container must be hidden by default
        self.assertIn('<div id="piSessionList" style="display:none;"></div>', content)

        # 2. Loading state does not set textContent inside piSessionListEl
        self.assertNotIn('piSessionListEl.textContent = "Loading sessions…";', content)
        self.assertIn('piSessionErrEl.textContent = "Loading sessions…";', content)

        # 3. renderSessionList hides the list element when no sessions match/exist
        self.assertIn('piSessionListEl.style.display = "none";', content)
        self.assertIn('piSessionListEl.style.display = "";', content)


if __name__ == "__main__":
    unittest.main()
