"""Issue #1577: the deterministic scope gate gets the current issue id.

`td verify-scope --issue <N>` allows exactly docs/specs/issue-<N>.md (the
pipeline's own generated spec) and nothing else. These tests run the real
`td` against a throwaway git repo (the gate diffs the worktree against
origin/main), so they prove the auditor/pipeline wiring end to end instead of
a model of the gate. CI installs td and sets REQUIRE_TD=1 so they cannot skip.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

import auditor

try:
    import langgraph  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False

if HAS_AGENT_DEPS:
    import pipeline

HAS_TD = shutil.which("td") is not None
if not HAS_TD and os.environ.get("REQUIRE_TD") == "1":
    raise ImportError("REQUIRE_TD=1 but td is not on PATH")

NUMBER = 1551
OWN_SPEC = f"docs/specs/issue-{NUMBER}.md"
SPEC_TEXT = "# Spec\n\nChange `calc.py`.\n"
SCOPE_FAILED = "verify-scope: gate reported passed=false"


def _git(root, *args):
    subprocess.run(
        ["git", "-C", root, *args],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
        },
    )


def _write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def make_worktree(root, changed):
    """Repo whose HEAD differs from origin/main by `changed` (path -> text).

    `td verify-scope` diffs the worktree against origin/main, so the base
    commit is published as that ref before the change is committed.
    """
    _git(root, "init", "-q", "-b", "main")
    _write(root, "calc.py", "def f():\n    return 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(root, "checkout", "-q", "-b", "feature")
    for rel, text in changed.items():
        _write(root, rel, text)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "change")


def _changed(*extra):
    files = {"calc.py": "def f():\n    return 2\n", OWN_SPEC: SPEC_TEXT}
    files.update({path: "x\n" for path in extra})
    return files


def _hard_gate(root, issue_id):
    graph = auditor.AuditorGraph(
        root,
        audit_fn=lambda gate: "ok",
        # Absolute: without a cwd-aware runner td would resolve it against
        # this process's cwd, not the worktree.
        spec_file=os.path.join(root, OWN_SPEC),
        issue_id=issue_id,
    )
    return graph.hard_gate()


@unittest.skipUnless(HAS_TD, "needs td on PATH")
class TestHardGateScopeIssue(unittest.TestCase):
    def gate(self, changed, issue_id=NUMBER):
        with tempfile.TemporaryDirectory() as root:
            make_worktree(root, changed)
            return _hard_gate(root, issue_id)

    def test_own_generated_spec_is_not_out_of_scope(self):
        valid, payloads, errors = self.gate(_changed())
        self.assertTrue(valid, errors)
        self.assertIn(OWN_SPEC, payloads["verify-scope"]["modified_files"])

    def test_absent_issue_rejects_spec(self):
        # None, 0 and False mean "no issue": the flag is omitted, so there
        # is no exemption.
        for issue_id in (None, 0, False):
            with self.subTest(issue_id=issue_id):
                valid, _payloads, errors = self.gate(_changed(), issue_id)
                self.assertFalse(valid)
                self.assertIn(SCOPE_FAILED, errors)

    def test_invalid_issue_fails_closed(self):
        # td rejects --issue below 1 or non-integer with a usage error (no
        # JSON), which run_td_gate reports as a failed gate.
        for issue_id in (-NUMBER, "abc"):
            with self.subTest(issue_id=issue_id):
                valid, payloads, errors = self.gate(_changed(), issue_id)
                self.assertFalse(valid)
                self.assertNotIn("verify-scope", payloads)
                self.assertEqual(len(errors), 1)

    def test_other_issue_spec_still_rejected(self):
        other = "docs/specs/issue-1552.md"
        valid, payloads, errors = self.gate(_changed(other))
        self.assertFalse(valid)
        self.assertEqual(payloads["verify-scope"]["violations"], [other])
        self.assertIn(SCOPE_FAILED, errors)

    def test_own_spec_does_not_exempt_other_issue_id(self):
        valid, payloads, _errors = self.gate(_changed(), NUMBER + 1)
        self.assertFalse(valid)
        self.assertEqual(payloads["verify-scope"]["violations"], [OWN_SPEC])

    def test_unrelated_and_noncanonical_paths_still_rejected(self):
        for path in (
            "extra.py",
            "docs/specs/other.md",
            "docs/specs/issue-01551.md",
            "docs/specs/issue-1551.md.bak",
            "docs/specs/sub/issue-1551.md",
            "sub/docs/specs/issue-1551.md",
        ):
            with self.subTest(path=path):
                valid, payloads, errors = self.gate(_changed(path))
                self.assertFalse(valid)
                self.assertEqual(payloads["verify-scope"]["violations"], [path])
                self.assertIn(SCOPE_FAILED, errors)


@unittest.skipUnless(HAS_TD, "needs td on PATH")
@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langgraph)")
class TestPipelineScopeIssue(unittest.TestCase):
    def _run(self, changed, audit_results):
        results = list(audit_results)
        with tempfile.TemporaryDirectory() as root:
            make_worktree(root, changed)
            # Real td (default td_run); the stages that need a model or a
            # worktree are stubbed so Pipeline.run() drives only the
            # audit -> repair -> pre-audit loop against the prepared repo.
            pipe = pipeline.Pipeline(
                root,
                "fix calc",
                issue_id=NUMBER,
                audit_fn=lambda gate: results.pop(0),
                pre_audit_fn=lambda wt: (True, None, ""),
                max_repair_cycles=3,
            )
            coder_calls = []
            pipe.stage_init = lambda: (True, "ready")
            pipe.stage_spec = lambda: (True, {}, "spec")

            def fake_code(brief, worktree_root=None, repair_notes=""):
                coder_calls.append(repair_notes)
                return True, {}, root

            pipe.stage_code = fake_code
            pipe.stage_cleanup = lambda wt: (True, "")
            return pipe.run(), coder_calls

    def test_audit_repair_preaudit_path_accepts_own_generated_spec(self):
        reject = json.dumps(
            {"verified": False, "findings": [{"severity": "high", "description": "x"}]}
        )
        approve = json.dumps({"verified": True, "findings": []})
        result, coder_calls = self._run(_changed(), [reject, approve])
        self.assertEqual(result["status"], pipeline.APPROVED, result)
        # Code, then repair after the rejected first audit: the second
        # deterministic pre-audit gate saw the generated spec and passed.
        self.assertEqual(len(coder_calls), 2)

    def test_unrelated_scope_failure_still_blocks_the_run(self):
        approve = json.dumps({"verified": True, "findings": []})
        result, _calls = self._run(_changed("extra.py"), [approve])
        self.assertEqual(result["status"], pipeline.BLOCKED, result)
        self.assertIn(SCOPE_FAILED, str(result))


if __name__ == "__main__":
    unittest.main()
