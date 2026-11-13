"""Verify-before-write tests (D3): per-kind content gate + reason shape.

The verify pass runs server-side after shape validation, before any
approval card renders. These tests pin: which content checks run per
proposal kind (repo exists, PR number real), what deliberately does
NOT run (branch existence — a brand-new branch is a legitimate
dispatch/coder target; the over-strict-gate risk from the spec), the
rejection-reason shape (VERIFY-FAILED: <check>: <detail>), and the
fail-closed contract when the checker itself cannot run.
"""

import unittest
from unittest import mock

from backend.exec import verify


def ok_repo(repo):
    return True, '{"name": "remote-server"}'


def ok_pr(repo, number):
    return True, '{"number": 9826}'


class VerifyReasonShape(unittest.TestCase):
    def test_failures_carry_prefix_and_check_name(self):
        with mock.patch.object(verify, "repo_exists", return_value=(False, "gh down")):
            ok, reason = verify.verify_proposal({"kind": "issue"}, "arii/ghost")
        self.assertFalse(ok)
        self.assertTrue(reason.startswith(verify.VERIFY_REASON_PREFIX), reason)
        self.assertIn("repo_exists", reason)
        self.assertIn("gh down", reason)

    def test_prefix_constant_is_stable(self):
        # The re-prompt and the SSE error text both key on this prefix.
        self.assertEqual(verify.VERIFY_REASON_PREFIX, "VERIFY-FAILED:")


class VerifyPerKind(unittest.TestCase):
    def test_issue_passes_on_real_turn_repo(self):
        with mock.patch.object(verify, "repo_exists", ok_repo):
            ok, reason = verify.verify_proposal(
                {"kind": "issue", "title": "t", "body": "b"}, "arii/remote-server"
            )
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_issue_fails_on_missing_turn_repo(self):
        with mock.patch.object(verify, "repo_exists", return_value=(False, "not found")):
            ok, reason = verify.verify_proposal(
                {"kind": "issue", "title": "t", "body": "b"}, "arii/ghost"
            )
        self.assertFalse(ok)
        self.assertIn("repo_exists", reason)

    def test_td_adds_no_content_check(self):
        # The td allowlist (engine.ALLOWED_TD_COMMANDS) IS the content
        # gate and shape validation already applied it: verify runs no
        # gh call at all — prove it by leaving the real gh shelling in
        # place and asserting pass without network.
        ok, reason = verify.verify_proposal(
            {"kind": "td", "command": "td doctor"}, "arii/remote-server"
        )
        self.assertTrue(ok, reason)

    def test_dispatch_passes_on_real_repo(self):
        with mock.patch.object(verify, "repo_exists", ok_repo):
            ok, reason = verify.verify_proposal(
                {
                    "kind": "dispatch",
                    "repo": "arii/remote-server",
                    "branch": "feat/whatever",
                    "task": "do it",
                },
                "arii/remote-server",
            )
        self.assertTrue(ok, reason)

    def test_dispatch_brand_new_branch_is_legitimate(self):
        # Spec risk: an over-strict gate bounces legitimate proposals
        # pointing at a branch that does not exist yet — the coder
        # creates it. Branch existence must NOT be checked; only the
        # repo is verified.
        with mock.patch.object(
            verify, "repo_exists", side_effect=ok_repo
        ) as repo_check:
            with mock.patch.object(verify, "pr_exists", side_effect=ok_pr) as pr_check:
                ok, reason = verify.verify_proposal(
                    {
                        "kind": "coder",
                        "repo": "arii/remote-server",
                        "branch": "feat/brand-new-branch",
                        "task": "do it",
                    },
                    "arii/remote-server",
                )
        self.assertTrue(ok, reason)
        repo_check.assert_called_once_with("arii/remote-server")
        pr_check.assert_not_called()

    def test_dispatch_fails_on_missing_repo(self):
        with mock.patch.object(verify, "repo_exists", return_value=(False, "not found")):
            ok, reason = verify.verify_proposal(
                {
                    "kind": "dispatch",
                    "repo": "arii/ghost",
                    "branch": "main",
                    "task": "do it",
                },
                "arii/remote-server",
            )
        self.assertFalse(ok)
        self.assertIn("repo_exists", reason)

    def test_comment_checks_repo_then_pr(self):
        with mock.patch.object(
            verify, "repo_exists", side_effect=ok_repo
        ), mock.patch.object(verify, "pr_exists", side_effect=ok_pr) as pr_check:
            ok, reason = verify.verify_proposal(
                {
                    "kind": "comment",
                    "repo": "arii/remote-server",
                    "pr_number": 9826,
                    "body": "hi",
                },
                "arii/remote-server",
            )
        self.assertTrue(ok, reason)
        pr_check.assert_called_once_with("arii/remote-server", 9826)

    def test_comment_pr_number_must_be_real(self):
        with mock.patch.object(verify, "repo_exists", ok_repo), mock.patch.object(
            verify, "pr_exists", return_value=(False, "Could not resolve to a PullRequest")
        ):
            ok, reason = verify.verify_proposal(
                {
                    "kind": "review",
                    "repo": "arii/remote-server",
                    "pr_number": 999999,
                    "review_event": "comment",
                    "body": "hi",
                },
                "arii/remote-server",
            )
        self.assertFalse(ok)
        self.assertIn("pr_exists", reason)

    def test_comment_repo_checked_before_pr(self):
        # A fake repo and a fake PR: the reason names the FIRST failed
        # check, so the re-prompt fixes one thing at a time.
        with mock.patch.object(
            verify, "repo_exists", return_value=(False, "not found")
        ), mock.patch.object(verify, "pr_exists", side_effect=ok_pr) as pr_check:
            ok, reason = verify.verify_proposal(
                {
                    "kind": "comment",
                    "repo": "arii/ghost",
                    "pr_number": 7,
                    "body": "hi",
                },
                "arii/remote-server",
            )
        self.assertFalse(ok)
        self.assertIn("repo_exists", reason)
        pr_check.assert_not_called()

    def test_unknown_kind_passes_through(self):
        # Shape validation rejects unknown kinds before verify runs;
        # verify stays an addition, never a second validator that can
        # drift from the first.
        ok, reason = verify.verify_proposal({"kind": "nonsense"}, "arii/remote-server")
        self.assertTrue(ok, reason)

    def test_non_dict_args_fail_closed(self):
        ok, reason = verify.verify_proposal("not a dict", "arii/remote-server")
        self.assertFalse(ok)
        self.assertIn("shape", reason)


