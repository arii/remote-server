"""Unit tests for scheduled/periodic merge_gated (#867, #1790).

Covers:
- Green PRs passing merge gates are merged.
- PRs with failing/pending CI checks are not merged.
- PRs with operator holds / skip_reason() (e.g. boomtick #1473, hold labels) are skipped.
- Evaluation across boomtick and remote-server target repositories.
- Logging of gate evaluation details for each candidate PR.
"""

from __future__ import annotations

import threading
import unittest

from backend import merge_gated


def _green_pr_view(number: int = 100, base: str = "main") -> dict:
    return {
        "number": number,
        "title": "Fix calculation in feature",
        "headRefName": f"agent/issue-{number}",
        "baseRefName": base,
        "isDraft": False,
        "labels": [],
        "statusCheckRollup": [
            {"status": "COMPLETED", "conclusion": "SUCCESS", "name": "CI"}
        ],
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "",
    }


class TestMergeGated(unittest.TestCase):
    def test_green_pr_passing_merge_gates_is_merged(self):
        merged_calls = []
        logs = []

        res = merge_gated.evaluate_repo(
            "remote-server",
            "arii/remote-server",
            default_branch_fn=lambda _: "main",
            open_prs_fn=lambda _: [{"number": 100}],
            pr_gate_view_fn=lambda _, num: _green_pr_view(num),
            merge_pr_fn=lambda r, num: merged_calls.append((r, num)),
            log_fn=logs.append,
        )

        self.assertEqual(res["merged"], 1)
        self.assertEqual(res["skipped"], 0)
        self.assertEqual(merged_calls, [("arii/remote-server", 100)])
        self.assertTrue(any("PR #100: merged" in line for line in logs))
        self.assertTrue(any("gate facts: ci=passing" in line for line in logs))

    def test_pr_with_failing_ci_is_not_merged(self):
        merged_calls = []
        logs = []

        view = _green_pr_view(101)
        view["statusCheckRollup"] = [
            {"status": "COMPLETED", "conclusion": "FAILURE", "name": "CI"}
        ]

        res = merge_gated.evaluate_repo(
            "remote-server",
            "arii/remote-server",
            default_branch_fn=lambda _: "main",
            open_prs_fn=lambda _: [{"number": 101}],
            pr_gate_view_fn=lambda _, num: view,
            merge_pr_fn=lambda r, num: merged_calls.append((r, num)),
            log_fn=logs.append,
        )

        self.assertEqual(res["merged"], 0)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(merged_calls, [])
        self.assertTrue(any("ci is failing" in line for line in logs))

    def test_pr_with_pending_ci_is_not_merged(self):
        merged_calls = []
        logs = []

        view = _green_pr_view(102)
        view["statusCheckRollup"] = [
            {"status": "IN_PROGRESS", "conclusion": None, "name": "CI"}
        ]

        res = merge_gated.evaluate_repo(
            "remote-server",
            "arii/remote-server",
            default_branch_fn=lambda _: "main",
            open_prs_fn=lambda _: [{"number": 102}],
            pr_gate_view_fn=lambda _, num: view,
            merge_pr_fn=lambda r, num: merged_calls.append((r, num)),
            log_fn=logs.append,
        )

        self.assertEqual(res["merged"], 0)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(merged_calls, [])
        self.assertTrue(any("ci is pending" in line for line in logs))

    def test_pr_with_operator_hold_label_is_skipped(self):
        for hold_label in [
            "hold",
            "do-not-merge",
            "operator-hold",
            "autorelease: pending",
            "wip",
        ]:
            merged_calls = []
            logs = []
            view = _green_pr_view(103)
            view["labels"] = [{"name": hold_label}]

            res = merge_gated.evaluate_repo(
                "remote-server",
                "arii/remote-server",
                default_branch_fn=lambda _: "main",
                open_prs_fn=lambda _: [
                    {"number": 103, "labels": [{"name": hold_label}]}
                ],
                pr_gate_view_fn=lambda _, num: view,
                merge_pr_fn=lambda r, num: merged_calls.append((r, num)),
                log_fn=logs.append,
            )

            self.assertEqual(res["merged"], 0)
            self.assertEqual(res["skipped"], 1)
            self.assertEqual(merged_calls, [], f"PR with {hold_label} should not merge")
            self.assertTrue(
                any(f"held by label: {hold_label}" in line for line in logs),
                f"Missing hold log for {hold_label}",
            )

    def test_boomtick_1473_release_pr_strictly_skipped(self):
        merged_calls = []
        logs = []

        view = _green_pr_view(1473)
        view["title"] = "chore: release main"
        view["headRefName"] = "release-please--branches--main"
        view["labels"] = [{"name": "autorelease: pending"}]

        res = merge_gated.evaluate_repo(
            "boomtick",
            "arii/boomtick",
            default_branch_fn=lambda _: "main",
            open_prs_fn=lambda _: [{"number": 1473, "title": "chore: release main"}],
            pr_gate_view_fn=lambda _, num: view,
            merge_pr_fn=lambda r, num: merged_calls.append((r, num)),
            log_fn=logs.append,
        )

        self.assertEqual(res["merged"], 0)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(merged_calls, [])
        self.assertTrue(any("held release PR" in line for line in logs))

    def test_draft_pr_is_skipped(self):
        merged_calls = []
        logs = []

        view = _green_pr_view(104)
        view["isDraft"] = True

        res = merge_gated.evaluate_repo(
            "remote-server",
            "arii/remote-server",
            default_branch_fn=lambda _: "main",
            open_prs_fn=lambda _: [{"number": 104, "isDraft": True}],
            pr_gate_view_fn=lambda _, num: view,
            merge_pr_fn=lambda r, num: merged_calls.append((r, num)),
            log_fn=logs.append,
        )

        self.assertEqual(res["merged"], 0)
        self.assertEqual(res["skipped"], 1)
        self.assertEqual(merged_calls, [])
        self.assertTrue(any("draft PR" in line for line in logs))

    def test_evaluate_all_targets_boomtick_and_remote_server(self):
        evaluated = []

        def mock_eval(name, gh, **_kwargs):
            evaluated.append((name, gh))
            return {"repo": name, "gh": gh, "merged": 0, "skipped": 0, "error": None}

        outcomes = merge_gated.evaluate_all(
            repos=merge_gated.DEFAULT_TARGET_REPOS,
            default_branch_fn=lambda _: "main",
            open_prs_fn=lambda _: [],
        )

        self.assertEqual(len(outcomes), 2)
        gh_targets = [o["gh"] for o in outcomes]
        self.assertIn("arii/boomtick", gh_targets)
        self.assertIn("arii/remote-server", gh_targets)

    def test_periodic_loop_with_stop_event(self):
        stop_event = threading.Event()
        runs = 0

        def fake_eval_all(*_args, **_kwargs):
            nonlocal runs
            runs += 1
            stop_event.set()
            return []

        orig_eval = merge_gated.evaluate_all
        try:
            merge_gated.evaluate_all = fake_eval_all
            merge_gated.periodic_loop(
                interval_seconds=1, once=False, stop_event=stop_event
            )
        finally:
            merge_gated.evaluate_all = orig_eval

        self.assertEqual(runs, 1)


if __name__ == "__main__":
    unittest.main()
