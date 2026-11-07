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

    def test_summary_prompt_first_line(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(extra={"prompt": "  fix the deploy\nsecond line"}),
                _row(extra={}),
                _row(extra={"prompt": "x" * 200}),
            ])
            out = runs.list_runs(ledger_path=path)
            self.assertEqual(out[2]["prompt_first_line"], "fix the deploy")
            self.assertEqual(out[1]["prompt_first_line"], "")
            self.assertEqual(len(out[0]["prompt_first_line"]), 120)

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

    def test_summary_provider_from_model_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(model="ollama:qwen3:8b"),
                _row(model="nebius:zai-org/GLM-5.3-Flash"),
            ])
            out = runs.list_runs(ledger_path=path)
            self.assertEqual(out[0]["provider"], "nebius")
            self.assertEqual(out[1]["provider"], "ollama")

    def test_summary_provider_none_without_known_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            bare = _row(model="qwen3:8b")
            absent = _row()
            del absent["model"]
            nulled = _row(model=None)
            path = self._ledger(d, [bare, absent, nulled])
            out = runs.list_runs(ledger_path=path)
            # Bare `qwen3:8b` keeps its colon without minting a
            # bogus provider; missing/null models fall back to ?.
            self.assertIsNone(out[2]["provider"])
            self.assertEqual(out[2]["model"], "qwen3:8b")
            self.assertIsNone(out[1]["provider"])
            self.assertEqual(out[1]["model"], "?")
            self.assertIsNone(out[0]["provider"])
            self.assertEqual(out[0]["model"], "?")

    def test_summary_latency_null_safe(self):
        with tempfile.TemporaryDirectory() as d:
            missing = _row()
            del missing["duration_s"]
            path = self._ledger(d, [
                _row(duration_s=12.5),
                _row(duration_s=-1),
                _row(duration_s=None),
                _row(duration_s="12"),
                missing,
            ])
            out = runs.list_runs(ledger_path=path)
            self.assertIsNone(out[0]["duration_s"])
            self.assertIsNone(out[1]["duration_s"])
            self.assertIsNone(out[2]["duration_s"])
            self.assertIsNone(out[3]["duration_s"])
            self.assertEqual(out[4]["duration_s"], 12.5)

    def test_summary_usage_non_dict_null_safe(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [_row(usage="junk")])
            out = runs.list_runs(ledger_path=path)
            self.assertEqual(out[0]["tokens"], 0)
            self.assertFalse(out[0]["metered"])
            self.assertEqual(out[0]["cost_usd"], 0.0)

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

    def test_backfill_rewrites_only_poison_rows(self):
        zero = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        usage = {"input_tokens": 2000, "output_tokens": 1000,
                 "total_tokens": 3000}
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                # The issue #1121 shape: bare model, test thread,
                # zero tokens, metered with no rate.
                _row(model="qwen3:8b", thread_id="test-thread-2",
                     usage=zero, cost_usd=None, metered=True),
                _row(model="qwen3:8b", thread_id="coder-test-2",
                     usage=zero, cost_usd=None, metered=True),
                # Untouched: rated, real-thread bare (fail-closed),
                # token-bearing test row, deleted tombstone.
                _row(model="nebius:zai-org/GLM-5.3-Flash",
                     usage=usage, cost_usd=0.0008, metered=True),
                _row(model="qwen3:8b", thread_id="real-1",
                     usage=zero, cost_usd=None, metered=True),
                _row(model="qwen3:8b", thread_id="test-thread-9",
                     usage=usage, cost_usd=None, metered=True),
                {"deleted": True, "ts": 1.0},
            ])
            result = runs.backfill_test_providers(ledger_path=path)
            self.assertEqual(result["matched"], [0, 1])
            self.assertEqual(result["rewritten"], 2)
            with open(path, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
            self.assertEqual(rows[0]["provider"], "ollama")
            self.assertFalse(rows[0]["metered"])
            self.assertEqual(rows[0]["cost_usd"], 0.0)
            self.assertIn("1121", rows[0]["extra"]["backfill"])
            self.assertNotIn("provider", rows[3])
            self.assertTrue(rows[3]["metered"])
            self.assertNotIn("provider", rows[4])
            total = runs.total_metered_spend(ledger_path=path)
            # Real-thread bare row + token-bearing test row stay
            # unknown (fail-closed); only the poison is cured.
            self.assertEqual(total["unknown"], 2)

    def test_backfill_dry_run_and_idempotent(self):
        zero = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(model="qwen3:8b", thread_id="test-thread-2",
                     usage=zero, cost_usd=None, metered=True),
            ])
            before = open(path, encoding="utf-8").read()
            result = runs.backfill_test_providers(
                ledger_path=path, dry_run=True)
            self.assertEqual(result["matched"], [0])
            self.assertEqual(result["rewritten"], 0)
            self.assertEqual(open(path, encoding="utf-8").read(), before)
            self.assertEqual(
                runs.backfill_test_providers(ledger_path=path)["rewritten"],
                1)
            self.assertEqual(
                runs.backfill_test_providers(ledger_path=path)["matched"],
                [])

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

    def test_evidence_less_tombstones_meter_nothing(self):
        # Bare delete markers (deleted + ts, no model/usage/cost —
        # as left in the buttons ledger) must not wedge the
        # fail-closed spend gate: there is no spend evidence to
        # price. A deleted row that kept model+usage still counts
        # unknown (#968).
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                {"deleted": True, "ts": 1793746866.0},
                {"deleted": True, "ts": 1793746868.0},
                _row(model="nebius:Some/Future-Model",
                     usage={"input_tokens": 1}),
            ])
            self.assertTrue(runs.delete_run("run-2", ledger_path=path))
            total = runs.total_metered_spend(ledger_path=path)
            self.assertEqual(total["unknown"], 1)
            self.assertAlmostEqual(total["spent"], 0.0)


