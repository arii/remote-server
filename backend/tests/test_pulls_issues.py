"""Tests for the pulls + issues routers (subprocess mocked).

Covers the ports of buttons/server.py GET /prs, GET /pr-details,
POST /pr-action, POST /repair/dispatch, GET /repair/status,
GET /issues, POST /create-issue,
POST /issue-draft: shapes, validation errors, and pr-action gates.
"""

import json
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


class FakeProc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode

    def wait(self):
        return self.returncode


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


def gh_pr_view(ci_conclusion="SUCCESS", merge="CLEAN"):
    status = "COMPLETED" if ci_conclusion != "IN_PROGRESS" else "IN_PROGRESS"
    concl = ci_conclusion if ci_conclusion != "IN_PROGRESS" else ""
    return json.dumps({
        "number": 7, "title": "Fix thing", "headRefName": "fix-thing",
        "baseRefName": "main", "mergeable": "MERGEABLE",
        "mergeStateStatus": merge, "reviewDecision": "APPROVED",
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


class PrsTest(unittest.TestCase):
    def test_list_shapes_rows(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_list_prs()))
        out = pulls_mod.prs(repo="remote-server", runner=runner)
        self.assertEqual(out["repo"], "remote-server")
        row = out["prs"][0]
        self.assertEqual(
            (row["number"], row["title"], row["author"], row["branch"],
             row["ciStatus"], row["ciLabel"], row["reviewDecision"],
             row["mergeable"]),
            (7, "Fix thing", "octo", "fix-thing", "passing", "passing",
             "APPROVED", "MERGEABLE"))

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

    def test_details_bad_pr(self):
        from fastapi import HTTPException

        runner = FakeRunner()
        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.pr_details(repo="remote-server", pr=0,
                                 runner=runner)
        self.assertEqual(ctx.exception.status_code, 400)


class PrDiffTest(unittest.TestCase):
    DIFF = ("diff --git a/app.py b/app.py\n"
            "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n"
            "-print(1)\n+print(2)\n")

    def test_diff_shape(self):
        runner = FakeRunner().on(["gh"], FakeProc(stdout=self.DIFF))
        out = pulls_mod.pr_diff(repo="remote-server", pr=7,
                                runner=runner)
        self.assertEqual(out["number"], 7)
        self.assertIn("+print(2)", out["diff"])
        self.assertFalse(out["truncated"])
        argv = runner.calls[0]
        self.assertEqual(argv[:4], ["gh", "pr", "diff", "7"])
        self.assertIn("arii/remote-server", argv)

    def test_diff_truncates_huge_output(self):
        big = self.DIFF * 5000
        runner = FakeRunner().on(["gh"], FakeProc(stdout=big))
        out = pulls_mod.pr_diff(repo="remote-server", pr=7,
                                runner=runner)
        self.assertTrue(out["truncated"])
        self.assertLessEqual(len(out["diff"]), 30000)

    def test_diff_bad_pr(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.pr_diff(repo="remote-server", pr=0,
                              runner=FakeRunner())
        self.assertEqual(ctx.exception.status_code, 400)

    def test_diff_gh_failure_is_500(self):
        from fastapi import HTTPException

        runner = FakeRunner().on(
            ["gh"], FakeProc(stderr="boom", returncode=1))
        with self.assertRaises(HTTPException) as ctx:
            pulls_mod.pr_diff(repo="remote-server", pr=7,
                              runner=runner)
        self.assertEqual(ctx.exception.status_code, 500)


class PrActionTest(unittest.TestCase):
    def test_merge_gate_refuses_dirty_ci(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_pr_view("FAILURE", "DIRTY")))
        with self.assertRaises(ValueError):
            _pr_action("arii/remote-server", 7, "merge", runner=runner)

    def test_merge_ok(self):
        runner = FakeRunner().on(
            ["gh", "pr", "view"], FakeProc(stdout=gh_pr_view())).on(
            ["gh", "pr", "merge"], FakeProc(stdout="merged"))
        out = _pr_action("arii/remote-server", 7, "merge",
                         runner=runner)
        self.assertTrue(out["ok"])

    def test_update_only_behind(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_pr_view("SUCCESS", "CLEAN")))
        with self.assertRaises(ValueError):
            _pr_action("arii/remote-server", 7, "update", runner=runner)
        runner2 = FakeRunner().on(
            ["gh", "pr", "view"],
            FakeProc(stdout=gh_pr_view("SUCCESS", "BEHIND"))).on(
            ["gh", "api"], FakeProc(stdout="{}"))
        out = _pr_action("arii/remote-server", 7, "update",
                         runner=runner2)
        self.assertTrue(out["ok"])

    def test_fix_ci_requires_failing(self):
        runner = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_pr_view()))
        with self.assertRaises(ValueError):
            _pr_action("arii/remote-server", 7, "fix-ci", runner=runner)
        runner2 = FakeRunner().on(
            ["gh"], FakeProc(stdout=gh_pr_view("FAILURE", "DIRTY"))).on(
            ["td"], FakeProc(stdout='{"ok": true}'))
        out = _pr_action("arii/remote-server", 7, "fix-ci",
                         runner=runner2)
        self.assertTrue(out["ok"])
        td_call = [c for c in runner2.calls if c[0] == "td"][0]
        self.assertEqual(
            td_call,
            ["td", "--json", "agent", "fix-ci", "--pr", "7", "--execute"])

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


class IssuesTest(unittest.TestCase):
    def test_list_truncates_body(self):
        with patch.object(issues_mod, "run_gh_json",
                          return_value=json.loads(gh_issue_list())):
            out = issues_mod.issues(repo="remote-server")
        self.assertEqual(len(out["issues"]), 1)
        self.assertTrue(out["issues"][0]["body"].endswith("…"))
        self.assertEqual(len(out["issues"][0]["body"]), 601)

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
            argv = m.call_args[0][1]
            self.assertEqual(argv[:3], ["issue", "create", "--repo"])
            self.assertNotIn(";", " ".join(argv))
        self.assertTrue(out["url"].endswith("/issues/9"))

    def test_issue_draft_shape(self):
        out = issues_mod.issue_draft(issues_mod.IssueDraftRequest(
            repo="remote-server", source="ci", title="T",
            cause="c", details="d"))
        self.assertTrue(out["ok"])
        self.assertIn("### Recommended Remediation", out["body"])
        self.assertIn("ci", out["body"])


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


class RepairTest(unittest.TestCase):
    def test_dispatch_and_status(self):
        from fastapi import HTTPException

        with self.assertRaises(HTTPException):
            issues_mod.repair_dispatch(
                issues_mod.RepairDispatchRequest(issue="nope"))

        proc = FakeProc(stdout=iter(["line1\n"]), returncode=0)
        runner = FakeRunner()
        runner.handlers["popen"] = proc
        old = issues_mod._repair_runner
        issues_mod._repair_runner = runner
        try:
            out = issues_mod.repair_dispatch(
                issues_mod.RepairDispatchRequest(issue="#5"))
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
                ["python3", "scripts/repair_worker.py",
                 "--issue", "5"])
        finally:
            issues_mod._repair_runner = old


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


if __name__ == "__main__":
    unittest.main()
