import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import runs


def _row(**over):
    base = {"kind": "deep-agent-run", "ts": 1700000000.0,
            "script": "ask_investigator.py", "model": "ollama:qwen3:8b",
            "thread_id": "ask-a-b", "duration_s": 12.5,
            "tool_calls": [{"name": "grep_repo", "args": "{}"}],
            "usage": {"input_tokens": 1, "output_tokens": 2,
                      "total_tokens": 3},
            "final_preview": "answer here", "extra": {}}
    base.update(over)
    return base


class TestRuns(unittest.TestCase):
    def _ledger(self, directory, rows):
        path = os.path.join(directory, "ledger.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(row if isinstance(row, str)
                        else json.dumps(row) + "\n")
        return path

    def test_list_newest_first(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(thread_id="first", ts=1.0),
                _row(thread_id="second", ts=2.0),
            ])
            out = runs.list_runs(ledger_path=path)
            self.assertEqual([r["thread"] for r in out],
                             ["second", "first"])
            self.assertEqual(out[0]["id"], "run-1")
            self.assertTrue(out[0]["ok"])
            self.assertEqual(out[0]["tools"], ["grep_repo"])
            self.assertEqual(out[0]["tokens"], 3)

    def test_list_skips_corrupt_and_missing(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, ["{nope\n", _row(),
                                     _row(extra={"error": "x"})])
            out = runs.list_runs(ledger_path=path)
            self.assertEqual(len(out), 2)
            self.assertFalse(out[0]["ok"])
            self.assertEqual(
                runs.list_runs(ledger_path=os.path.join(d, "nope")), [])

    def test_get_run(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [_row(thread_id="t0")])
            run = runs.get_run("run-0", ledger_path=path)
            self.assertEqual(run["id"], "run-0")
            self.assertEqual(run["entry"]["thread_id"], "t0")
            self.assertEqual(run["trace"], [])
            self.assertIsNone(runs.get_run("run-9", ledger_path=path))
            self.assertIsNone(runs.get_run("nope", ledger_path=path))
            self.assertIsNone(runs.get_run("../x", ledger_path=path))

    def test_trace_escape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(extra={"trace": "/etc/passwd"})])
            run = runs.get_run("run-0", ledger_path=path)
            self.assertEqual(run["trace"], [])


if __name__ == "__main__":
    unittest.main()
