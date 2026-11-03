"""deep-agent/ledger.py unit tests. No LLM, no langchain import:
messages are faked with SimpleNamespace/dicts. Run: python3
tests/test_ledger.py"""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import ledger


def ai(content="", usage=None, response_meta=None, calls=None):
    return SimpleNamespace(
        content=content,
        usage_metadata=usage,
        response_metadata=response_meta,
        tool_calls=calls or [],
    )


def real_tool_msg(name, content):
    # True class-name match for the isinstance-free check in record().
    cls = type("ToolMessage", (SimpleNamespace,), {})
    return cls(name=name, content=content, tool_calls=[])


class TestUsageTotal(unittest.TestCase):
    def test_sums_usage_metadata(self):
        msgs = [
            ai(usage={"input_tokens": 10, "output_tokens": 5,
                      "total_tokens": 15}),
            ai(usage={"input_tokens": 3, "output_tokens": 7,
                      "total_tokens": 10}),
        ]
        self.assertEqual(
            ledger._usage_total(msgs),
            {"input_tokens": 13, "output_tokens": 12, "total_tokens": 25},
        )

    def test_response_metadata_fallback(self):
        msgs = [ai(response_meta={"prompt_eval_count": 32,
                                  "eval_count": 71})]
        self.assertEqual(
            ledger._usage_total(msgs),
            {"input_tokens": 32, "output_tokens": 71, "total_tokens": 103},
        )

    def test_prefers_usage_metadata_no_double_count(self):
        msgs = [ai(usage={"input_tokens": 1, "output_tokens": 2,
                           "total_tokens": 3},
                   response_meta={"prompt_eval_count": 100,
                                  "eval_count": 200})]
        self.assertEqual(
            ledger._usage_total(msgs),
            {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )

    def test_tolerates_junk(self):
        self.assertEqual(
            ledger._usage_total(None),
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
        msgs = [None, ai(), {"role": "user", "content": "hi"},
                {"usage_metadata": {"input_tokens": 4, "output_tokens": 1,
                                    "total_tokens": 5}}]
        self.assertEqual(ledger._usage_total(msgs)["input_tokens"], 4)


class TestRecord(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False)
        self.tmp.close()
        self.patch = patch.object(ledger, "LEDGER", self.tmp.name)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        os.unlink(self.tmp.name)

    def read_entry(self):
        with open(self.tmp.name, encoding="utf-8") as f:
            return json.loads(f.read().strip().splitlines()[-1])

    def test_prompt_capped_at_4k(self):
        entry = ledger.record(
            "s.py", "m", "t", [], time.time(),
            extra={"prompt": "x" * 5000})
        self.assertEqual(len(entry["extra"]["prompt"]), 4000)
        self.assertEqual(len(self.read_entry()["extra"]["prompt"]), 4000)

    def test_short_prompt_kept_verbatim(self):
        entry = ledger.record(
            "s.py", "m", "t", [], time.time(),
            extra={"prompt": "do the thing"})
        self.assertEqual(entry["extra"]["prompt"], "do the thing")

    def test_records_cost_for_metered_model(self):
        entry = ledger.record(
            "s.py", "nebius:zai-org/GLM-5.3-Flash", "t", [],
            time.time(),
            usage={"input_tokens": 2000, "output_tokens": 1000,
                   "total_tokens": 3000})
        self.assertTrue(entry["metered"])
        self.assertAlmostEqual(entry["cost_usd"], 0.0008)
        filed = self.read_entry()
        self.assertTrue(filed["metered"])
        self.assertAlmostEqual(filed["cost_usd"], 0.0008)

    def test_records_free_for_local_model(self):
        entry = ledger.record(
            "s.py", "ollama:qwen3:8b", "t", [], time.time(),
            usage={"input_tokens": 2000, "output_tokens": 1000,
                   "total_tokens": 3000})
        self.assertFalse(entry["metered"])
        self.assertEqual(entry["cost_usd"], 0.0)

    def test_happy_path(self):
        msgs = [
            ai("thinking", usage={"input_tokens": 10, "output_tokens": 5,
                                  "total_tokens": 15},
               calls=[{"name": "grep_repo",
                       "args": {"pattern": "chat"}}]),
            real_tool_msg("grep_repo", "server.py: match"),
            ai("done here"),
        ]
        entry = ledger.record("s.py", "m", "t1", msgs, time.time())
        self.assertEqual(entry["tool_calls"],
                         [{"name": "grep_repo",
                           "args": "{'pattern': 'chat'}"}])
        self.assertEqual(entry["tool_results"],
                         [{"name": "grep_repo", "result": "server.py: match"}])
        self.assertEqual(entry["usage"]["total_tokens"], 15)
        self.assertEqual(entry["final_preview"], "done here")
        self.assertEqual(self.read_entry()["thread_id"], "t1")

    def test_usage_override(self):
        entry = ledger.record("s.py", "m", "t1", [ai("x")], time.time(),
                              usage={"input_tokens": 1, "output_tokens": 2,
                                     "total_tokens": 3})
        self.assertEqual(entry["usage"]["total_tokens"], 3)

    def test_bad_started_still_records(self):
        entry = ledger.record("s.py", "m", "t1", [ai("x")], "garbage")
        self.assertEqual(entry["duration_s"], -1)
        self.assertEqual(self.read_entry()["duration_s"], -1)

    def test_list_content_and_dicts(self):
        msgs = [
            {"type": "tool", "name": "t",
             "content": [{"text": "a"}, {"text": "b"}],
             "tool_calls": [{"name": "t", "args": {}}]},
            {"role": "assistant",
             "content": [{"text": "fin"}]},
        ]
        entry = ledger.record("s.py", "m", "t1", msgs, time.time())
        self.assertEqual(entry["tool_results"],
                         [{"name": "t", "result": "a b"}])
        self.assertEqual(entry["final_preview"], "fin")

    def test_never_raises(self):
        self.assertIsInstance(
            ledger.record("s.py", "m", "t1", [object()], time.time()), dict)
        self.assertIsInstance(
            ledger.record("s.py", "m", "t1", None, time.time()), dict)

    def test_calls_override_used_as_is(self):
        provided = [{"name": "grep_repo", "args": "{'pattern': 'x'}"}]
        entry = ledger.record("s.py", "m", "t1", [ai("x")], time.time(),
                              calls=provided)
        self.assertEqual(entry["tool_calls"], provided)

    def test_calls_override_wins_over_messages(self):
        msgs = [ai("y", calls=[{"name": "stale", "args": {}}])]
        provided = [{"name": "fresh", "args": "{}"}]
        entry = ledger.record("s.py", "m", "t1", msgs, time.time(),
                              calls=provided)
        self.assertEqual(entry["tool_calls"], provided)

    def test_calls_omitted_derives_from_messages(self):
        msgs = [ai("z", calls=[{"name": "derived", "args": {}}])]
        entry = ledger.record("s.py", "m", "t1", msgs, time.time())
        self.assertEqual(entry["tool_calls"],
                         [{"name": "derived", "args": "{}"}])


class TestMain(unittest.TestCase):
    def test_skips_corrupt_lines(self):
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"script": "a.py", "thread_id": "t", "duration_s": 1,\n')
            f.write("\n")
            f.write(json.dumps({"script": "b.py", "thread_id": "t2",
                                "duration_s": 2, "usage": {"total_tokens": 9},
                                "tool_calls": [{"name": "x"}],
                                "tool_results": [{"name": "x",
                                                  "result": "r"}],
                                "final_preview": "hi"}) + "\n")
            name = f.name
        try:
            with patch.object(ledger, "LEDGER", name), \
                    patch.object(sys, "argv", ["ledger.py", "5"]):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(ledger.main(), 0)
            self.assertIn("b.py", buf.getvalue())
        finally:
            os.unlink(name)


class TestLedgerPath(unittest.TestCase):
    def test_eval_ledger_override(self):
        # Parallel gates isolate ledgers: the writer must honor the
        # same EVAL_LEDGER knob the eval scorer reads. Env is read at
        # import, so this runs a child interpreter.
        import subprocess
        deep_agent = os.path.join(os.path.dirname(__file__), "..",
                                  "deep-agent")
        env = dict(os.environ, EVAL_LEDGER="/tmp/eval-t.jsonl")
        p = subprocess.run(
            [sys.executable, "-c",
             "import ledger, cli; cli.bootstrap(); print(ledger.LEDGER)"],
            capture_output=True, text=True, timeout=120, cwd=deep_agent,
            env=env,
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "/tmp/eval-t.jsonl")


if __name__ == "__main__":
    unittest.main()
