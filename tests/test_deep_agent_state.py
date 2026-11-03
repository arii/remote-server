"""Deep-agent state dir: run ledger + traces persist across restarts.

DEEP_AGENT_STATE_DIR points the run store at persisted storage; default
is the source dir (dev). Writer (ledger.record), reader (runs.list_runs),
and trace paths (observe.default_trace_path) must agree.
Run: python -m unittest tests.test_deep_agent_state
"""
import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import ledger
import observe
import runs


class StateDirTest(unittest.TestCase):
    def _reload(self, env):
        keys = ("DEEP_AGENT_STATE_DIR", "EVAL_LEDGER")
        old = {k: os.environ.get(k) for k in keys}
        for k in keys:
            os.environ.pop(k, None)
        os.environ.update(env)
        importlib.reload(ledger)
        importlib.reload(runs)
        importlib.reload(observe)

        def _restore():
            for k in keys:
                os.environ.pop(k, None)
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v
            importlib.reload(ledger)
            importlib.reload(runs)
            importlib.reload(observe)

        self.addCleanup(_restore)
        return ledger, runs, observe

    def test_default_is_source_dir(self):
        led, rns, _ = self._reload({})
        here = os.path.join(os.path.dirname(ledger.__file__))
        self.assertEqual(led.state_dir(), os.path.abspath(here))
        self.assertTrue(rns.LEDGER.startswith(os.path.abspath(here)))
        self.assertTrue(rns.TRACES.startswith(os.path.abspath(here)))

    def test_override_relocates_writer_reader_traces(self):
        with tempfile.TemporaryDirectory() as tmp:
            led, rns, obs = self._reload({"DEEP_AGENT_STATE_DIR": tmp})
            self.assertEqual(led.state_dir(), tmp)
            self.assertEqual(rns.LEDGER, os.path.join(tmp, "ledger.jsonl"))
            self.assertEqual(rns.TRACES, os.path.join(tmp, "traces"))
            self.assertTrue(
                obs.default_trace_path("s.py", "t1").startswith(
                    os.path.join(tmp, "traces")))

    def test_record_then_list_roundtrip_in_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            led, rns, _ = self._reload({"DEEP_AGENT_STATE_DIR": tmp})
            entry = led.record("deep_chat.py", "ollama:qwen3:8b",
                               "thread-9", [{"content": "hi there"}],
                               started=1000.0)
            self.assertEqual(entry["thread_id"], "thread-9")
            found = rns.list_runs()
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["thread"], "thread-9")
            self.assertIn("hi there", found[0]["preview"])
            # Fresh-reader view (simulated restart): same store, same run.
            _, rns2, _ = self._reload({"DEEP_AGENT_STATE_DIR": tmp})
            again = rns2.list_runs()
            self.assertEqual(len(again), 1)
            self.assertEqual(again[0]["id"], "run-0")

    def test_eval_ledger_still_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            ev = os.path.join(tmp, "eval.jsonl")
            led, rns, _ = self._reload({"DEEP_AGENT_STATE_DIR": tmp,
                                        "EVAL_LEDGER": ev})
            self.assertEqual(led.LEDGER, ev)
            self.assertEqual(rns.LEDGER, ev)


if __name__ == "__main__":
    unittest.main()
