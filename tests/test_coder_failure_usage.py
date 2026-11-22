import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import coder
import deep_chat
from backend.shared import ledger
import observe

try:
    from langchain_core.messages import AIMessage
except ImportError:
    AIMessage = None


@unittest.skipUnless(AIMessage, "requires deep-agent requirements")
class TestFailedCoderUsage(unittest.TestCase):
    def test_planner_http_error_preserves_gateway_cost(self):
        error = RuntimeError("gateway 502")
        error.response = SimpleNamespace(headers={"x-cost-usd": "0.004"})
        model = SimpleNamespace(invoke=mock.Mock(side_effect=error))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "ledger.jsonl")
            with (
                mock.patch.object(
                    coder,
                    "build_model",
                    return_value=(model, {"provider": "twt", "model": "default"}),
                ),
                mock.patch.object(ledger, "LEDGER", str(path)),
                mock.patch.object(
                    observe,
                    "default_trace_path",
                    return_value=str(Path(root, "trace.jsonl")),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "gateway 502"):
                    coder._default_plan_fn("brief", root, {})
            row = json.loads(path.read_text())
            self.assertEqual(row["cost_usd"], 0.004)
            self.assertEqual(row["extra"]["error"], "RuntimeError")

    def scenario(self, *, error=None, write=False, setup_error=False, cost=0.0123):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "ledger.jsonl")

            def build(*args, recorder, written_files, **kwargs):
                if setup_error:
                    raise ValueError("setup failed")
                if write:
                    written_files.add("result.txt")

                def invoke(*args, **kwargs):
                    usage = {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "total_tokens": 120,
                    }
                    if cost is not None:
                        usage["reported_cost_usd"] = cost
                    recorder.model_response(usage)
                    if error:
                        raise error
                    return {"messages": [AIMessage(content="DONE #1359")]}

                return SimpleNamespace(invoke=invoke)

            with (
                mock.patch.object(
                    coder,
                    "build_model",
                    return_value=(None, {"provider": "twt", "model": "default"}),
                ),
                mock.patch.object(coder, "build_agent", side_effect=build),
                mock.patch.object(coder, "verify_rounds", return_value=0),
                mock.patch.object(
                    coder, "run_bounded", side_effect=lambda fn, timeout: fn()
                ),
                mock.patch.object(
                    deep_chat, "open_checkpointer", return_value=(None, False, None)
                ),
                mock.patch.object(
                    observe,
                    "default_trace_path",
                    return_value=str(Path(root, "trace.jsonl")),
                ),
                mock.patch.object(ledger, "LEDGER", str(path)),
            ):
                try:
                    result = coder.run_coder(
                        root, "Implement #1359", "system", "failure-cost-proof", 5
                    )
                except (RuntimeError, ValueError, KeyboardInterrupt) as exc:
                    result = exc
            rows = (
                [json.loads(line) for line in path.read_text().splitlines()]
                if path.exists()
                else []
            )
            return result, rows

    def test_no_writes_flushes_paid_usage_once_with_error(self):
        result, rows = self.scenario()
        self.assertIn("no written files", str(result))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_usd"], 0.0123)
        self.assertEqual(rows[0]["usage"]["total_tokens"], 120)
        self.assertIn("no written files", rows[0]["extra"]["error"])

    def test_gateway_error_cost_is_added_to_completed_calls(self):
        error = RuntimeError("gateway 502")
        error.response = SimpleNamespace(headers={"x-cost-usd": "0.004"})
        _, rows = self.scenario(error=error)
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.0163)
        self.assertIn("gateway 502", rows[0]["extra"]["error"])

    def test_exception_timeout_and_interrupt_flush_usage(self):
        for error in (ValueError("failed"), TimeoutError(), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                result, rows = self.scenario(error=error)
                self.assertIsInstance(result, BaseException)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["cost_usd"], 0.0123)
                self.assertIn(type(error).__name__, rows[0]["extra"]["error"])

    def test_empty_setup_failure_is_not_dispatched_and_free(self):
        result, rows = self.scenario(setup_error=True)
        self.assertIsInstance(result, ValueError)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_usd"], 0)
        self.assertFalse(rows[0]["metered"])
        self.assertFalse(rows[0]["extra"]["llm_call"])

    def test_missing_reported_cost_stays_unknown_on_failure(self):
        _, rows = self.scenario(cost=None)
        self.assertIsNone(rows[0]["cost_usd"])

    def test_success_returns_usage_without_double_recording(self):
        result, rows = self.scenario(write=True)
        self.assertEqual(result[1]["reported_cost_usd"], 0.0123)
        self.assertEqual(rows, [])
