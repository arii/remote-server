"""Pin the ChatOps trigger author gate (#528, hardened in #549).

Any PR comment fires issue_comment[created]; the job `if` must restrict
dispatch to trusted authors before any secret-bearing step runs.
Operator-blind substring checks are not enough: an inverted gate
(`!=`) must fail, not pass.
"""
import os
import unittest

WORKFLOW = os.path.join(
    ".github", "workflows", "chatops-trigger.yml")

ROLES = ("OWNER", "MEMBER", "COLLABORATOR")


def _job_if_line():
    with open(WORKFLOW, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("if:"):
                return stripped
    return ""


def check_gate(case, cond):
    """Assert cond is a trusted-authors-only dispatch gate."""
    case.assertIn("github.event.issue.pull_request", cond)
    case.assertIn("github.event.comment.author_association", cond)
    for role in ROLES:
        case.assertIn(f"author_association == '{role}'", cond)
    case.assertNotIn("!=", cond)


class TestChatOpsGate(unittest.TestCase):
    def test_job_gates_untrusted_authors(self):
        check_gate(self, _job_if_line())

    def test_inverted_gate_fails(self):
        inverted = _job_if_line().replace("==", "!=")
        self.assertNotEqual(inverted, _job_if_line())
        with self.assertRaises(AssertionError):
            check_gate(self, inverted)
