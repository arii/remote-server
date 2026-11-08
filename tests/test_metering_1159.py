"""Regression tests for issue #1159: spend gate self-poison.

A crashed coder run files a zero-token sorry row with model
'unknown' (auditor failure rows use 'unknown:model'). Those rows
carry no spend — no model was ever resolved — so they must quote
$0 non-metered instead of counting as unknown and fail-closing
every later Nebius resolve. Real unknown-model spend (tokens
present, or usage missing/partial) still fail-closes.

Stdlib-only, no langchain. Run: python3 -m unittest tests.test_metering_1159
"""
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import ledger
import model_router
import pricing
import runs

ZERO = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
SOME = {"input_tokens": 2000, "output_tokens": 1000,
        "total_tokens": 3000}


def _write_ledger(directory, rows):
    path = os.path.join(directory, "ledger.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    return path


def _row(**over):
    base = {"kind": "deep-agent-run", "ts": 1700000000.0,
            "script": "coder.py", "model": "unknown",
            "thread_id": "coder-1-2", "duration_s": 0.1,
            "tool_calls": [], "usage": dict(ZERO),
            "cost_usd": None, "metered": True,
            "final_preview": "", "extra": {"error": "boom"}}
    base.update(over)
    return base


class TestSorryZeroQuote(unittest.TestCase):
    def test_unknown_zero_tokens_is_free(self):
        for model in ("unknown", "unknown:model"):
            cost, metered = pricing.quote(model, dict(ZERO))
            self.assertEqual(cost, 0.0)
            self.assertFalse(metered)

    def test_unknown_with_tokens_still_fail_closes(self):
        for usage in (dict(SOME), {"input_tokens": 5},
                      {"total_tokens": 9}, {}, None, "junk"):
            cost, metered = pricing.quote("unknown", usage)
            self.assertIsNone(cost)
            self.assertTrue(metered)

    def test_bare_real_model_zero_tokens_still_fail_closes(self):
        # #1121 semantics: a bare real-model label with zeros may
        # have lost its usage in a crash — never free.
        cost, metered = pricing.quote("qwen3:8b", dict(ZERO))
        self.assertIsNone(cost)
        self.assertTrue(metered)

    def test_rated_zero_tokens_still_priced(self):
        cost, metered = pricing.quote(
            "nebius:zai-org/GLM-5.3-Flash", dict(ZERO))
        self.assertEqual(cost, 0.0)
        self.assertTrue(metered)

    def test_never_raises_on_junk(self):
        self.assertEqual(pricing.quote("unknown", None), (None, True))
        self.assertEqual(pricing.quote(["unknown"], dict(ZERO)),
                         (None, True))


class TestSorryRowFiling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "ledger.jsonl")
        open(self.path, "w").close()
        self.patch = mock.patch.object(ledger, "LEDGER", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def _last(self):
        with open(self.path, encoding="utf-8") as f:
            return json.loads(f.read().strip().splitlines()[-1])

    def test_coder_sorry_row_files_non_metered(self):
        # The #1159 shape: pre-resolve crash, no usage passed.
        entry = ledger.record(
            "coder.py", "unknown", "coder-9-9",
            [{"role": "user", "content": "do it"}], time.time(),
            extra={"worktree": "/tmp/w", "error": "boom"})
        self.assertFalse(entry["metered"])
        self.assertEqual(entry["cost_usd"], 0.0)
        filed = self._last()
        self.assertFalse(filed["metered"])
        self.assertEqual(filed["cost_usd"], 0.0)

    def test_auditor_unknown_model_row_files_non_metered(self):
        entry = ledger.record(
            "auditor.py", "unknown:model", "audit-9", [], time.time(),
            extra={"repo": "a/b", "mode": "ask", "error": "boom"})
        self.assertFalse(entry["metered"])
        self.assertEqual(entry["cost_usd"], 0.0)

    def test_unknown_with_spend_files_unknown(self):
        entry = ledger.record(
            "coder.py", "unknown", "coder-9-9",
            [{"role": "user", "content": "do it"}], time.time(),
            usage=dict(SOME), extra={"error": "late crash"})
        self.assertTrue(entry["metered"])
        self.assertIsNone(entry["cost_usd"])


class TestSpendGateDoD(unittest.TestCase):
    def test_failed_zero_token_run_does_not_raise_unknown(self):
        # Both live poison shapes: the old recorded (None, True)
        # rows heal at read time through the quote fallback.
        with tempfile.TemporaryDirectory() as d:
            path = _write_ledger(d, [
                _row(thread_id="coder-11-1"),
                _row(thread_id="coder-22-2", model="unknown:model",
                     script="auditor.py"),
                _row(model="nebius:zai-org/GLM-5.3-Flash",
                     thread_id="coder-33-3", usage=dict(SOME),
                     cost_usd=0.0008, metered=True),
            ])
            total = runs.total_metered_spend(ledger_path=path)
            self.assertEqual(total["unknown"], 0)
            self.assertAlmostEqual(total["spent"], 0.0008)

    def test_real_unknown_spend_still_fail_closes(self):
        with tempfile.TemporaryDirectory() as d:
            path = _write_ledger(d, [
                _row(thread_id="coder-11-1"),
                _row(thread_id="coder-44-4", usage=dict(SOME),
                     cost_usd=None, metered=True),
            ])
            total = runs.total_metered_spend(ledger_path=path)
            self.assertEqual(total["unknown"], 1)

    def test_gate_blocks_on_real_unknown_allows_sorry_only(self):
        env = {"NEBIUS_API_KEY": "test-key",
               "NEBIUS_SPEND_CAP_USD": "5"}
        with tempfile.TemporaryDirectory() as d1, \
                tempfile.TemporaryDirectory() as d2:
            sorry_only = _write_ledger(d1, [_row(thread_id="coder-1")])
            real_unknown = _write_ledger(d2, [
                _row(thread_id="coder-1"),
                _row(thread_id="coder-2", usage=dict(SOME),
                     cost_usd=None, metered=True),
            ])
            with mock.patch.dict(os.environ, env, clear=False):
                with mock.patch.object(runs, "LEDGER", sorry_only):
                    spec = model_router.resolve_llm(prefer="nebius")
                    self.assertEqual(spec["provider"], "nebius")
                with mock.patch.object(runs, "LEDGER", real_unknown):
                    with self.assertRaisesRegex(RuntimeError,
                                                "unverifiable"):
                        model_router.resolve_llm(prefer="nebius")


if __name__ == "__main__":
    unittest.main()
