"""Guard for #1551: no tracked skill tells an agent to add the `ready` label.

Only Ariel adds `ready`. Ledger-intake issues come from an unauthenticated
Form, so an agent promoting them would bypass the human gate.
Run: python3 tests/test_skills_no_ready_label.py
"""

import os
import re
import subprocess
import unittest

SKILLS = ".agents/skills"
ORCHESTRATOR = os.path.join(SKILLS, "lane-orchestrator", "SKILL.md")

# An instruction verb followed shortly by the word `ready` (word boundaries
# keep "already" and "ready-labelled" out), or a gh flag/edit form.
INSTRUCTION = re.compile(
    r"(?<![\w-])(?:label|add|apply|mark|set)\b[^.\n]{0,40}(?<![\w-])ready(?![\w-])"
    r"|--add-label[ =]+[\"']?ready\b"
    r"|gh issue edit\b[^\n]*(?<![\w-])ready(?![\w-])",
    re.IGNORECASE,
)
# Prohibitions are the rule we want, not violations.
PROHIBITION = re.compile(
    r"\bnever\b|\bonly ariel\b|\bdo not\b|\bdon't\b", re.IGNORECASE
)


def tracked_skill_files():
    out = subprocess.run(
        ["git", "ls-files", SKILLS],
        cwd=os.getcwd(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    return [p for p in out if p.endswith((".md", ".txt"))]


def violations(text):
    return [
        line.strip()
        for line in text.splitlines()
        if INSTRUCTION.search(line) and not PROHIBITION.search(line)
    ]


class TestNoReadyLabelInstruction(unittest.TestCase):
    def test_matcher(self):
        bad = [
            "2. Label the next queue item `ready` (priority: x).",
            "gh issue edit 12 --add-label ready",
            "Add the `ready` label to the top issue.",
        ]
        ok = [
            "Never add the `ready` label to any issue.",
            "Only Ariel adds the `ready` label.",
            "This is already done and ready-labelled issues are picked.",
            "gh issue list --label ready --state open",
        ]
        for line in bad:
            self.assertTrue(violations(line), line)
        for line in ok:
            self.assertFalse(violations(line), line)

    def test_no_skill_adds_ready_label(self):
        files = tracked_skill_files()
        self.assertIn(ORCHESTRATOR, files)
        found = {}
        for path in files:
            with open(path, encoding="utf-8") as fh:
                hits = violations(fh.read())
            if hits:
                found[path] = hits
        self.assertEqual(found, {})

    def test_orchestrator_states_only_ariel_adds_ready(self):
        with open(ORCHESTRATOR, encoding="utf-8") as fh:
            text = fh.read()
        self.assertRegex(text, r"Only Ariel adds the `ready` label")
        self.assertRegex(text, r"Never add the `ready` label")


if __name__ == "__main__":
    unittest.main()