class RealCheckersCarryTheTarget(unittest.TestCase):
    """The real repo_exists/pr_exists format the offending repo/number
    into the detail — that is what lets the re-prompt fix the exact
    target instead of guessing (mocked-gate tests above pin only the
    check-order and check-name contract)."""

    def test_repo_exists_names_the_repo(self):
        with mock.patch.object(verify, "_gh", return_value=(False, "not found")):
            ok, detail = verify.repo_exists("arii/ghost")
        self.assertFalse(ok)
        self.assertIn("arii/ghost", detail)

    def test_pr_exists_names_the_number_and_repo(self):
        with mock.patch.object(verify, "_gh", return_value=(False, "Could not resolve")):
            ok, detail = verify.pr_exists("arii/remote-server", 999999)
        self.assertFalse(ok)
        self.assertIn("999999", detail)
        self.assertIn("arii/remote-server", detail)


class VerifyFailClosed(unittest.TestCase):
    def test_gh_failure_is_a_failed_check_not_an_exception(self):
        # _gh never raises: a gh outage surfaces as a VERIFY-FAILED
        # reason (fail closed — an unverified proposal never renders
        # a card), with the gh error carried in the detail.
        with mock.patch.object(
            verify,
            "_gh",
            return_value=(False, "gh unavailable: timeout"),
        ):
            ok, reason = verify.verify_proposal(
                {"kind": "issue", "title": "t", "body": "b"}, "arii/remote-server"
            )
        self.assertFalse(ok)
        self.assertIn("repo_exists", reason)
        self.assertIn("gh unavailable", reason)

    def test_gh_helper_survives_missing_binary(self):
        # Real _gh against a command that cannot exist: no exception.
        with mock.patch.object(verify, "GH_TIMEOUT", 5):
            ok, detail = verify._gh(["definitely-not-a-real-command-xyz"])
        self.assertFalse(ok)
        self.assertTrue(detail)


if __name__ == "__main__":
    unittest.main()
