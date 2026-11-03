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

    def test_delete_run_hides_row_and_keeps_ids_stable(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [_row(thread_id="t0"),
                                    _row(thread_id="t1")])
            self.assertTrue(runs.delete_run("run-0", ledger_path=path))
            self.assertIsNone(runs.get_run("run-0", ledger_path=path))
            out = runs.list_runs(ledger_path=path)
            self.assertEqual([r["id"] for r in out], ["run-1"])
            self.assertEqual(runs.get_run("run-1",
                                          ledger_path=path)["id"], "run-1")

    def test_delete_run_unknown_or_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [_row(thread_id="t0")])
            self.assertFalse(runs.delete_run("run-9", ledger_path=path))
            self.assertFalse(runs.delete_run("nope", ledger_path=path))
            self.assertTrue(runs.delete_run("run-0", ledger_path=path))
            self.assertFalse(runs.delete_run("run-0", ledger_path=path))

    def test_clear_finished_removes_all_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [_row(thread_id="t0"),
                                    _row(thread_id="t1")])
            self.assertEqual(runs.clear_finished_runs(ledger_path=path), 2)
            self.assertEqual(runs.list_runs(ledger_path=path), [])
            self.assertEqual(runs.clear_finished_runs(ledger_path=path), 0)

    def test_summary_has_prompt_flag(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(extra={"prompt": "do the thing"}),
                _row(extra={}),
            ])
            out = runs.list_runs(ledger_path=path)
            self.assertFalse(out[0]["has_prompt"])
            self.assertTrue(out[1]["has_prompt"])

    def test_summary_carries_recorded_cost(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(model="nebius:zai-org/GLM-5.3-Flash",
                     cost_usd=0.0008, metered=True),
            ])
            out = runs.list_runs(ledger_path=path)
            self.assertTrue(out[0]["metered"])
            self.assertAlmostEqual(out[0]["cost_usd"], 0.0008)

    def test_summary_derives_cost_for_old_rows(self):
        usage = {"input_tokens": 2000, "output_tokens": 1000,
                 "total_tokens": 3000}
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(model="nebius:zai-org/GLM-5.3-Flash",
                     usage=usage),
                _row(model="ollama:qwen3:8b", usage=usage),
            ])
            out = runs.list_runs(ledger_path=path)
            self.assertFalse(out[0]["metered"])
            self.assertEqual(out[0]["cost_usd"], 0.0)
            self.assertTrue(out[1]["metered"])
            self.assertAlmostEqual(out[1]["cost_usd"], 0.0008)

    def test_total_metered_spend(self):
        usage = {"input_tokens": 2000, "output_tokens": 1000,
                 "total_tokens": 3000}
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(model="nebius:zai-org/GLM-5.3-Flash",
                     usage=usage, cost_usd=0.0008, metered=True),
                _row(model="nebius:zai-org/GLM-5.3-Flash",
                     usage=usage),
                _row(model="ollama:qwen3:8b", usage=usage),
                _row(model="nebius:Some/Future-Model",
                     usage=usage),
            ])
            total = runs.total_metered_spend(ledger_path=path)
            self.assertAlmostEqual(total["spent"], 0.0016)
            self.assertEqual(total["unknown"], 1)

    def test_spend_survives_delete_and_clear(self):
        usage = {"input_tokens": 2000, "output_tokens": 1000,
                 "total_tokens": 3000}
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(model="nebius:zai-org/GLM-5.3-Flash",
                     usage=usage, cost_usd=0.0008, metered=True),
                _row(model="nebius:zai-org/GLM-5.3-Flash",
                     usage=usage),
            ])
            before = runs.total_metered_spend(ledger_path=path)
            self.assertTrue(runs.delete_run("run-0", ledger_path=path))
            after_delete = runs.total_metered_spend(ledger_path=path)
            self.assertAlmostEqual(after_delete["spent"],
                                   before["spent"])
            runs.clear_finished_runs(ledger_path=path)
            after_clear = runs.total_metered_spend(ledger_path=path)
            self.assertAlmostEqual(after_clear["spent"],
                                   before["spent"])
            self.assertEqual(runs.list_runs(ledger_path=path), [])


if __name__ == "__main__":
    unittest.main()
