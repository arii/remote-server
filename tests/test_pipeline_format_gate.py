import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from backend.shared.cli import repo_root
import pipeline


class PipelineFormatGateTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.repo = os.path.join(self.tmpdir, "repo")
        os.makedirs(self.repo, exist_ok=True)
        # Initialize bare origin and clone or git repo
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

        # Copy check-format-diff.sh into repo scripts
        scripts_dir = os.path.join(self.repo, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        real_script = os.path.join(repo_root(), "scripts", "check-format-diff.sh")
        shutil.copy2(real_script, os.path.join(scripts_dir, "check-format-diff.sh"))

        # Initial commit on main
        readme = os.path.join(self.repo, "README.md")
        with open(readme, "w") as f:
            f.write("# Repo\n")
        subprocess.run(
            ["git", "add", "."], cwd=self.repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_unformatted_changed_file_is_formatted_and_check_passes(self):
        """(a) Unformatted changed file is formatted and check passes before submit."""
        # Create a branch / worktree
        branch_dir = os.path.join(self.tmpdir, "wt")
        subprocess.run(
            ["git", "worktree", "add", "-b", "feat/test-a", branch_dir, "main"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

        # Write unformatted python file
        unformatted_py = os.path.join(branch_dir, "module.py")
        with open(unformatted_py, "w") as f:
            f.write("def add(  a,  b ):\n    x = [ 1, 2,  3]\n    return a+b+x[0]\n")

        evidence = []
        logs = []
        res = pipeline.prepare_worktree_for_submit(
            self.repo,
            branch_dir,
            base_ref="main",
            log_fn=logs.append,
            evidence_fn=evidence.append,
        )

        self.assertTrue(res)
        self.assertTrue(bool(res))
        with open(unformatted_py) as f:
            formatted_content = f.read()

        # Ruff formats function signature and list spacing
        self.assertIn("def add(a, b):", formatted_content)
        self.assertIn("x = [1, 2, 3]", formatted_content)

        # Evidence records the formatting step
        self.assertTrue(
            any(e.get("status") == "formatted_and_passed" for e in evidence)
        )
        ev = next(e for e in evidence if e.get("status") == "formatted_and_passed")
        self.assertEqual(ev["command"], "ruff format")
        self.assertIn("module.py", ev["files"])
        self.assertEqual(ev["exit_code"], 0)

    def test_already_formatted_change_passes_with_no_files_modified(self):
        """(b) Already-formatted change passes with no files modified by format step."""
        branch_dir = os.path.join(self.tmpdir, "wt-b")
        subprocess.run(
            ["git", "worktree", "add", "-b", "feat/test-b", branch_dir, "main"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

        formatted_py = os.path.join(branch_dir, "clean.py")
        content = "def clean():\n    return 42\n"
        with open(formatted_py, "w") as f:
            f.write(content)

        evidence = []
        logs = []
        res = pipeline.prepare_worktree_for_submit(
            self.repo,
            branch_dir,
            base_ref="main",
            log_fn=logs.append,
            evidence_fn=evidence.append,
        )

        self.assertTrue(res)
        with open(formatted_py) as f:
            self.assertEqual(f.read(), content)

        ev = next((e for e in evidence if e.get("status") == "passed"), None)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["files"], [])
        self.assertEqual(ev["exit_code"], 0)

    def test_unfixable_format_failure_blocks_submit_with_output_recorded(self):
        """(c) Unfixable format failure blocks submit with format output recorded."""
        branch_dir = os.path.join(self.tmpdir, "wt-c")
        subprocess.run(
            ["git", "worktree", "add", "-b", "feat/test-c", branch_dir, "main"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

        pipe = pipeline.Pipeline(self.repo, "test intent", issue_id=1744)

        # Mock check-format-diff to always fail (unfixable)
        bad_output = "error: Failed to parse broken.py: SyntaxError: unexpected EOF"
        fake_proc = subprocess.CompletedProcess(
            args=["bash", "check-format-diff.sh"],
            returncode=1,
            stdout="",
            stderr=bad_output,
        )

        with mock.patch("subprocess.run") as mock_sub:

            def side_effect(cmd, **kwargs):
                if any("check-format-diff.sh" in str(arg) for arg in cmd):
                    return fake_proc
                if "merge-base" in cmd:
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout="main\n", stderr=""
                    )
                if "diff" in cmd or "ls-files" in cmd:
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout=b"broken.py\0", stderr=""
                    )
                if "rebase" in cmd or "fetch" in cmd:
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=0, stdout="", stderr=""
                    )
                if "format" in cmd:
                    return subprocess.CompletedProcess(
                        args=cmd, returncode=1, stdout="", stderr="failed to parse"
                    )
                return subprocess.CompletedProcess(
                    args=cmd, returncode=0, stdout="", stderr=""
                )

            mock_sub.side_effect = side_effect

            # Broken file on disk so find_changed_python_files sees it
            with open(os.path.join(branch_dir, "broken.py"), "w") as f:
                f.write("def foo(:")

            ok, err = pipe.stage_submit(branch_dir, "test pr")

        self.assertFalse(ok)
        self.assertIn("format gate failed", err)
        self.assertIn("ruff format check failed", err)
        self.assertIn(bad_output, err)
        self.assertIsNotNone(pipe.format_evidence)
        self.assertEqual(pipe.format_evidence["status"], "failed")
        self.assertIn("broken.py", pipe.format_evidence["files"])

    def test_files_outside_diff_never_reformatted(self):
        """(d) Files outside diff are never reformatted, including legacy files that fail full-tree check."""
        # Add legacy unformatted file to main
        legacy_file = os.path.join(self.repo, "legacy_file.py")
        legacy_code = "def legacy(   x,   y ):\n    z = [  1,  2 ]\n    return x+y\n"
        with open(legacy_file, "w") as f:
            f.write(legacy_code)
        subprocess.run(
            ["git", "add", "."], cwd=self.repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "add legacy unformatted"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

        # Create branch
        branch_dir = os.path.join(self.tmpdir, "wt-d")
        subprocess.run(
            ["git", "worktree", "add", "-b", "feat/test-d", branch_dir, "main"],
            cwd=self.repo,
            check=True,
            capture_output=True,
        )

        # Touch only touched_feature.py
        feature_file = os.path.join(branch_dir, "touched_feature.py")
        with open(feature_file, "w") as f:
            f.write("def feature(  a ):\n    return a*2\n")

        evidence = []
        logs = []
        res = pipeline.prepare_worktree_for_submit(
            self.repo,
            branch_dir,
            base_ref="main",
            log_fn=logs.append,
            evidence_fn=evidence.append,
        )

        self.assertTrue(res)

        # touched_feature.py was reformatted
        with open(feature_file) as f:
            self.assertEqual(f.read(), "def feature(a):\n    return a * 2\n")

        # legacy_file.py in worktree was NOT reformatted
        with open(os.path.join(branch_dir, "legacy_file.py")) as f:
            self.assertEqual(f.read(), legacy_code)

        # Evidence only names touched_feature.py
        ev = next(e for e in evidence if e.get("status") == "formatted_and_passed")
        self.assertEqual(ev["files"], ["touched_feature.py"])


if __name__ == "__main__":
    unittest.main()
