"""Regression tests for #1216: eval_pipeline offline fixture gate + CI job.

Covers the ``--verify-fixtures`` mode added to
``deep-agent/eval_pipeline.py`` (four fail-loud checks, exit non-zero
naming the drifted check) and the path filter of the ``eval-fixtures``
CI job in ``.github/workflows/ci.yml``.

Stdlib only (no models, no network); the suite-discrimination probes
shell out to pytest in temp dirs, mirroring the CI job.
"""

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest import mock

import eval_pipeline
from cli import repo_root

ROOT = repo_root()
CI_YML = os.path.join(ROOT, ".github", "workflows", "ci.yml")

EXPECTED_CHECKS = [
    "fixtures-compile",
    "spec-gate-criterion",
    "suite-discriminates",
    "gate-contract",
]


class TestVerifyFixturesPassesOnMain(unittest.TestCase):
    def test_all_four_checks_pass(self):
        checks, ok = eval_pipeline.verify_fixtures()
        self.assertTrue(ok, checks)
        self.assertEqual([c["name"] for c in checks], EXPECTED_CHECKS)
        for check in checks:
            self.assertTrue(check["passed"], check)
            self.assertTrue(check["detail"], check)

    def test_cli_flag_exits_zero_with_json_report(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = eval_pipeline.main(["--verify-fixtures"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["passed"])
        self.assertEqual(
            [c["name"] for c in payload["verify_fixtures"]], EXPECTED_CHECKS
        )


class TestFixtureDriftProbeFailsLoud(unittest.TestCase):
    """Each drifted fixture must fail its own check by name (#1216 DoD)."""

    def test_uncompilable_fixture_names_fixtures_compile(self):
        with mock.patch.object(eval_pipeline, "CALC_FIXED", "def broken(:\n"):
            check = eval_pipeline._check_fixtures_compile()
        self.assertFalse(check["passed"])
        self.assertEqual(check["name"], "fixtures-compile")
        self.assertIn("CALC_FIXED", check["detail"])

    def test_spec_without_acceptance_section_names_spec_gate(self):
        bad = "# Empty spec\n\nNo acceptance section here.\n"
        with mock.patch.object(eval_pipeline, "SPEC_DRAFT", bad):
            check = eval_pipeline._check_spec_gate_criterion()
        self.assertFalse(check["passed"])
        self.assertEqual(check["name"], "spec-gate-criterion")

    def test_indiscriminate_suite_names_suite_discriminates(self):
        # Buggy module identical to the fixed one: the suite goes green
        # on both, so it no longer discriminates (the #1216 drift probe).
        with mock.patch.object(eval_pipeline, "CALC_BUGGY", eval_pipeline.CALC_FIXED):
            check = eval_pipeline._check_suite_discriminates()
        self.assertFalse(check["passed"])
        self.assertEqual(check["name"], "suite-discriminates")
        self.assertIn("no longer discriminates", check["detail"])

    def test_empty_eval_registry_names_gate_contract(self):
        with mock.patch.object(eval_pipeline, "EVALS", []):
            check = eval_pipeline._check_gate_contract()
        self.assertFalse(check["passed"])
        self.assertEqual(check["name"], "gate-contract")

    def test_cli_drift_exits_nonzero_naming_the_check(self):
        buf = io.StringIO()
        with mock.patch.object(eval_pipeline, "CALC_BUGGY", eval_pipeline.CALC_FIXED):
            with redirect_stdout(buf):
                code = eval_pipeline.main(["--verify-fixtures"])
        self.assertNotEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertFalse(payload["passed"])
        failed = [c["name"] for c in payload["verify_fixtures"] if not c["passed"]]
        self.assertIn("suite-discriminates", failed)


def _ci_job_text():
    with open(CI_YML, encoding="utf-8") as handle:
        return handle.read()


def _eval_job_block(text):
    start = text.index("  eval-fixtures:")
    end = text.index("\n  format:", start)
    return text[start:end]


class TestEvalFixturesCiGate(unittest.TestCase):
    """The CI job must fire on backend/deep-agent/.agents/ and stay
    quiet (step skipped, job green) otherwise. Wiring assertions pin the
    filter; the diff-command test below exercises its exact shape."""

    def test_job_exists_and_feeds_report(self):
        text = _ci_job_text()
        self.assertIn("  eval-fixtures:", text)
        self.assertIn(
            "python3 deep-agent/eval_pipeline.py --verify-fixtures",
            _eval_job_block(text),
        )
        needs_line = [
            line for line in text.splitlines() if line.strip().startswith("needs:")
        ]
        self.assertTrue(
            any("eval-fixtures" in line for line in needs_line),
            "report aggregator must wait on eval-fixtures",
        )

    def test_path_filter_covers_exactly_the_eval_scope(self):
        job = _eval_job_block(_ci_job_text())
        self.assertIn(
            "git diff --name-only", job, "filter must diff the PR against its base"
        )
        self.assertIn("backend deep-agent .agents", job)
        self.assertIn("origin/${{ github.base_ref }}", job)
        # Non-PR events (push to main) always run the check.
        self.assertIn('"${{ github.event_name }}" != "pull_request"', job)
        self.assertIn('echo "matched=true"', job)

    def test_unmatched_prs_skip_the_step_but_stay_green(self):
        job = _eval_job_block(_ci_job_text())
        self.assertIn("matched=false", job)
        self.assertIn(
            "if: steps.scope.outputs.matched == 'true'",
            job,
            "unrelated PRs must skip the check step, not fail it",
        )

    def test_filter_shape_matches_this_branch_and_ignores_unrelated(self):
        try:
            subprocess.run(
                ["git", "rev-parse", "--verify", "origin/main"],
                cwd=ROOT,
                capture_output=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            self.skipTest("no origin/main ref here")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        main = subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if head == main:
            self.skipTest("on main: no PR diff to filter")
        base = ["git", "diff", "--name-only", "origin/main...HEAD", "--"]
        in_scope = subprocess.run(
            base + ["backend", "deep-agent", ".agents"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(in_scope.returncode, 0)
        # This branch touches deep-agent/, so the filter must fire.
        self.assertTrue(
            in_scope.stdout.strip(), "filter must match a branch touching deep-agent/"
        )
        out_of_scope = subprocess.run(
            base + ["docs/evaluation-and-next-steps.md"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(out_of_scope.returncode, 0)
        self.assertEqual(
            out_of_scope.stdout.strip(), "", "filter must stay quiet on unrelated paths"
        )


if __name__ == "__main__":
    unittest.main()
