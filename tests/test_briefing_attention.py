import unittest
from core_cli import briefing_summary


def _make_briefing_data(**overrides):
    base = {
        "prs": None, "prs_raw": "", "prs_err": None,
        "pr_list": [],
        "gh": {},
        "reviews": {}, "reviews_err": {}, "reviews_raw_parts": [],
        "board": None, "board_err": None, "board_raw": "",
        "conf": {}, "conf_err": None, "conf_raw": "",
        "runs": None, "runs_err": None, "runs_raw": "",
        "jobs": {},
        "jules": [], "jules_err": None, "jules_raw": "",
    }
    base.update(overrides)
    return base


class TestBriefingAttention(unittest.TestCase):
    def test_failing_runs_feed_attention_list(self):
        # Scenario: gitleaks failure on main
        data = _make_briefing_data(
            runs={
                "workflow_runs": [
                    {
                        "id": 999,
                        "name": "Security Scan",
                        "head_branch": "main",
                        "event": "push",
                        "conclusion": "failure",
                        "html_url": "https://github.com/example/repo/actions/runs/999",
                    }
                ]
            },
            jobs={
                "999": [
                    ("gitleaks", ["run gitleaks scan"])
                ]
            },
        )

        analysis = briefing_summary.analyze(data)
        self.assertTrue(len(analysis["attention"]) > 0, "Attention items should not be empty when failing run exists")
        attention_text = analysis["attention"][0]
        self.assertIn("Security Scan", attention_text)
        self.assertIn("main", attention_text)
        self.assertIn("likely cause: gitleaks (run gitleaks scan)", attention_text)

        # Check marker presence in attention_with_markers
        self.assertTrue(len(analysis["attention_with_markers"]) > 0)
        marker_text, marker_tag = analysis["attention_with_markers"][0]
        self.assertIn("Security Scan", marker_text)
        self.assertIn("<!-- action:diagnose-run", marker_tag)

        # Build output verification
        briefing_text, attention_items, raw_text = briefing_summary.build(data, analysis=analysis)
        self.assertNotIn("Nothing needs attention.", briefing_text)
        self.assertIn("Security Scan", briefing_text)
        self.assertIn("likely cause: gitleaks", briefing_text)

    def test_nothing_needs_attention_only_when_genuinely_clear(self):
        data = _make_briefing_data(
            runs={
                "workflow_runs": [
                    {
                        "id": 1000,
                        "name": "CI",
                        "head_branch": "main",
                        "event": "push",
                        "conclusion": "success",
                    }
                ]
            },
        )

        analysis = briefing_summary.analyze(data)
        self.assertEqual(len(analysis["attention"]), 0)
        briefing_text, attention_items, raw_text = briefing_summary.build(data, analysis=analysis)
        self.assertIn("Nothing needs attention.", briefing_text)


if __name__ == "__main__":
    unittest.main()
