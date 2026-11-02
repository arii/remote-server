#!/usr/bin/env python3
"""Test script initialization order and briefing timeout handling in buttons/server.py.

Asserts that:
- Top-level state objects (let tabBadgeCounts) and function declarations (function projectRepo, etc.)
  precede the initial execution calls (refreshProject(), refreshJobs(), refreshBriefing()).
- The served HTML uses a declarations-first ordering in a single <script> block.
- An AbortController and timeout exist on the briefing fetch path.
- The init sequence is wrapped in try/catch to render the 'Briefing unavailable — Retry' UI on errors.
"""

import re
import unittest
from buttons.server import HTML


class TestButtonsInitOrder(unittest.TestCase):
    def test_single_script_block(self):
        scripts = re.findall(r"<script>(.*?)</script>", HTML, re.DOTALL)
        self.assertEqual(len(scripts), 1, f"Expected 1 <script> block, found {len(scripts)}")

    def test_declarations_precede_init_calls(self):
        script_match = re.search(r"<script>(.*?)</script>", HTML, re.DOTALL)
        self.assertIsNotNone(script_match, "<script> block not found in HTML")
        script = script_match.group(1)

        pos_tab_badge_counts = script.find("let tabBadgeCounts =")
        pos_project_repo = script.find("function projectRepo()")
        pos_render_attention_list = script.find("function renderAttentionList(")

        self.assertNotEqual(pos_tab_badge_counts, -1, "let tabBadgeCounts declaration not found")
        self.assertNotEqual(pos_project_repo, -1, "function projectRepo declaration not found")
        self.assertNotEqual(pos_render_attention_list, -1, "function renderAttentionList declaration not found")

        # Find initial execution block
        pos_init_try = script.find("// Initial execution sequence")
        self.assertNotEqual(pos_init_try, -1, "Initial execution sequence not found")

        pos_init_project = script.find("refreshProject()", pos_init_try)
        pos_init_jobs = script.find("refreshJobs()", pos_init_try)
        pos_init_briefing = script.find("refreshBriefing()", pos_init_try)

        self.assertNotEqual(pos_init_project, -1, "refreshProject() not called in init sequence")
        self.assertNotEqual(pos_init_jobs, -1, "refreshJobs() not called in init sequence")
        self.assertNotEqual(pos_init_briefing, -1, "refreshBriefing() not called in init sequence")

        # Assert declarations precede initial execution
        self.assertLess(
            pos_tab_badge_counts,
            pos_init_try,
            "let tabBadgeCounts must be declared before initial execution",
        )
        self.assertLess(
            pos_project_repo,
            pos_init_try,
            "function projectRepo must be declared before initial execution",
        )
        self.assertLess(
            pos_render_attention_list,
            pos_init_try,
            "function renderAttentionList must be declared before initial execution",
        )

        # Assert declarations precede refreshProject() init call specifically
        self.assertLess(
            pos_project_repo,
            pos_init_project,
            "function projectRepo must be declared before refreshProject() runs",
        )
        self.assertLess(
            pos_tab_badge_counts,
            pos_init_project,
            "let tabBadgeCounts must be declared before refreshProject() runs",
        )

    def test_briefing_fetch_has_abort_controller_timeout(self):
        script_match = re.search(r"<script>(.*?)</script>", HTML, re.DOTALL)
        self.assertIsNotNone(script_match)
        script = script_match.group(1)

        self.assertIn("new AbortController()", script)
        self.assertIn("controller.abort()", script)
        self.assertIn("signal: controller.signal", script)
        # Timeout around 60s
        self.assertTrue(
            "60000" in script or "60 * 1000" in script,
            "Expected ~60s timeout for briefing fetch AbortController",
        )

    def test_init_sequence_try_catch_renders_retry(self):
        script_match = re.search(r"<script>(.*?)</script>", HTML, re.DOTALL)
        self.assertIsNotNone(script_match)
        script = script_match.group(1)

        # Check renderBriefingUnavailable helper definition
        self.assertIn("function renderBriefingUnavailable(", script)
        self.assertIn("Briefing unavailable — ", script)
        self.assertIn("btn-ghost btn-retry", script)
        self.assertIn("Retry", script)

        # Check try / catch around init sequence
        pos_init_try = script.find("// Initial execution sequence")
        init_slice = script[pos_init_try : pos_init_try + 500]
        self.assertIn("try {", init_slice)
        self.assertIn("catch (err)", init_slice)
        self.assertIn("renderBriefingUnavailable(", init_slice)


if __name__ == "__main__":
    unittest.main()
