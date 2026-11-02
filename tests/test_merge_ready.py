#!/usr/bin/env python3
"""Focused tests for the merge agent's ready + td-gate tools.

The harness keeps NO local merge gate: merging goes through released
`td agent merge-approved`. Tests mock gh/td; no network.
"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest

_DEEP_AGENT = os.path.join(os.path.dirname(__file__), "..", "deep-agent")
sys.path.insert(0, os.path.abspath(_DEEP_AGENT))

_spec = importlib.util.spec_from_file_location(
    "merge_approved",
    os.path.join(_DEEP_AGENT, "merge_approved.py"),
)
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)


def _pr(**kw):
    pr = {
        "number": 184,
        "title": "fix: scope Jules session lists",
        "isDraft": True,
        "reviewDecision": "APPROVED",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"name": "ci", "conclusion": "SUCCESS"}],
    }
    pr.update(kw)
    return pr


class FakeGh:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, timeout=120):
        self.calls.append(args)
        if args[:2] == ("pr", "ready"):
            return subprocess.CompletedProcess(args, 0, "readied", "")
        raise AssertionError(f"unexpected gh call: {args}")


def _td_result(results):
    return subprocess.CompletedProcess(
        ("td",), 0, json.dumps({"results": results}), "")


class TestMergeReady(unittest.TestCase):
    def setUp(self):
        self.fake = FakeGh()
        self.prev_gh = ma._gh
        self.prev_sleep = __import__("time").sleep
        ma._gh = self.fake
        __import__("time").sleep = lambda s: None
        self.addCleanup(setattr, ma, "_gh", self.prev_gh)
        self.addCleanup(setattr, __import__("time"), "sleep",
                        self.prev_sleep)
        self.pr_states = []
        self.prev_pr_json = ma._pr_json
        ma._pr_json = lambda repo, number: self.pr_states.pop(0)
        self.addCleanup(setattr, ma, "_pr_json", self.prev_pr_json)
        self.prev_td = ma._td
        self.addCleanup(setattr, ma, "_td", self.prev_td)

    def test_mark_ready_marks_approved_green_draft(self):
        self.pr_states = [(_pr(), ""),
                          (_pr(isDraft=False), "")]
        out = ma.mark_ready.invoke({"repo": "arii/remote-server",
                                    "number": "184"})
        self.assertIn("READY", out)
        self.assertIn("draft=False", out)
        self.assertEqual(self.fake.calls[0][:2], ("pr", "ready"))

    def test_mark_ready_refuses_non_draft(self):
        self.pr_states = [(_pr(isDraft=False), "")]
        out = ma.mark_ready.invoke({"repo": "arii/remote-server",
                                    "number": "184"})
        self.assertIn("REFUSED", out)
        self.assertEqual(self.fake.calls, [])

    def test_mark_ready_refuses_unapproved(self):
        self.pr_states = [(_pr(reviewDecision="CHANGES_REQUESTED"), "")]
        out = ma.mark_ready.invoke({"repo": "arii/remote-server",
                                    "number": "184"})
        self.assertIn("REFUSED", out)
        self.assertEqual(self.fake.calls, [])

    def test_mark_ready_refuses_failing_checks(self):
        bad = _pr(statusCheckRollup=[{"name": "ci",
                                      "conclusion": "FAILURE"}])
        self.pr_states = [(bad, "")]
        out = ma.mark_ready.invoke({"repo": "arii/remote-server",
                                    "number": "184"})
        self.assertIn("REFUSED", out)
        self.assertEqual(self.fake.calls, [])

    def test_mark_ready_refuses_conflicting(self):
        self.pr_states = [(_pr(mergeable="CONFLICTING"), "")]
        out = ma.mark_ready.invoke({"repo": "arii/remote-server",
                                    "number": "184"})
        self.assertIn("REFUSED", out)
        self.assertEqual(self.fake.calls, [])

    def test_merge_goes_through_td(self):
        ma._td = lambda *a, **k: _td_result([
            {"pr_number": 184, "title": "t", "merged": True,
             "is_eligible": True, "reason": None},
            {"pr_number": 171, "title": "u", "merged": False,
             "is_eligible": False, "reason": "merge conflicts"},
        ])
        out = ma.merge_approved_td.invoke({"repo": "arii/remote-server"})
        self.assertIn("MERGED: PR #184", out)
        self.assertIn("REFUSED: PR #171", out)

    def test_merge_td_failure_surfaced(self):
        ma._td = lambda *a, **k: subprocess.CompletedProcess(
            a, 1, "", "boom")
        out = ma.merge_approved_td.invoke({"repo": "arii/remote-server"})
        self.assertIn("FAILED", out)
        self.assertIn("boom", out)

    def test_merge_td_non_json_surfaced(self):
        ma._td = lambda *a, **k: subprocess.CompletedProcess(
            a, 0, "not json", "")
        out = ma.merge_approved_td.invoke({"repo": "arii/remote-server"})
        self.assertIn("FAILED", out)

    def test_number_schema_rejects_bool_and_int(self):
        for bad in [True, 184]:
            with self.assertRaises(Exception, msg=f"number={bad!r}"):
                ma.pr_status.invoke({"repo": "arii/remote-server",
                                      "number": bad})
        for bad in ["18a", "", "0"]:
            self.assertIn(
                "REJECTED",
                ma.pr_status.invoke({"repo": "arii/remote-server",
                                      "number": bad}),
                f"number={bad!r}",
            )


if __name__ == "__main__":
    unittest.main()
