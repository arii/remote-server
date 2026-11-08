"""Independent fail-closed viewer tests for the pulls router.

Authored without reference to the fix's own tests: every assertion
pins the observable contract — a gh outage or malformed `gh api user`
output surfaces viewer "unknown" (never ""), the PR list still
returns its rows, and valid logins pass through untouched.
"""

import json
import unittest

from backend.api.routers import pulls as pulls_mod


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class _Runner:
    """Minimal subprocess double keyed on argv prefix."""

    def __init__(self):
        self.handlers = {}

    def on(self, prefix, proc_or_exc):
        self.handlers[tuple(prefix)] = proc_or_exc
        return self

    def run(self, argv, **kwargs):
        for prefix, outcome in self.handlers.items():
            if list(argv[: len(prefix)]) == list(prefix):
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"unexpected argv: {argv}")


def _pr_list_payload():
    return json.dumps(
        [
            {
                "number": 7,
                "title": "Fix thing",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "reviewDecision": "",
                "headRefName": "fix-thing",
                "statusCheckRollup": [],
                "url": "https://github.com/arii/remote-server/pull/7",
                "author": {"login": "teammate"},
            }
        ]
    )


class ViewerSentinelTest(unittest.TestCase):
    def test_sentinel_is_nonempty_unknown(self):
        self.assertEqual(pulls_mod.VIEWER_UNKNOWN, "unknown")
        self.assertTrue(pulls_mod.VIEWER_UNKNOWN)
        self.assertNotEqual(pulls_mod.VIEWER_UNKNOWN, "")

    def test_prlist_default_viewer_is_unknown(self):
        self.assertEqual(pulls_mod.PrList().viewer, "unknown")
        self.assertNotEqual(pulls_mod.PrList().viewer, "")


class GhOutageViewerTest(unittest.TestCase):
    def test_gh_exception_yields_unknown(self):
        runner = _Runner().on(["gh", "api", "user"], RuntimeError("gh down"))
        self.assertEqual(pulls_mod._gh_viewer(runner), "unknown")

    def test_gh_nonzero_exit_yields_unknown(self):
        runner = _Runner().on(
            ["gh", "api", "user"],
            _Proc(stdout="octo", stderr="auth error", returncode=1),
        )
        self.assertEqual(pulls_mod._gh_viewer(runner), "unknown")

    def test_list_endpoint_reports_unknown_on_outage_but_keeps_rows(self):
        runner = (
            _Runner()
            .on(["gh", "pr", "list"], _Proc(stdout=_pr_list_payload()))
            .on(["gh", "api", "user"], RuntimeError("connect timeout"))
        )
        out = pulls_mod.prs(repo="remote-server", runner=runner)
        self.assertEqual(out["viewer"], "unknown")
        self.assertNotEqual(out["viewer"], "")
        self.assertEqual(len(out["prs"]), 1)
        self.assertEqual(out["prs"][0]["number"], 7)


class MalformedViewerOutputTest(unittest.TestCase):
    MALFORMED = (
        "",
        "   ",
        "a/b",
        "two words",
        "[1, 2]",
        '"quoted login"',
        "has space",
        "a\tb",
        "a\nb",
    )

    def test_malformed_outputs_yield_unknown(self):
        for stdout in self.MALFORMED:
            with self.subTest(stdout=stdout):
                runner = _Runner().on(["gh", "api", "user"], _Proc(stdout=stdout))
                self.assertEqual(
                    pulls_mod._gh_viewer(runner), "unknown", f"stdout={stdout!r}"
                )

    def test_list_endpoint_reports_unknown_on_malformed_viewer(self):
        runner = (
            _Runner()
            .on(["gh", "pr", "list"], _Proc(stdout=_pr_list_payload()))
            .on(["gh", "api", "user"], _Proc(stdout="not a login"))
        )
        out = pulls_mod.prs(repo="remote-server", runner=runner)
        self.assertEqual(out["viewer"], "unknown")
        self.assertEqual(len(out["prs"]), 1)

    def test_valid_login_passes_through(self):
        # Positive control: the malformed rejection is selective,
        # not a blanket unknown.
        runner = _Runner().on(["gh", "api", "user"], _Proc(stdout="octo\n"))
        self.assertEqual(pulls_mod._gh_viewer(runner), "octo")
        runner = _Runner().on(["gh", "api", "user"], _Proc(stdout='"octo"'))
        self.assertEqual(pulls_mod._gh_viewer(runner), "octo")

    def test_list_endpoint_reports_login_when_known(self):
        runner = (
            _Runner()
            .on(["gh", "pr", "list"], _Proc(stdout=_pr_list_payload()))
            .on(["gh", "api", "user"], _Proc(stdout="octo"))
        )
        out = pulls_mod.prs(repo="remote-server", runner=runner)
        self.assertEqual(out["viewer"], "octo")


if __name__ == "__main__":
    unittest.main()