class TestSearchTurns(unittest.TestCase):
    def _ledger(self, directory, rows):
        path = os.path.join(directory, "ledger.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(row if isinstance(row, str)
                        else json.dumps(row) + "\n")
        return path

    def test_newest_first_and_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(thread_id="old", ts=1.0,
                     final_preview="triage verdict: retry the gate"),
                _row(thread_id="new", ts=2.0,
                     final_preview="triage verdict: skip the gate"),
            ])
            out = runs.search_turns("triage verdict", ledger_path=path)
            self.assertIn("newest first", out.splitlines()[0])
            self.assertLess(out.index("skip the gate"),
                            out.index("retry the gate"))
            self.assertIn("run-1", out)
            self.assertIn("ask_investigator.py", out)
            self.assertIn("ollama:qwen3:8b", out)

    def test_matches_script_model_thread_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(thread_id="ask-buttons-9",
                     extra={"prompt": "why is the deploy stuck"}),
                _row(script="deep_chat.py"),
            ])
            self.assertIn(
                "ask-buttons-9",
                runs.search_turns("deploy stuck", ledger_path=path))
            self.assertIn(
                "deep_chat.py",
                runs.search_turns("deep_chat", ledger_path=path))
            self.assertIn(
                "qwen3",
                runs.search_turns("QWEN3", ledger_path=path))

    def test_words_and_across_fields(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(thread_id="triage-7",
                     final_preview="all green"),
            ])
            self.assertIn(
                "triage-7",
                runs.search_turns("triage green", ledger_path=path))
            self.assertIn(
                "no past turns match",
                runs.search_turns("triage red", ledger_path=path))

    def test_kind_filter(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(script="a.py", final_preview="boom fixed",
                     kind="deep-agent-run"),
                _row(script="b.py", final_preview="boom fixed",
                     kind="other-kind"),
            ])
            out = runs.search_turns("boom", kind="other-kind",
                                    ledger_path=path)
            self.assertIn("b.py", out)
            self.assertNotIn("a.py", out)
            out = runs.search_turns("boom", kind="DEEP-agent-RUN",
                                    ledger_path=path)
            self.assertIn("a.py", out)
            self.assertIn(
                "kind=zzz",
                runs.search_turns("boom", kind="zzz",
                                  ledger_path=path))

    def test_missing_empty_and_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            missing = os.path.join(d, "nope.jsonl")
            self.assertEqual(runs.search_turns("x", ledger_path=missing),
                             "no past turns recorded yet")
            empty = self._ledger(d, [])
            self.assertEqual(runs.search_turns("x", ledger_path=empty),
                             "no past turns recorded yet")
            path = self._ledger(d, ["{nope\n", "[1,2]\n", _row(
                final_preview="survives the noise")])
            self.assertIn("survives the noise",
                          runs.search_turns("survives",
                                            ledger_path=path))

    def test_skips_tombstoned_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(final_preview="gone from recall"),
            ])
            self.assertTrue(
                runs.delete_run("run-0", ledger_path=path))
            self.assertIn("no past turns",
                          runs.search_turns("recall", ledger_path=path))

    def test_empty_query_returns_usage(self):
        for bad in ("", "   ", None):
            out = runs.search_turns(bad, ledger_path="/nonexistent")
            self.assertIn("usage:", out)
            self.assertIn("search_turns", out)

    def test_caps_rows_and_line_length(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                _row(thread_id="t%d" % i, ts=float(i),
                     final_preview="hit %d " % i + "x" * 2000)
                for i in range(8)
            ])
            out = runs.search_turns("hit", ledger_path=path)
            lines = out.splitlines()
            self.assertEqual(len(lines), 6)  # header + 5 hits
            for line in lines:
                self.assertLessEqual(len(line), 400)
            self.assertIn("t7", out)
            self.assertNotIn("t0", out)
            out = runs.search_turns("hit", limit=2, ledger_path=path)
            self.assertEqual(len(out.splitlines()), 3)
            out = runs.search_turns("hit", limit=99, ledger_path=path)
            self.assertEqual(len(out.splitlines()), 6)
            out = runs.search_turns("hit", limit="bogus",
                                    ledger_path=path)
            self.assertEqual(len(out.splitlines()), 6)

    def test_env_ledger_resolution(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [_row(final_preview="env knob")])
            prev = os.environ.get("EVAL_LEDGER")
            os.environ["EVAL_LEDGER"] = path
            try:
                self.assertIn("env knob",
                              runs.search_turns("knob"))
            finally:
                if prev is None:
                    del os.environ["EVAL_LEDGER"]
                else:
                    os.environ["EVAL_LEDGER"] = prev

    def test_never_raises_on_odd_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = self._ledger(d, [
                json.dumps({"kind": "deep-agent-run", "ts": "bogus",
                            "final_preview": "odd ts",
                            "extra": "not-a-dict"}),
                json.dumps({"kind": "deep-agent-run", "ts": 3.0,
                            "tool_calls": "not-a-list",
                            "final_preview": ""}),
                json.dumps(None),
                _row(final_preview="odd ts normal"),
            ])
            out = runs.search_turns("odd ts", ledger_path=path)
            self.assertIn("odd ts", out)
            out = runs.search_turns("odd ts", limit={"bad": 1},
                                    ledger_path=path)
            self.assertIn("odd ts", out)

    def test_tail_only_past_size_guard(self):
        with tempfile.TemporaryDirectory() as d:
            rows = [_row(thread_id="old-%d" % i, ts=float(i),
                         final_preview="ancient history " + "y" * 2000)
                    for i in range(600)]
            rows.append(_row(thread_id="fresh", ts=9999.0,
                             final_preview="fresh needle"))
            path = self._ledger(d, rows)
            self.assertGreater(os.path.getsize(path),
                               runs.SEARCH_MAX_BYTES)
            out = runs.search_turns("fresh needle", ledger_path=path)
            self.assertIn("tail only", out.splitlines()[0])
            self.assertIn("fresh", out)


if __name__ == "__main__":
    unittest.main()
