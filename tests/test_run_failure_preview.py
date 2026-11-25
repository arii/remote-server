import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.shared import runs


class FailurePreviewTests(unittest.TestCase):
    def test_codes_and_flags(self):
        for code, expected in [
            (124, "timeout"),
            ("124", "timeout"),
            (" 124 ", "timeout"),
            (75, "claim_conflict"),
            ("75", "claim_conflict"),
            (True, "agent_error"),
            ([], "agent_error"),
        ]:
            with self.subTest(code=code):
                self.assertEqual(
                    runs._failure_class({"error": "failed", "exit_code": code}),
                    expected,
                )
        self.assertEqual(runs._failure_class({"timed_out": True}), "timeout")
        self.assertEqual(
            runs._failure_class({"claim_conflict": True, "exit_code": 124}),
            "claim_conflict",
        )
        self.assertEqual(
            runs._failure_class({"failure_class": "tests", "exit_code": 124}), "tests"
        )

    def test_words_not_substrings(self):
        for text, expected in [
            ("digital image failed", "agent_error"),
            ("pushup exercise failed", "agent_error"),
            ("claimant document unavailable", "agent_error"),
            ("latest request failed", "agent_error"),
            ("git push failed", "git_pr_error"),
            ("pull request failed", "git_pr_error"),
            ("claim conflict", "claim_conflict"),
            ("already running", "claim_conflict"),
            ("test assertion failed", "tests"),
            ("deadline exceeded", "timeout"),
        ]:
            with self.subTest(text=text):
                self.assertEqual(runs._failure_class({"error": text}), expected)

    def test_tail_bound_and_missing_paths(self):
        with tempfile.TemporaryDirectory() as d, patch.object(runs, "TRACES", ""):
            with patch.object(runs, "TRACES", d):
                p = Path(d) / "trace.jsonl"
                p.write_text("".join(f"line {i}\n" for i in range(30)))
                self.assertEqual(
                    runs._log_tail({"trace": str(p)}).splitlines(),
                    [f"line {i}" for i in range(10, 30)],
                )
                self.assertEqual(
                    runs._log_tail({"trace": str(p.parent / "absent")}), ""
                )
                self.assertEqual(runs._log_tail({"trace": "/etc/passwd"}), "")
                self.assertEqual(runs._log_tail({}), "")

    def test_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "traces"
            root.mkdir()
            outside = Path(d) / "outside"
            outside.write_text("not a trace")
            link = root / "link"
            link.symlink_to(outside)
            with patch.object(runs, "TRACES", str(root)):
                self.assertEqual(runs._log_tail({"trace": str(link)}), "")

    def test_tail_redacts_credentials_without_changing_source(self):
        lines = [
            "https://alice:EXAMPLE_PASSWORD@test.invalid/?token=EXAMPLE_TOKEN&keep=yes",
            "Authorization: Bearer EXAMPLE_BEARER",
            "password=EXAMPLE_LABELLED",
            json.dumps(
                {
                    "type": "tool_call",
                    "args": {"api_key": "EXAMPLE_JSON"},
                    "result_preview": "ordinary failure",
                    "usage": {"input_tokens": 123},
                    "args_preview": json.dumps({"password": "EXAMPLE_NESTED"}),
                }
            ),
        ]
        with tempfile.TemporaryDirectory() as d, patch.object(runs, "TRACES", d):
            p = Path(d) / "trace.jsonl"
            original = "\n".join(lines)
            p.write_text(original)
            tail = runs._log_tail({"trace": str(p)})
            self.assertNotIn("EXAMPLE_", tail)
            self.assertIn("keep=yes", tail)
            self.assertIn("ordinary failure", tail)
            self.assertIn('"input_tokens": 123', tail)
            self.assertEqual(p.read_text(), original)

    def test_summary_exposes_expected_failure_and_links(self):
        row = {
            "extra": {
                "error": "failed",
                "exit_code": "124",
                "repo": "arii/remote-server",
                "branch": "fix/example",
                "pr": 123,
            },
            "usage": {},
        }
        summary = runs._entry_summary(0, row)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["failure_class"], "timeout")
        self.assertEqual(
            summary["links"]["pr"], "https://github.com/arii/remote-server/pull/123"
        )


if __name__ == "__main__":
    unittest.main()
