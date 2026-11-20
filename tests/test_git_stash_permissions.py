"""Tests for Issue #1378: git stash permissions and shared repository access.

Verifies that:
1. Shared repositories with core.sharedRepository=group maintain group-writable
   refs and reflogs so non-root users (like host operator `ari`) can stash without
   hitting 'Cannot save the current status' (silent EACCES in do_store_stash).
2. scripts/update-permissions.sh registers all essential git stash operations.
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class TestGitStashPermissions(unittest.TestCase):
    def test_shared_repository_allows_stash_round_trip(self):
        """Verify core.sharedRepository=group allows stash creation and restore."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            repo.mkdir()

            env = dict(
                os.environ,
                GIT_AUTHOR_NAME="Test",
                GIT_AUTHOR_EMAIL="test@example.com",
                GIT_COMMITTER_NAME="Test",
                GIT_COMMITTER_EMAIL="test@example.com",
            )

            # Initialize repo with sharedRepository=group
            subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
            subprocess.run(
                ["git", "config", "core.sharedRepository", "group"],
                cwd=repo,
                env=env,
                check=True,
            )

            file1 = repo / "script.sh"
            file1.write_text("#!/bin/bash\necho hello\n")
            subprocess.run(["git", "add", "script.sh"], cwd=repo, env=env, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "initial"], cwd=repo, env=env, check=True
            )

            # Simulate an initial stash (creates refs/stash and logs/refs/stash)
            file1.write_text("#!/bin/bash\necho modified\n")
            untracked = repo / "untracked.txt"
            untracked.write_text("new untracked file\n")

            proc = subprocess.run(
                ["git", "stash", "push", "-u", "-m", "first_stash"],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("Saved working directory", proc.stdout)

            # Verify that refs/stash and logs/refs/stash exist and are group-writable
            stash_ref = repo / ".git" / "refs" / "stash"
            stash_log = repo / ".git" / "logs" / "refs" / "stash"
            self.assertTrue(stash_ref.exists())
            self.assertTrue(stash_log.exists())
            self.assertTrue(bool(stash_ref.stat().st_mode & 0o020))
            self.assertTrue(bool(stash_log.stat().st_mode & 0o020))

            # Stash pop restores both tracked and untracked changes
            subprocess.run(
                ["git", "stash", "pop"],
                cwd=repo,
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(file1.read_text(), "#!/bin/bash\necho modified\n")
            self.assertEqual(untracked.read_text(), "new untracked file\n")

    def test_update_permissions_script_has_git_stash_operations(self):
        """Verify update-permissions.sh includes push, pop, drop, apply stash rules."""
        from cli import repo_root

        script_path = Path(repo_root(), "scripts/update-permissions.sh")
        content = script_path.read_text(encoding="utf-8")

        for expected in (
            "command(git stash)",
            "command(git stash *)",
            "command(git stash push*)",
            "command(git stash pop*)",
            "command(git stash apply*)",
            "command(git stash drop*)",
            "core.sharedRepository",
        ):
            self.assertIn(expected, content)


if __name__ == "__main__":
    unittest.main()
