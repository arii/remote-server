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


class TestBriefingEmpties(unittest.TestCase):
    def test_empty_sections_stay_empty(self):
        analysis = briefing_summary.analyze(_make_briefing_data())
        for key in ("reviews_lines", "board_lines",
                    "conflicts_lines", "overlaps_lines",
                    "workflow_lines"):
            self.assertEqual(analysis[key], [],
                             f"{key} should be empty, not a placeholder")

    def test_error_lines_still_surface(self):
        analysis = briefing_summary.analyze(_make_briefing_data(
            board_err="timeout", reviews_err={3: "denied"}))
        self.assertTrue(any("unavailable" in line
                            for line in analysis["board_lines"]))
        self.assertTrue(any("unavailable" in line
                            for line in analysis["reviews_lines"]))

    def test_markdown_keeps_none_markers(self):
        data = _make_briefing_data()
        text, _attention, _raw = briefing_summary.build(data)
        self.assertIn("## Reviews\n(none)", text)
        self.assertIn("## Conflicts\n(none)", text)
