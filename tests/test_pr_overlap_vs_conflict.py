import unittest
from buttons import briefing_summary


def _pr(num, draft=False):
    return {
        "number": num,
        "title": f"PR {num}",
        "headRefName": f"branch-{num}",
        "isDraft": draft,
        "updatedAt": "2026-11-02T00:00:00Z",
        "url": f"https://github.com/example/repo/pull/{num}",
    }


def _data():
    # Mirrors live remote-server shape: two CLEAN PRs (one draft) that both
    # touch buttons/server.py, plus one genuinely DIRTY PR.
    return {
        "prs": None, "prs_raw": "", "prs_err": None,
        "pr_list": [_pr(153, draft=True), _pr(164), _pr(200)],
        "gh": {
            153: {"mergeStateStatus": "CLEAN", "statusCheckRollup": []},
            164: {"mergeStateStatus": "CLEAN", "statusCheckRollup": []},
            200: {"mergeStateStatus": "DIRTY", "statusCheckRollup": []},
        },
        "reviews": {}, "reviews_err": {}, "reviews_raw_parts": [],
        "board": None, "board_err": None, "board_raw": "",
        "conf": {"status": "success", "conflicts": [
            {"prs": [153, 164], "files": ["buttons/server.py"]}]},
        "conf_err": None, "conf_raw": "",
        "runs": None, "runs_err": None, "runs_raw": "",
        "jobs": {},
        "jules": [], "jules_err": None, "jules_raw": "",
    }


class TestOverlapVsConflict(unittest.TestCase):
    def test_overlap_scan_does_not_mark_prs_conflicted(self):
        analysis = briefing_summary.analyze(_data())
        by_num = {p["number"]: p for p in analysis["prs"]}
        # CLEAN PRs in the overlap scan are NOT merge-conflicted,
        # draft or not.
        self.assertFalse(by_num[153]["conflicted"])
        self.assertFalse(by_num[164]["conflicted"])
        # A genuinely DIRTY PR still is.
        self.assertTrue(by_num[200]["conflicted"])

    def test_overlap_peers_recorded(self):
        analysis = briefing_summary.analyze(_data())
        by_num = {p["number"]: p for p in analysis["prs"]}
        self.assertEqual(by_num[153]["overlaps"],
                         {"with": [164], "files": ["buttons/server.py"]})
        self.assertEqual(by_num[164]["overlaps"],
                         {"with": [153], "files": ["buttons/server.py"]})
        self.assertEqual(by_num[200]["overlaps"],
                         {"with": [], "files": []})

    def test_attention_names_overlaps_not_conflicts(self):
        analysis = briefing_summary.analyze(_data())
        texts = analysis["attention"]
        # No "conflicted with base" for the merely-overlapping PRs.
        for num in (153, 164):
            self.assertFalse(
                any(f"#{num}" in t and "conflicted with base" in t
                    for t in texts),
                f"PR #{num} wrongly flagged conflicted: {texts}")
        # The DIRTY PR keeps its conflict attention item.
        self.assertTrue(any("#200" in t and "conflicted with base" in t
                            for t in texts))
        # The ready overlapping PR gets an overlap advisory (drafts
        # get no attention item of their own, per existing contract —
        # though #164's advisory names #153 as the peer).
        self.assertTrue(any("#164" in t and "overlap" in t for t in texts))
        self.assertFalse(any(t.startswith('#153 "') for t in texts))

    def test_conflicts_section_lists_only_dirty_prs(self):
        analysis = briefing_summary.analyze(_data())
        numbers = [c["number"] for c in analysis["conflicts"]]
        self.assertEqual(numbers, [200])
        overlap_numbers = sorted(
            n for o in analysis["overlaps"] for n in [o["number"]])
        self.assertEqual(overlap_numbers, [153, 164])

    def test_built_text_splits_conflicts_and_overlaps(self):
        data = _data()
        briefing_text, _items, _raw = briefing_summary.build(
            data, analysis=briefing_summary.analyze(data))
        conf_section = briefing_text.split("## Conflicts")[1].split("## ")[0]
        self.assertIn("#200", conf_section)
        self.assertNotIn("#153", conf_section)
        self.assertNotIn("#164", conf_section)
        self.assertIn("## Overlaps", briefing_text)
        over_section = briefing_text.split("## Overlaps")[1].split("## ")[0]
        self.assertIn("#153", over_section)
        self.assertIn("#164", over_section)
        self.assertIn("buttons/server.py", over_section)

    def test_overlaps_chip_rendered_by_page(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("pr.overlaps.with", content)


if __name__ == "__main__":
    unittest.main()
