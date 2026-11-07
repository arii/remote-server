"""Tests for deep-agent/skills_registry.py (stdlib only, no model).

Run: python3 -m unittest tests.test_skills_registry"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import skills_registry
from skills_registry import SkillNotFoundError, SkillsRegistry

REPO_SKILLS = os.path.join(os.path.dirname(__file__), "..", "deep-agent", "skills")


def write_skill(tmp, name, frontmatter, body):
    path = os.path.join(tmp, name + ".md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("---\n" + frontmatter + "---\n" + body)
    return path


class TestParse(unittest.TestCase):
    def test_frontmatter_name_description_triggers(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_skill(tmp, "demo", "name: demo\ndescription: Demo skill.\ntrigger_nodes:\n  - node_a\n  - node_b\n",
                        "# Demo body\n")
            reg = SkillsRegistry(skills_dir=__import__("pathlib").Path(tmp))
            self.assertEqual(reg.skill_names(), ["demo"])
            self.assertIn("Demo body", reg.load_skill("demo"))

    def test_missing_dir_yields_empty_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = SkillsRegistry(skills_dir=__import__("pathlib").Path(tmp, "absent"))
            self.assertEqual(reg.get_progressive_index_summary(), "No dynamic skills registered.")
            self.assertEqual(reg.inject_skills_for_node("any", "base"), "base")

    def test_unknown_skill_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            reg = SkillsRegistry(skills_dir=__import__("pathlib").Path(tmp))
            with self.assertRaises(SkillNotFoundError):
                reg.load_skill("nope")


class TestInjection(unittest.TestCase):
    def test_injects_only_matching_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            write_skill(tmp, "one", "name: one\ndescription: One.\ntrigger_nodes:\n  - node_a\n", "# One\n")
            write_skill(tmp, "two", "name: two\ndescription: Two.\ntrigger_nodes:\n  - node_b\n", "# Two\n")
            reg = SkillsRegistry(skills_dir=Path(tmp))
            out = reg.inject_skills_for_node("node_a", "base prompt")
            self.assertIn("# One", out)
            self.assertNotIn("# Two", out)
            self.assertTrue(out.startswith("base prompt"))
            self.assertEqual(reg.inject_skills_for_node("node_z", "base prompt"), "base prompt")

    def test_index_summary_lists_all(self):
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            write_skill(tmp, "one", "name: one\ndescription: One.\ntrigger_nodes:\n  - node_a\n", "# One\n")
            reg = SkillsRegistry(skills_dir=Path(tmp))
            summary = reg.get_progressive_index_summary()
            self.assertIn("one", summary)
            self.assertIn("node_a", summary)
            self.assertLess(len(summary), 1000)


class TestRepoSkills(unittest.TestCase):
    def test_all_core_skills_present(self):
        reg = SkillsRegistry(skills_dir=__import__("pathlib").Path(REPO_SKILLS))
        self.assertEqual(
            reg.skill_names(),
            ["architectural_audit_rubric", "co_browsing_state_takeover",
             "cobrowse_handoff_specialist", "coder_root_cause_analysis",
             "shared_browser_access", "write_spec_issue"])

    def test_repo_node_wiring(self):
        from pathlib import Path
        reg = SkillsRegistry(skills_dir=Path(REPO_SKILLS))
        draft = reg.inject_skills_for_node("draft_spec_node", "base")
        self.assertIn("Anti-Slop Guide", draft)
        self.assertNotIn("Four-Step Diagnostic Loop", draft)
        self.assertNotIn("Takeover", draft)
        replan = reg.inject_skills_for_node("validated_replanner_node", "base")
        self.assertIn("Four-Step Diagnostic Loop", replan)
        audit = reg.inject_skills_for_node("deep_semantic_auditor_node", "base")
        self.assertIn("Tautological Test Detection", audit)


if __name__ == "__main__":
    unittest.main()
