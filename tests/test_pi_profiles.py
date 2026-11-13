#!/usr/bin/env python3
"""Tests for Pi task-scoped tool profiles, mcp exposure, and container config."""

import json
import os
import stat
import unittest


class TestPiProfiles(unittest.TestCase):
    def setUp(self):
        self.bin_dir = os.path.join("terminal", "bin")

    def test_profile_scripts_exist_and_executable(self):
        scripts = ["pi-code", "pi-review", "pi-triage"]
        for name in scripts:
            script_path = os.path.join(self.bin_dir, name)
            self.assertTrue(os.path.isfile(script_path), f"{name} must exist in terminal/bin/")
            mode = os.stat(script_path).st_mode
            self.assertTrue(bool(mode & stat.S_IXUSR), f"{name} must be executable")

    def test_pi_code_profile_definition(self):
        script_path = os.path.join(self.bin_dir, "pi-code")
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("--no-mcp", content)
        self.assertIn("--tools read,edit,write,bash", content)

    def _exec_line(self, name):
        script_path = os.path.join(self.bin_dir, name)
        with open(script_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("exec pi "):
                    return line
        self.fail(f"{name} has no exec pi line")

    def test_pi_review_profile_definition(self):
        exec_line = self._exec_line("pi-review")
        self.assertIn("mcp__boomtick__github_pr", exec_line)
        self.assertIn("mcp__boomtick__github_issue", exec_line)
        self.assertNotIn("github_get_pr", exec_line)
        self.assertNotIn("github_issue_comment", exec_line)
        self.assertNotIn("comment_triage_summary", exec_line)

    def test_pi_triage_profile_definition(self):
        exec_line = self._exec_line("pi-triage")
        self.assertIn("mcp__boomtick__github_pr", exec_line)
        self.assertIn("mcp__boomtick__github_issue", exec_line)
        self.assertNotIn("repo_read_ci_logs", exec_line)
        self.assertNotIn("github_get_pr", exec_line)
        self.assertNotIn("github_issue_comment", exec_line)
        self.assertNotIn("search_open_prs", exec_line)
        self.assertNotIn("get_changed_files", exec_line)

    def test_dockerfile_installs_profiles(self):
        dockerfile_path = os.path.join("terminal", "Dockerfile")
        with open(dockerfile_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("COPY bin/ /usr/local/bin/", content)
        self.assertIn("chmod +x /usr/local/bin/pi-*", content)

    def test_dockerfile_installs_tmux_and_vim(self):
        dockerfile_path = os.path.join("terminal", "Dockerfile")
        with open(dockerfile_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("tmux", content)
        self.assertIn(" vim ", content)

    def test_dockerfile_installs_vimrc(self):
        dockerfile_path = os.path.join("terminal", "Dockerfile")
        with open(dockerfile_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("COPY vimrc.local /etc/vim/vimrc.local", content)

    def test_mcp_config_valid_and_selective_exposure(self):
        mcp_path = os.path.join("terminal", "pi-config", "mcp.json")
        with open(mcp_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("mcpServers", data)
        self.assertIn("boomtick", data["mcpServers"])
        boomtick = data["mcpServers"]["boomtick"]
        self.assertIn("toolExposure", boomtick)
        exposure = boomtick["toolExposure"]
        self.assertEqual(exposure.get("*.*"), "hidden")
        # Removed tools stay out (#1105: never existed on the live
        # surface, or dropped by the td worktree split).
        for tool in ("github_get_pr_diff", "github_search_open_prs",
                     "github_comment_triage_summary",
                     "repo_get_changed_files", "td_init_and_rag",
                     "worktree_create", "worktree_list",
                     "worktree_remove"):
            self.assertNotIn(tool, exposure, tool)
        # The 8 names retired by the 2026-11 surface consolidation
        # (boomtick PR #1259) stay out of the allowlist: they now
        # return "Tool not found".
        for tool in ("boomtick_health", "repo_read_agent_context",
                     "repo_read_ci_logs", "repo_run_tests",
                     "github_get_pr", "github_issue_view",
                     "github_issue_update", "github_issue_comment"):
            self.assertNotIn(tool, exposure, tool)
        # The 11-tool live surface (post boomtick PR #1259) stays
        # directly callable: the wildcard hides everything not listed
        # here.
        for tool in ("repo_context", "repo_test",
                     "github_pr", "github_issue",
                     "github_create_issue", "agent_submit_pr",
                     "td_init", "td_rag", "td_validate_issue",
                     "td_verify_scope", "td_audit_worktree"):
            self.assertEqual(exposure.get(tool), "direct", tool)

    def test_docs_describe_tool_profiles_and_benchmarks(self):
        docs_path = os.path.join("docs", "pi-setup.md")
        with open(docs_path, "r", encoding="utf-8") as f:
            docs = f.read()
        self.assertIn("pi-code", docs)
        self.assertIn("pi-review", docs)
        self.assertIn("pi-triage", docs)
        self.assertIn("< 10s", docs)
        self.assertIn("2.7s", docs)
        self.assertIn("190s", docs)


if __name__ == "__main__":
    unittest.main()
