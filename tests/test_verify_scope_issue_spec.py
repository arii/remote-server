"""Issue #1577: the deterministic scope gate gets the current issue id.

`td verify-scope --issue <N>` allows exactly docs/specs/issue-<N>.md (the
pipeline's own generated spec) and nothing else. Real `td` is not required:
`ScopeGateFake` models that contract so these tests prove the pipeline
passes the issue through and that every other scope failure still blocks.
"""

import json
import re
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


def _is_canonical_issue(value):
    return re.fullmatch(r"[1-9][0-9]*", value) is not None


class ScopeGateFake:
    """Fake td runner: verify-scope allows spec-listed files plus, only
    with a matching positive `--issue N`, exactly docs/specs/issue-N.md."""

    def __init__(self, changed):
        self.changed = list(changed)
        self.scope_argvs = []

    def __call__(self, argv):
        name = argv[0]
        if name != "verify-scope":
            return 0, json.dumps(
                {
                    "status": "success",
                    "passed": True,
                    "impacted_files": [],
                    "dependent_tests": [],
                    "violations": [],
                }
            )
        self.scope_argvs.append(list(argv))
        allowed = {"calc.py"}
        if "--issue" in argv:
            issue = argv[argv.index("--issue") + 1]
            if _is_canonical_issue(issue):
                allowed.add(f"docs/specs/issue-{issue}.md")
        violations = [f for f in self.changed if f not in allowed]
        return (0 if not violations else 2), json.dumps(
            {
                "status": "success",
                "passed": not violations,
                "changed_files": self.changed,
                "violations": [{"file": f, "rule": "scope"} for f in violations],
            }
        )


def _hard_gate(changed, issue_id, spec_file="docs/specs/issue-1551.md"):
    fake = ScopeGateFake(changed)
    with tempfile.TemporaryDirectory() as tmp:
        graph = auditor.AuditorGraph(
            tmp,
            audit_fn=lambda gate: "ok",
            td_runner=fake,
            spec_file=spec_file,
            issue_id=issue_id,
        )
        valid, _payloads, errors = graph.hard_gate()
    return fake, valid, errors


class TestHardGateScopeIssue(unittest.TestCase):
    def test_issue_is_passed_to_verify_scope(self):
        fake, valid, errors = _hard_gate(["calc.py"], 1551)
        self.assertTrue(valid, errors)
        self.assertEqual(len(fake.scope_argvs), 1)
        argv = fake.scope_argvs[0]
        self.assertEqual(argv[argv.index("--issue") + 1], "1551")

    def test_own_generated_spec_is_not_out_of_scope(self):
        _fake, valid, errors = _hard_gate(["calc.py", "docs/specs/issue-1551.md"], 1551)
        self.assertTrue(valid, errors)
        self.assertEqual(errors, [])

    def test_absent_issue_omits_flag_and_rejects_spec(self):
        for issue_id in (None, 0, False):
            with self.subTest(issue_id=issue_id):
                fake, valid, errors = _hard_gate(
                    ["calc.py", "docs/specs/issue-1551.md"], issue_id
                )
                self.assertNotIn("--issue", fake.scope_argvs[0])
                self.assertFalse(valid)
                self.assertIn("verify-scope: gate reported passed=false", errors)

    def test_invalid_issue_rejects_spec(self):
        for issue_id in (-1551, "abc"):
            with self.subTest(issue_id=issue_id):
                _fake, valid, _errors = _hard_gate(
                    ["calc.py", "docs/specs/issue-1551.md"], issue_id
                )
                self.assertFalse(valid)

    def test_other_issue_spec_still_rejected(self):
        _fake, valid, _errors = _hard_gate(
            ["calc.py", "docs/specs/issue-1552.md"], 1551
        )
        self.assertFalse(valid)

    def test_unrelated_and_noncanonical_paths_still_rejected(self):
        for path in (
            "extra.py",
            "docs/specs/other.md",
            "docs/specs/issue-01551.md",
            "docs/specs/../specs/issue-1551.md",
            "../docs/specs/issue-1551.md",
            "docs/specs/issue-1551.md/x",
            "/docs/specs/issue-1551.md",
        ):
            with self.subTest(path=path):
                _fake, valid, errors = _hard_gate(["calc.py", path], 1551)
                self.assertFalse(valid)
                self.assertIn("verify-scope: gate reported passed=false", errors)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langgraph)")
class TestPipelineScopeIssue(unittest.TestCase):
    def _pipeline(self, root, changed, audit_results):
        fake = ScopeGateFake(changed)
        results = list(audit_results)
        pipe = pipeline.Pipeline(
            root,
            "fix calc",
            issue_id=1551,
            td_run=lambda argv, cwd: fake(argv),
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
        return pipe, fake, coder_calls

    def test_audit_repair_preaudit_path_accepts_own_generated_spec(self):
        reject = json.dumps(
            {"verified": False, "findings": [{"severity": "high", "description": "x"}]}
        )
        approve = json.dumps({"verified": True, "findings": []})
        with tempfile.TemporaryDirectory() as root:
            pipe, fake, coder_calls = self._pipeline(
                root, ["calc.py", "docs/specs/issue-1551.md"], [reject, approve]
            )
            result = pipe.run()
        self.assertEqual(result["status"], pipeline.APPROVED, result)
        # Audit -> repair -> second audit: the gate ran twice, both with the issue.
        self.assertEqual(len(coder_calls), 2)
        self.assertEqual(len(fake.scope_argvs), 2)
        for argv in fake.scope_argvs:
            self.assertEqual(argv[argv.index("--issue") + 1], "1551")

    def test_unrelated_scope_failure_still_blocks_the_run(self):
        approve = json.dumps({"verified": True, "findings": []})
        with tempfile.TemporaryDirectory() as root:
            pipe, _fake, _calls = self._pipeline(
                root, ["calc.py", "docs/specs/issue-1551.md", "extra.py"], [approve]
            )
            result = pipe.run()
        self.assertEqual(result["status"], pipeline.BLOCKED, result)
        self.assertIn("verify-scope: gate reported passed=false", str(result))


if __name__ == "__main__":
    unittest.main()
