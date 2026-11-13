"""Unified skill registry wiring tests (#1103). Stdlib only, no langchain.

The old intent loader (deep-agent/skills.py + agent_skills/) is gone:
intent-routed skills now live in deep-agent/skills/*.md with
intent_* trigger nodes, injected through SkillsRegistry.
"""
import os
import unittest

from skills_registry import SkillsRegistry

REPO_SKILLS = os.path.join("deep-agent", "skills")


def _reg():
    from pathlib import Path
    return SkillsRegistry(skills_dir=Path(REPO_SKILLS))


class TestIntentWiring(unittest.TestCase):
    def test_review_injects_repo_review(self):
        out = _reg().inject_skills_for_node("intent_review", "hint")
        self.assertIn("# Repo Review", out)
        self.assertIn("hint", out)

    def test_implement_and_fix_inject_dispatch_brief(self):
        for node in ("intent_implement", "intent_fix"):
            out = _reg().inject_skills_for_node(node, "hint")
            self.assertIn("# Dispatch Brief", out, node)

    def test_unknown_intent_returns_base_unchanged(self):
        # A node no skill registers for must inject nothing. This used
        # to use intent_explain — now a live co-browsing trigger node
        # (#1301) — so it pins a genuinely unregistered node instead.
        self.assertEqual(
            _reg().inject_skills_for_node("intent_bogus", "hint"), "hint")

    def test_moved_skills_registered(self):
        names = _reg().skill_names()
        self.assertIn("dispatch_brief", names)
        self.assertIn("repo_review", names)


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
        reg = _reg()
        for name in ("dispatch_brief", "repo_review"):
            body = reg.load_skill(name)
            self.assertTrue(body, name)
            for token in banned:
                self.assertNotIn(token, body, f"{name}: {token!r}")
            self.assertTrue(
                any(tool in body for tool in allowed), name)
            self.assertIn("virtual", body, f"{name}: must warn")


if __name__ == "__main__":
    unittest.main()
