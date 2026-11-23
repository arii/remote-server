"""Immutable validated-spec run evidence (#1591)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline as pipeline_mod
from backend.tests.agent_control_case import AgentControlTestCase
from test_pipeline_e2e import ScriptedPipeline

SPEC = "# Original validated spec\n\n" + "Detailed plan line.\n" * 50


class SpecEvidenceCase(AgentControlTestCase):
    def setUp(self):
        super().setUp()
        self.run_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "S5_BUDGET_STATE": str(self.run_dir / "budget.json"),
                    "DEEP_AGENT_STATE_DIR": str(self.run_dir / "state"),
                },
            )
        )
        self.artifact = self.run_dir / "spec-validated.md"

    def pipe(self, script=("PASS",), spec=SPEC, spec_ok=True, **kwargs):
        pipe = ScriptedPipeline(list(script), **kwargs)
        pipe.stage_spec = lambda: (spec_ok, {}, spec)
        return pipe


class TestValidatedSpecArtifact(SpecEvidenceCase):
    def test_artifact_is_saved_before_the_first_code_call(self):
        pipe = self.pipe()
        seen = []
        original = pipe.stage_code

        def stage_code(brief, worktree_root=None, repair_notes=""):
            seen.append(self.artifact.read_bytes())
            return original(brief, worktree_root, repair_notes)

        pipe.stage_code = stage_code
        pipe.run()
        self.assertEqual(seen[0], SPEC.encode())

    def test_repair_rewriting_the_worktree_spec_leaves_artifact_identical(self):
        worktree = self.run_dir / "wt"
        working = worktree / "docs" / "specs" / "issue-1.md"
        working.parent.mkdir(parents=True)
        working.write_text(SPEC)
        pipe = self.pipe(script=("CHANGES_REQUESTED", "PASS"))
        original = pipe.stage_code

        def stage_code(brief, worktree_root=None, repair_notes=""):
            if repair_notes:
                working.write_text("short rewrite")
            return original(brief, str(worktree), repair_notes)

        pipe.stage_code = stage_code
        pipe.run()
        self.assertEqual(working.read_text(), "short rewrite")
        self.assertEqual(self.artifact.read_bytes(), SPEC.encode())

    def test_invalid_spec_writes_no_artifact(self):
        pipe = self.pipe(spec=["bad"], spec_ok=False)
        result = pipe.run()
        self.assertEqual(result["status"], pipeline_mod.BLOCKED)
        self.assertEqual(pipe.calls, [])
        self.assertFalse(self.artifact.exists())

    def test_empty_spec_writes_no_artifact_and_blocks_code(self):
        for empty in ("", "  \n"):
            with self.subTest(empty=empty):
                pipe = self.pipe(spec=empty)
                result = pipe.run()
                self.assertEqual(result["status"], pipeline_mod.BLOCKED)
                self.assertEqual(pipe.calls, [])
                self.assertFalse(self.artifact.exists())

    def test_persistence_failure_blocks_code_with_actionable_error(self):
        pipe = self.pipe()
        with mock.patch.object(
            pipeline_mod.os, "link", side_effect=OSError("disk full")
        ):
            result = pipe.run()
        self.assertEqual(result["status"], pipeline_mod.BLOCKED)
        self.assertIn("cannot save validated spec evidence", result["detail"])
        self.assertIn("disk full", result["detail"])
        self.assertEqual(pipe.calls, [])
        self.assertFalse(self.artifact.exists())
        self.assertEqual([p.name for p in self.run_dir.glob("*.tmp")], [])

    def test_attempted_overwrite_is_refused(self):
        pipeline_mod.save_validated_spec(str(self.artifact), SPEC)
        with self.assertRaisesRegex(pipeline_mod.SpecEvidenceError, "refusing"):
            pipeline_mod.save_validated_spec(str(self.artifact), "changed")
        self.assertEqual(self.artifact.read_bytes(), SPEC.encode())

    def test_resume_with_a_different_spec_blocks_before_code(self):
        self.pipe().run()
        pipe = self.pipe(spec="regenerated different spec")
        result = pipe.run()
        self.assertEqual(result["status"], pipeline_mod.BLOCKED)
        self.assertEqual(pipe.calls, [])
        self.assertEqual(self.artifact.read_bytes(), SPEC.encode())

    def test_identical_recapture_is_a_no_op(self):
        pipeline_mod.save_validated_spec(str(self.artifact), SPEC)
        pipeline_mod.save_validated_spec(str(self.artifact), SPEC)
        self.assertEqual(self.artifact.read_bytes(), SPEC.encode())


class TestRetryArchival(SpecEvidenceCase):
    def test_archived_run_keeps_original_and_new_run_gets_its_own(self):
        # finish_retry renames the whole run directory to the archive path.
        self.pipe().run()
        archive = self.run_dir.parent / (self.run_dir.name + "-retry-1")
        self.addCleanup(shutil.rmtree, archive, True)
        self.run_dir.rename(archive)
        self.run_dir.mkdir()
        new_spec = "# New run spec\n"
        self.pipe(spec=new_spec).run()
        self.assertEqual((archive / "spec-validated.md").read_bytes(), SPEC.encode())
        self.assertEqual(self.artifact.read_bytes(), new_spec.encode())


if __name__ == "__main__":
    unittest.main()
