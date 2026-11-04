#!/usr/bin/env python3
"""Per-PR fan-out is bounded and honest (issue #487).

Regression: gh enrichment looped ALL open PRs uncapped (30s timeout
each) while review enrichment silently stopped at 10 — PRs past #10
got CI data but no review substance with zero indication of the
cutoff. Both loops now share ENRICHMENT_CAP, most-recently-updated
first, with an explicit enrichment_note when capped.
"""

import unittest
from unittest import mock

import core_cli.briefing_summary as bs


def _pr(num, updated):
    return {"number": num, "title": f"pr {num}",
            "headRefName": f"branch-{num}", "isDraft": False,
            "updatedAt": updated, "mergeStateStatus": "CLEAN",
            "url": f"https://example.invalid/{num}"}


def _rows(count, start=1):
    # updatedAt ascending with number: higher number = more recent.
    return [_pr(n, f"2026-11-{min(n, 28):02d}T00:00:00Z")
            for n in range(start, start + count)]


def _payload_for(open_rows):
    def payload(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            if "merged" in argv:
                return [], "", ""
            return list(open_rows), "raw-open", ""
        if argv[:3] == ["gh", "pr", "view"]:
            return {"number": int(argv[3])}, "", ""
        if "read-pr-reviews" in argv:
            return {"reviews": []}, "", ""
        if "workflow-runs" in argv:
            return {"workflow_runs": []}, "", ""
        if "status-board" in argv:
            return {"work": []}, "", ""
        if "detect-conflicts" in argv:
            return {}, "", ""
        if "list-sessions" in argv:
            return {"sessions": []}, "", ""
        return {}, "", ""
    return payload


def _run_fetch(open_rows):
    calls = []

    def fake_run_json(cmd, timeout, cwd=None):
        calls.append(cmd)
        return _payload_for(open_rows)(cmd)

    old_key, old_gh = bs.REPO_KEY, bs.REPO_GH
    bs.REPO_KEY, bs.REPO_GH = "remote-server", "arii/remote-server"
    try:
        with mock.patch.object(bs, "run_json",
                               side_effect=fake_run_json), \
                mock.patch.object(bs, "_target_checkout_cwd",
                                  return_value=""), \
                mock.patch.object(bs, "_config_repo",
                                  return_value="arii/remote-server"):
            data = bs.fetch()
    finally:
        bs.REPO_KEY, bs.REPO_GH = old_key, old_gh
    return data, calls


def _analyze_data(**over):
    data = {
        "prs": None, "prs_raw": "", "prs_err": "",
        "pr_list": [], "gh": {},
        "conf": None, "conf_raw": "", "conf_err": "",
        "reviews": {}, "reviews_err": {},
        "reviews_raw_parts": [],
        "board": None, "board_raw": "", "board_err": "",
        "runs": None, "runs_raw": "", "runs_err": "",
        "jobs": {}, "jules": [], "jules_err": "",
        "jules_raw": "", "merged_heads": set(),
        "enrichment_total": 0, "enrichment_shown": 0,
        "enrichment_truncated": False, "enrichment_note": "",
        "enrichment_cap": bs.ENRICHMENT_CAP,
    }
    data.update(over)
    return data


class FanoutCapTest(unittest.TestCase):
    def test_subprocess_count_capped_regardless_of_open_prs(self):
        data, calls = _run_fetch(_rows(25))
        views = [c for c in calls if c[:3] == ["gh", "pr", "view"]]
        reviews = [c for c in calls if "read-pr-reviews" in c]
        self.assertEqual(len(views), bs.ENRICHMENT_CAP)
        self.assertEqual(len(reviews), bs.ENRICHMENT_CAP)
        self.assertEqual(data["enrichment_total"], 25)
        self.assertEqual(data["enrichment_shown"], bs.ENRICHMENT_CAP)
        self.assertTrue(data["enrichment_truncated"])

    def test_under_cap_enriches_all_with_no_note(self):
        data, calls = _run_fetch(_rows(3))
        views = [c for c in calls if c[:3] == ["gh", "pr", "view"]]
        reviews = [c for c in calls if "read-pr-reviews" in c]
        self.assertEqual(len(views), 3)
        self.assertEqual(len(reviews), 3)
        self.assertEqual(data["enrichment_note"], "")
        self.assertFalse(data["enrichment_truncated"])
        analysis = bs.analyze(data)
        self.assertEqual(analysis["enrichment_note"], "")
        self.assertFalse(analysis["enrichment_truncated"])

    def test_most_recently_updated_first(self):
        rows = _rows(25)
        data, calls = _run_fetch(rows)
        enriched = {int(c[3]) for c in calls
                    if c[:3] == ["gh", "pr", "view"]}
        # Numbers 16-25 are the 10 most recent; oldest (1-15) excluded.
        self.assertEqual(enriched, set(range(16, 26)))
        reviewed = set()
        for c in calls:
            if "read-pr-reviews" in c:
                idx = c.index("--pr")
                reviewed.add(int(c[idx + 1]))
        self.assertEqual(reviewed, set(range(16, 26)))

    def test_truncation_note_renders_when_capped(self):
        data, _ = _run_fetch(_rows(23))
        analysis = bs.analyze(data)
        note = analysis["enrichment_note"]
        self.assertIn(f"showing enrichment for {bs.ENRICHMENT_CAP} of 23",
                      note)
        self.assertTrue(analysis["enrichment_truncated"])
        self.assertEqual(analysis["enrichment_total"], 23)
        self.assertEqual(analysis["enrichment_shown"], bs.ENRICHMENT_CAP)
        text, _, _ = bs.build(data, analysis=analysis)
        self.assertIn(note, text)
        self.assertIn("Note: showing enrichment for", text)

    def test_no_note_in_text_when_under_cap(self):
        data, _ = _run_fetch(_rows(2))
        text, _, _ = bs.build(data)
        self.assertNotIn("showing enrichment for", text)

    def test_per_pr_section_errors_kept(self):
        rows = _rows(3)

        def payload(argv):
            if "read-pr-reviews" in argv:
                idx = argv.index("--pr")
                if argv[idx + 1] == "2":
                    return None, "raw", "denied"
                return {"reviews": []}, "", ""
            return _payload_for(rows)(argv)

        calls = []

        def fake_run_json(cmd, timeout, cwd=None):
            calls.append(cmd)
            return payload(cmd)

        old_key, old_gh = bs.REPO_KEY, bs.REPO_GH
        bs.REPO_KEY, bs.REPO_GH = "remote-server", "arii/remote-server"
        try:
            with mock.patch.object(bs, "run_json",
                                   side_effect=fake_run_json), \
                    mock.patch.object(bs, "_target_checkout_cwd",
                                      return_value=""), \
                    mock.patch.object(bs, "_config_repo",
                                      return_value="arii/remote-server"):
                data = bs.fetch()
        finally:
            bs.REPO_KEY, bs.REPO_GH = old_key, old_gh
        self.assertEqual(data["reviews_err"], {2: "denied"})
        analysis = bs.analyze(data)
        self.assertTrue(any("unavailable for #2" in line
                            for line in analysis["reviews_lines"]))

    def test_single_run_worst_case_documented(self):
        self.assertIsInstance(bs.ENRICHMENT_CAP, int)
        self.assertGreaterEqual(bs.ENRICHMENT_CAP, 10)
        self.assertLessEqual(bs.ENRICHMENT_CAP, 15)
        doc = (bs.__doc__ or "") + (bs.fetch.__doc__ or "")
        self.assertIn("worst case", doc.lower())
        cap_comment = f"2 * ENRICHMENT_CAP subprocesses"
        import inspect
        source = inspect.getsource(bs)
        self.assertIn(cap_comment, source)

    def test_analyze_tolerates_legacy_data_without_enrichment_keys(self):
        data = _analyze_data(pr_list=_rows(12))
        for key in ("enrichment_total", "enrichment_shown",
                    "enrichment_truncated", "enrichment_note",
                    "enrichment_cap"):
            del data[key]
        analysis = bs.analyze(data)
        self.assertTrue(analysis["enrichment_truncated"])
        self.assertIn("showing enrichment for", analysis["enrichment_note"])


if __name__ == "__main__":
    unittest.main()
