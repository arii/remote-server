"""Pin the ChatOps trigger author gate (#528).

Any PR comment fires issue_comment[created]; the job `if` must restrict
dispatch to trusted authors before any secret-bearing step runs.
"""
import os
import unittest

WORKFLOW = os.path.join(
    os.path.dirname(__file__), "..",
    ".github", "workflows", "chatops-trigger.yml")


def _job_if_line():
    with open(WORKFLOW, encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped.startswith("if:"):
                return stripped
    return ""


class TestChatOpsGate(unittest.TestCase):
    def test_job_gates_untrusted_authors(self):
        cond = _job_if_line()
        self.assertIn("github.event.issue.pull_request", cond)
        self.assertIn("github.event.comment.author_association", cond)
        for role in ("OWNER", "MEMBER", "COLLABORATOR"):
            self.assertIn(role, cond)
