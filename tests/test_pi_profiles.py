#!/usr/bin/env python3
"""Tests for Pi task-scoped tool profiles, mcp exposure, and container config."""

import json
import os
import stat
import unittest


class TestPiProfiles(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.bin_dir = os.path.join(self.repo_root, "terminal", "bin")

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

    def test_pi_review_profile_definition(self):
        script_path = os.path.join(self.bin_dir, "pi-review")
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("mcp__boomtick__github_get_pr_diff", content)
        self.assertIn("mcp__boomtick__github_comment_triage_summary", content)

    def test_pi_triage_profile_definition(self):
        script_path = os.path.join(self.bin_dir, "pi-triage")
        with open(script_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("mcp__boomtick__github_search_open_prs", content)
        self.assertIn("mcp__boomtick__repo_read_ci_logs", content)
        self.assertIn("mcp__boomtick__repo_get_changed_files", content)

    def test_dockerfile_installs_profiles(self):
        dockerfile_path = os.path.join(self.repo_root, "terminal", "Dockerfile")
        with open(dockerfile_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("COPY bin/ /usr/local/bin/", content)
        self.assertIn("chmod +x /usr/local/bin/pi-*", content)

    def test_dockerfile_installs_tmux_and_vim(self):
        dockerfile_path = os.path.join(self.repo_root, "terminal", "Dockerfile")
        with open(dockerfile_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("tmux", content)
        self.assertIn(" vim ", content)

    def test_dockerfile_installs_vimrc(self):
        dockerfile_path = os.path.join(self.repo_root, "terminal", "Dockerfile")
        with open(dockerfile_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("COPY vimrc.local /etc/vim/vimrc.local", content)

    def test_mcp_config_valid_and_selective_exposure(self):
        mcp_path = os.path.join(self.repo_root, "terminal", "pi-config", "mcp.json")
        with open(mcp_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("mcpServers", data)
        self.assertIn("boomtick", data["mcpServers"])
        boomtick = data["mcpServers"]["boomtick"]
        self.assertIn("toolExposure", boomtick)
        exposure = boomtick["toolExposure"]
        self.assertEqual(exposure.get("github_get_pr_diff"), "direct")
        self.assertNotIn("github_comment_triage_summary", exposure)
        self.assertEqual(exposure.get("github_search_open_prs"), "direct")
        self.assertEqual(exposure.get("repo_read_ci_logs"), "direct")
        self.assertEqual(exposure.get("repo_get_changed_files"), "direct")
        self.assertEqual(exposure.get("*.*"), "hidden")

    def test_docs_describe_tool_profiles_and_benchmarks(self):
        docs_path = os.path.join(self.repo_root, "docs", "pi-setup.md")
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
