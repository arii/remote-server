"""skills.py unit tests. Stdlib only, no langchain. Run: python3
tests/test_skills.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import skills


class TestLoader(unittest.TestCase):
    def test_lists_shipped_skills(self):
        names = skills.list_skills()
        self.assertIn("repo-review", names)
        self.assertIn("dispatch-brief", names)

    def test_load_returns_body(self):
        body = skills.load_skill("repo-review")
        self.assertIn("# Repo Review", body)
        self.assertIn("read-only", body)

    def test_missing_skill_tolerated(self):
        self.assertEqual(skills.load_skill("no-such-skill"), "")
        self.assertEqual(skills.load_skill("../escape"), "")
        self.assertEqual(skills.load_skill(""), "")

    def test_frontmatter_parsed(self):
        name, desc, _ = skills.parse_skill_file(
            skills.skill_path("repo-review"))
        self.assertEqual(name, "repo-review")
        self.assertTrue(desc)


class TestIntentMap(unittest.TestCase):
    def test_review_loads_repo_review(self):
        addition = skills.system_addition("review")
        self.assertIn("# Repo Review", addition)

    def test_implement_loads_dispatch_brief(self):
        addition = skills.system_addition("implement")
        self.assertIn("# Dispatch Brief", addition)
        self.assertIn("dispatch", addition)

    def test_fix_loads_dispatch_brief(self):
        self.assertIn("# Dispatch Brief", skills.system_addition("fix"))

    def test_explain_loads_nothing(self):
        self.assertEqual(skills.system_addition("explain"), "")
        self.assertEqual(skills.system_addition("bogus"), "")


class TestToolSurfaceGuardrail(unittest.TestCase):
    def test_skills_reference_only_real_surfaces(self):
        # Regression pin for the item-6 finding: pi-config skills
        # target MCP tools this agent lacks. Agent-native skills must
        # only name surfaces that exist here: the repo tools, the
        # virtual harness fs tools (to steer away from them), and
        # propose_action. No MCP, shell, or CLI references.
        allowed = {"list_dir", "grep_repo", "read_repo_file",
                   "search_docs", "propose_action"}
        banned = ("mcp__", "mcp.", "shell", "exec(", "subprocess",
                  "td --", "gh pr", "gh issue", "npx", "docker ")
        for name in skills.list_skills():
            body = skills.load_skill(name)
            self.assertTrue(body, name)
            for token in banned:
                self.assertNotIn(token, body, f"{name}: {token!r}")
            self.assertTrue(
                any(tool in body for tool in allowed), name)
            self.assertIn("virtual", body, f"{name}: must warn")


if __name__ == "__main__":
    unittest.main()
