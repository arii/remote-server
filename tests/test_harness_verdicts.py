"""Tests for deep-agent/verdicts.py: machine-readable verdict schema.

Run: python3 -m pytest tests/test_harness_verdicts.py
"""

import unittest

from verdicts import parse_verdict, verdict_prompt_hint

PROBE = (
    '{"verdict": "NEEDS-WORK", '
    '"findings": [{"file": "app/layout.tsx", "line": 28, "note": "x"}], '
    '"summary": "y"}'
)


class TestParseVerdict(unittest.TestCase):
    def test_accepts_probe_output(self):
        ok, result = parse_verdict(PROBE)
        self.assertTrue(ok)
        self.assertEqual(result.verdict, "NEEDS-WORK")
        self.assertEqual(result.findings[0].file, "app/layout.tsx")
        self.assertEqual(result.findings[0].line, 28)

    def test_accepts_fenced_json(self):
        ok, result = parse_verdict("```json\n" + PROBE + "\n```")
        self.assertTrue(ok)
        self.assertEqual(result.verdict, "NEEDS-WORK")

    def test_accepts_bare_fence(self):
        ok, result = parse_verdict("```\n" + PROBE + "\n```")
        self.assertTrue(ok)

    def test_rejects_bad_json(self):
        ok, _ = parse_verdict("{not json")
        self.assertFalse(ok)

    def test_rejects_unknown_verdict(self):
        ok, _ = parse_verdict('{"verdict": "MAYBE", "summary": "y"}')
        self.assertFalse(ok)

    def test_rejects_string_line(self):
        ok, _ = parse_verdict(
            '{"verdict": "READY", '
            '"findings": [{"file": "a.py", "line": "28", "note": "x"}], '
            '"summary": "y"}'
        )
        self.assertFalse(ok)

    def test_rejects_empty_summary(self):
        ok, _ = parse_verdict('{"verdict": "READY", "summary": ""}')
        self.assertFalse(ok)

    def test_rejects_finding_missing_note(self):
        ok, _ = parse_verdict(
            '{"verdict": "READY", "findings": [{"file": "a.py", "line": 1}], '
            '"summary": "y"}'
        )
        self.assertFalse(ok)

    def test_hint_mentions_literals_and_integer_rule(self):
        hint = verdict_prompt_hint()
        for literal in ("READY", "NEEDS-WORK", "BLOCK", "APPROVE"):
            self.assertIn(literal, hint)
        self.assertIn("integer", hint)


if __name__ == "__main__":
    unittest.main()
