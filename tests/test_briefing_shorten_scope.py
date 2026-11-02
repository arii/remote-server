#!/usr/bin/env python3
"""Regression test for shorten function scoping in buttons/server.py.

renderBriefing() calls shorten(), which must be defined at top-level
scope rather than nested inside renderBriefing. The attention strip is
rendered by renderAttentionList(), called inline from renderBriefing()
(the former renderAttention() wrapper was intentionally inlined).
"""

import re
import unittest


class TestBriefingShortenScope(unittest.TestCase):
    def test_shorten_defined_at_top_level(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            lines = f.readlines()

        occurrences = [line for line in lines if "function shorten(" in line]
        self.assertEqual(
            len(occurrences),
            1,
            f"Expected exactly 1 occurrence of 'function shorten(', found {len(occurrences)}: {occurrences}",
        )

        shorten_def = occurrences[0]
        self.assertTrue(
            shorten_def.startswith("function shorten(s, n) {"),
            f"Expected 'function shorten(s, n) {{' to start at column 0, got: {repr(shorten_def)}",
        )

    def test_render_functions_present(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("function renderBriefing(", content)
        self.assertIn("function renderAttentionList(", content)


if __name__ == "__main__":
    unittest.main()
