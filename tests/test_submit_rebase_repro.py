import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

import pipeline


class TestSubmitAutoRebaseRepro(unittest.TestCase):
    def test_reproduce_verified_subbrief_submit_failure_when_branch_matches_base(self):
        """Reproduce: verified sub-brief result + audit approved, followed by
        auto-rebase ending in branch matching base. Currently fails with CHANGES_REQUESTED.
        """
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as wt_dir,
        ):
            pipe = pipeline.Pipeline(repo_dir, "test intent", issue_id=1568, submit_pr=True)
            pipe.budget = pipeline.new_budget([{"id": 1, "title": "part 1", "acceptance": []}])
            pipe.budget["sub_briefs"][0]["status"] = "done"
            pipe.budget["parent"]["verified"] = True

            with (
                mock.patch.object(pipe, "stage_init", return_value=(True, {})),
                mock.patch.object(pipe, "stage_spec", return_value=(True, {"valid": True}, "# Spec\n## Acceptance\n- [ ] ok")),
                mock.patch.object(pipe, "stage_code", return_value=(True, {}, wt_dir)),
                mock.patch.object(pipe, "pre_audit_check", return_value=(True, None, "")),
                mock.patch.object(pipe, "stage_audit", return_value=(pipeline.APPROVED, {}, "audit passed")),
                mock.patch.object(
                    pipe,
                    "td_run",
                    return_value=(
                        1,
                        json.dumps({
                            "status": "error",
                            "message": "No changes to submit (branch matches base)",
                            "code": 1,
                        }),
                    ),
                ),
            ):
                result = pipe.run()

            # Verified, audit-approved run when branch matches base must report APPROVED with evidence
            self.assertEqual(result["status"], pipeline.APPROVED)
            self.assertIn("No changes to submit (branch matches base)", result["detail"])
            self.assertIn("verified changes already present on base branch after auto-rebase", result["detail"])
            self.assertEqual(result["submit_url"], "")
            self.assertTrue(any("ALREADY_MERGED" in ev["message"] for ev in pipe.events if ev["stage"] == "submit"))

    def test_verified_subbrief_submit_produces_pr_when_changes_exist(self):
        """When changes exist vs base, stage_submit produces the PR URL."""
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as wt_dir,
        ):
            pipe = pipeline.Pipeline(repo_dir, "test intent", issue_id=1568, submit_pr=True)
            pipe.budget = pipeline.new_budget([{"id": 1, "title": "part 1", "acceptance": []}])
            pipe.budget["sub_briefs"][0]["status"] = "done"
            pipe.budget["parent"]["verified"] = True

            with (
                mock.patch.object(pipe, "stage_init", return_value=(True, {})),
                mock.patch.object(pipe, "stage_spec", return_value=(True, {"valid": True}, "# Spec\n## Acceptance\n- [ ] ok")),
                mock.patch.object(pipe, "stage_code", return_value=(True, {}, wt_dir)),
                mock.patch.object(pipe, "pre_audit_check", return_value=(True, None, "")),
                mock.patch.object(pipe, "stage_audit", return_value=(pipeline.APPROVED, {}, "audit passed")),
                mock.patch.object(
                    pipe,
                    "td_run",
                    return_value=(
                        0,
                        json.dumps({
                            "status": "ok",
                            "url": "https://github.com/arii/remote-server/pull/1741",
                        }),
                    ),
                ),
            ):
                result = pipe.run()

            self.assertEqual(result["status"], pipeline.APPROVED)
            self.assertEqual(result["submit_url"], "https://github.com/arii/remote-server/pull/1741")

    def test_verified_subbrief_submit_fails_when_other_error(self):
        """When td agent submit-pr fails with genuine error (e.g. push failure), reports CHANGES_REQUESTED."""
        with (
            tempfile.TemporaryDirectory() as repo_dir,
            tempfile.TemporaryDirectory() as wt_dir,
        ):
            pipe = pipeline.Pipeline(repo_dir, "test intent", issue_id=1568, submit_pr=True)
            pipe.budget = pipeline.new_budget([{"id": 1, "title": "part 1", "acceptance": []}])
            pipe.budget["sub_briefs"][0]["status"] = "done"
            pipe.budget["parent"]["verified"] = True

            with (
                mock.patch.object(pipe, "stage_init", return_value=(True, {})),
                mock.patch.object(pipe, "stage_spec", return_value=(True, {"valid": True}, "# Spec\n## Acceptance\n- [ ] ok")),
                mock.patch.object(pipe, "stage_code", return_value=(True, {}, wt_dir)),
                mock.patch.object(pipe, "pre_audit_check", return_value=(True, None, "")),
                mock.patch.object(pipe, "stage_audit", return_value=(pipeline.APPROVED, {}, "audit passed")),
                mock.patch.object(
                    pipe,
                    "td_run",
                    return_value=(
                        1,
                        json.dumps({
                            "status": "error",
                            "message": "Failed to push agent/issue-1568: connection refused",
                            "code": 1,
                        }),
                    ),
                ),
            ):
                result = pipe.run()

            self.assertEqual(result["status"], pipeline.CHANGES_REQUESTED)
            self.assertIn("submit failed:", result["detail"])
            self.assertIn("connection refused", result["detail"])

    def test_git_rebase_subsumes_verified_changes_leaving_branch_matching_base(self):
        """Git-level reproduction: when main already incorporated the changes,
        rebase --autostash leaves the branch matching base with zero diff and clean status.
        """
        with tempfile.TemporaryDirectory() as tmp:
            origin = os.path.join(tmp, "origin")
            clone = os.path.join(tmp, "clone")
            
            # Bare origin
            subprocess.run(["git", "init", "--bare", "-b", "main", origin], check=True, capture_output=True)
            
            # Clone repo
            subprocess.run(["git", "clone", origin, clone], check=True, capture_output=True)
            subprocess.run(["git", "-C", clone, "config", "user.email", "test@test.com"], check=True)
            subprocess.run(["git", "-C", clone, "config", "user.name", "Test"], check=True)
            
            # Initial commit on main
            with open(os.path.join(clone, "file.py"), "w") as f:
                f.write("initial = True\n")
            subprocess.run(["git", "-C", clone, "add", "."], check=True)
            subprocess.run(["git", "-C", clone, "commit", "-m", "initial"], check=True)
            subprocess.run(["git", "-C", clone, "push", "origin", "main"], check=True, capture_output=True)
            
            # Create feature branch
            subprocess.run(["git", "-C", clone, "checkout", "-b", "agent/issue-1568"], check=True, capture_output=True)
            
            # Meanwhile, origin/main has advanced and incorporated the verified fix:
            other = os.path.join(tmp, "other")
            subprocess.run(["git", "clone", origin, other], check=True, capture_output=True)
            subprocess.run(["git", "-C", other, "config", "user.email", "test@test.com"], check=True)
            subprocess.run(["git", "-C", other, "config", "user.name", "Test"], check=True)
            with open(os.path.join(other, "file.py"), "w") as f:
                f.write("initial = True\nverified_change = 42\n")
            subprocess.run(["git", "-C", other, "commit", "-am", "upstream landed fix"], check=True)
            subprocess.run(["git", "-C", other, "push", "origin", "main"], check=True, capture_output=True)
            
            # In our feature worktree, the sub-briefs implemented the exact verified fix:
            with open(os.path.join(clone, "file.py"), "w") as f:
                f.write("initial = True\nverified_change = 42\n")
                
            # Now run prepare_worktree_for_submit auto-rebase path:
            logs = []
            res = pipeline.prepare_worktree_for_submit(clone, clone, log_fn=logs.append)
            self.assertTrue(res)
            
            # Inspect the branch status:
            status = subprocess.run(["git", "-C", clone, "status", "--porcelain"], check=False, capture_output=True, text=True)
            diff = subprocess.run(["git", "-C", clone, "diff", "origin/main...HEAD"], check=False, capture_output=True, text=True)
            
            # The verified changes are on origin/main, so working tree is clean and diff is empty:
            self.assertEqual(status.stdout.strip(), "")
            self.assertEqual(diff.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
