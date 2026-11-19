import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import deep_chat
import ledger
import model_router


@unittest.skipUnless(importlib.util.find_spec("langchain_core"), "deep-agent venv")
class ChatAccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name, "ledger.jsonl")
        for patch in (
            mock.patch.object(ledger, "LEDGER", str(self.path)),
            mock.patch.object(
                deep_chat.observe,
                "default_trace_path",
                return_value=str(Path(self.tmp.name, "trace.jsonl")),
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.spec = {"provider": "twt", "model": "default"}

    def answer(self, cost):
        from langchain_core.messages import AIMessage

        return AIMessage(
            content="Hello!",
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            response_metadata={
                "headers": {"x-trace-id": "chat-receipt", "x-cost-usd": str(cost)}
            },
        )

    def assert_cost(self, cost):
        row = json.loads(self.path.read_text().splitlines()[-1])
        self.assertEqual(row["cost_usd"], cost)
        self.assertTrue(row["metered"])
        self.assertEqual(row["usage"]["twt_trace_ids"], ["chat-receipt"])

    def test_greeting_preserves_known_zero_paid_and_unknown_cost(self):
        for cost in (0, 0.012, None):
            with self.subTest(cost=cost), contextlib.redirect_stdout(io.StringIO()):
                model = SimpleNamespace(
                    invoke=mock.Mock(return_value=self.answer(cost))
                )
                self.assertEqual(
                    deep_chat._cmd_trivial_turn(
                        "arii/remote-server", "greeting", "hi", 0, model, self.spec
                    ),
                    0,
                )
                self.assert_cost(cost)

    def test_greeting_error_preserves_receipt_metadata(self):
        error = RuntimeError("upstream failed")
        error.response = SimpleNamespace(
            headers={"x-trace-id": "chat-receipt", "x-cost-usd": ".03"}
        )
        model = SimpleNamespace(invoke=mock.Mock(side_effect=error))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                deep_chat._cmd_trivial_turn(
                    "arii/remote-server", "greeting", "hi", 0, model, self.spec
                ),
                1,
            )
        self.assert_cost(0.03)

    def review(self, model):
        with (
            mock.patch.object(model_router, "resolve_llm", return_value=self.spec),
            mock.patch.object(
                model_router,
                "build_structured_chat_model",
                return_value=(model, self.spec),
            ),
            mock.patch.object(deep_chat.s5tools, "read_pr", return_value="PR #1 diff"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return deep_chat._cmd_review_pr("arii/remote-server", "1")

    def test_review_preserves_known_zero_paid_and_unknown_cost(self):
        for cost in (0, 0.012, None):
            with self.subTest(cost=cost):
                model = SimpleNamespace(
                    invoke=mock.Mock(
                        return_value={
                            "raw": self.answer(cost),
                            "parsed": {"verdict": "APPROVE", "reason": "clean"},
                            "parsing_error": None,
                        }
                    )
                )
                self.assertEqual(self.review(model), 0)
                self.assert_cost(cost)

    def test_review_parsing_failure_preserves_paid_raw_response(self):
        model = SimpleNamespace(
            invoke=mock.Mock(
                return_value={
                    "raw": self.answer(0.012),
                    "parsed": None,
                    "parsing_error": ValueError("invalid verdict"),
                }
            )
        )
        self.assertEqual(self.review(model), 1)
        self.assert_cost(0.012)

    def test_structured_builder_can_preserve_raw_metadata(self):
        model = mock.Mock()
        with mock.patch.object(
            model_router, "build_chat_model", return_value=(model, self.spec)
        ):
            model_router.build_structured_chat_model(
                self.spec, schema=dict, include_raw=True
            )
        model.with_structured_output.assert_called_once_with(
            dict, method="json_schema", strict=True, include_raw=True
        )

    def test_review_error_preserves_receipt_metadata(self):
        error = RuntimeError("upstream failed")
        error.response = SimpleNamespace(
            headers={"x-trace-id": "chat-receipt", "x-cost-usd": ".03"}
        )
        self.assertEqual(
            self.review(SimpleNamespace(invoke=mock.Mock(side_effect=error))), 1
        )
        self.assert_cost(0.03)
