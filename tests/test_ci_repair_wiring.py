"""Wiring test for .github/workflows/ci-repair.yml (issue #1077).

The repair caller must pass only inputs the boomtick ci-repair
action accepts (mode, jules_api_key, ...). Passing unknown inputs
(run_id, gemini_api_key, ...) fails the step and repair never runs.
"""
import os
import unittest

WORKFLOW = os.path.join(
    os.path.dirname(__file__), "..", ".github", "workflows", "ci-repair.yml"
)

# Inputs accepted by arii/boomtick/.github/actions/ci-repair (action.yml).
VALID_INPUTS = {
    "mode",
    "issue_number",
    "comment_body",
    "jules_api_key",
    "github_token",
}


def with_block_keys(text):
    """Collect `key:` names under the first `with:` block of the workflow."""
    keys = set()
    with_indent = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if with_indent is None:
            if stripped.startswith("with:"):
                with_indent = indent
            continue
        if indent <= with_indent:
            break
        if ":" in stripped:
            keys.add(stripped.split(":", 1)[0].strip())
    return keys


class TestCiRepairWiring(unittest.TestCase):
    def test_only_valid_action_inputs(self):
        with open(WORKFLOW, encoding="utf-8") as f:
            text = f.read()
        keys = with_block_keys(text)
        self.assertTrue(keys, "no `with:` inputs found in ci-repair.yml")
        self.assertEqual(
            keys - VALID_INPUTS,
            set(),
            f"unknown ci-repair inputs (repair never runs): {keys - VALID_INPUTS}",
        )

    def test_no_legacy_gemini_repair_inputs(self):
        with open(WORKFLOW, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("run_id", text)
        self.assertNotIn("gemini_api_key", text)


if __name__ == "__main__":
    unittest.main()
