import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import coder
import ledger
import observe
import twt_receipts as receipts


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(
            ledger, "LEDGER", str(Path(self.tmp.name, "ledger.jsonl"))
        )
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(ledger, "state_dir", return_value=self.tmp.name)
        patch.start()
        self.addCleanup(patch.stop)

    def reply(self, trace_id, cost=0.012):
        return {
            "trace_id": trace_id,
            "model": "paid",
            "provider": "nebius",
            "cost_usd": cost,
            "cost_complete": cost is not None,
            "prompt_tokens": 10,
            "completion_tokens": 5,
        }

    def rows(self):
        return [
            json.loads(line) for line in Path(ledger.LEDGER).read_text().splitlines()
        ]

    def test_empty_run_does_not_contact_gateway(self):
        fetch = mock.Mock()
        receipts.ReceiptRun("empty").reconcile(fetch)
        fetch.assert_not_called()
        self.assertFalse(Path(ledger.LEDGER).exists())

    def test_late_receipt_is_polled_recorded_and_idempotent(self):
        scope = receipts.ReceiptRun("pipeline-late")
        trace_id = scope.request()
        clock = [0]
        fetch = mock.Mock(side_effect=[[], [self.reply(trace_id)]])
        scope.reconcile(
            fetch,
            clock=lambda: clock[0],
            sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        )
        self.assertIsNone(scope.error)
        (row,) = self.rows()
        self.assertEqual(row["cost_usd"], 0.012)
        self.assertEqual(row["usage"]["twt_trace_ids"], [trace_id])
        self.assertIn("shutdown", row["extra"]["error"])
        scope.reconcile(lambda *_: [self.reply(trace_id)])
        self.assertEqual(len(self.rows()), 1)
        with self.assertRaisesRegex(RuntimeError, "Pipeline stopped"):
            scope.request()

    def test_aggregate_row_refresh_does_not_double_bill(self):
        scope = receipts.ReceiptRun("aggregate")
        ids = [scope.request(), scope.request()]
        ledger.record(
            "coder.py",
            "default",
            "pipeline",
            [],
            0,
            provider="twt",
            usage={"twt_trace_ids": ids, "reported_cost_usd": 0.024},
        )
        scope.reconcile(lambda *_: [self.reply(trace_id) for trace_id in ids])
        (row,) = self.rows()
        self.assertEqual(row["cost_usd"], 0.024)
        self.assertEqual(row["usage"]["input_tokens"], 20)

    def test_missing_receipt_fails_closed_and_can_be_resumed(self):
        scope = receipts.ReceiptRun("interrupted")
        trace_id = scope.request()
        scope.reconcile(lambda *_: [], timeout=0)
        (row,) = self.rows()
        self.assertTrue(row["metered"])
        self.assertIsNone(row["cost_usd"])
        self.assertTrue(scope.error)
        scope.reconcile(lambda *_: [self.reply(trace_id)])
        self.assertIsNone(scope.error)
        self.assertEqual(self.rows()[0]["cost_usd"], 0.012)

    def test_network_and_foreign_receipts_do_not_zero_unknown_spend(self):
        for response in (OSError("unavailable"), [{"trace_id": "other"}]):
            with self.subTest(response=response):
                scope = receipts.ReceiptRun("failed")
                scope.request()
                fetch = (
                    mock.Mock(side_effect=response)
                    if isinstance(response, Exception)
                    else mock.Mock(return_value=response)
                )
                scope.reconcile(fetch)
                self.assertTrue(scope.error)
                self.assertIsNone(self.rows()[-1]["cost_usd"])

    def test_trace_ids_survive_recorder_aggregation_and_errors(self):
        recorder = observe.TraceRecorder(str(Path(self.tmp.name, "trace")))
        message = {
            "response_metadata": {"headers": {"X-Trace-Id": "id", "x-cost-usd": "0.5"}}
        }
        recorder.model_response(ledger._usage_total([message]))
        recorder.close()
        self.assertEqual(recorder.totals()[0]["twt_trace_ids"], ["id"])
        error = RuntimeError("interrupted")
        error.twt_trace_id = "pending"
        self.assertEqual(ledger.error_usage(error)["twt_trace_ids"], ["pending"])

    def test_capture_tags_requests_and_models_and_propagates_into_worker(self):
        scope = receipts.ReceiptRun("capture")
        token = receipts.current.set(scope)
        last = receipts.latest.set(None)
        try:
            request = SimpleNamespace(
                url=SimpleNamespace(path="/v1/chat/completions"), headers={}
            )
            coder.run_bounded(lambda: receipts.capture(request), 1)
            self.assertIn(request.headers["X-Trace-Id"], scope.ids)
            receipts.capture(request)
            result = SimpleNamespace(
                generations=[
                    SimpleNamespace(message=SimpleNamespace(response_metadata={}))
                ]
            )
            receipts.tag(result)
            self.assertEqual(
                result.generations[0].message.response_metadata["headers"][
                    "x-trace-id"
                ],
                request.headers["X-Trace-Id"],
            )
        finally:
            receipts.current.reset(token)
            receipts.latest.reset(last)

    def test_cancelled_run_still_settles_in_finally(self):
        original = receipts.ReceiptRun.reconcile

        def settle(scope):
            return original(
                scope, lambda *_: [self.reply(trace_id) for trace_id in scope.ids]
            )

        with mock.patch.object(receipts.ReceiptRun, "reconcile", settle):
            with self.assertRaises(KeyboardInterrupt):
                with receipts.run("cancel") as scope:
                    scope.request()
                    raise KeyboardInterrupt()
        self.assertIsNone(receipts.current.get())
        self.assertEqual(self.rows()[0]["cost_usd"], 0.012)

    def test_real_sync_and_async_model_hooks_tag_hosted_and_native_requests(self):
        import asyncio
        import httpx
        import model_router

        scope = receipts.ReceiptRun("sdk")
        token = receipts.current.set(scope)
        try:
            for asynchronous in (False, True):
                for provider in ("twt", "ollama"):

                    def handler(request):
                        self.assertIn(request.headers["x-trace-id"], scope.ids)
                        if provider == "ollama":
                            body = {
                                "model": "qwen3:8b",
                                "message": {"role": "assistant", "content": "OK"},
                                "done": True,
                                "prompt_eval_count": 10,
                                "eval_count": 2,
                            }
                        else:
                            body = {
                                "id": "reply",
                                "object": "chat.completion",
                                "created": 1,
                                "model": "default",
                                "choices": [
                                    {
                                        "index": 0,
                                        "message": {
                                            "role": "assistant",
                                            "content": "OK",
                                        },
                                        "finish_reason": "stop",
                                    }
                                ],
                                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
                            }
                        return httpx.Response(200, json=body)

                    base = "http://127.0.0.1:1234/" + (
                        "v1" if provider == "twt" else "local"
                    )
                    model, _ = model_router.build_chat_model(
                        {
                            "provider": provider,
                            "model": "default" if provider == "twt" else "qwen3:8b",
                            "base_url": base,
                        }
                    )
                    client = (
                        (model.http_async_client if asynchronous else model.http_client)
                        if provider == "twt"
                        else (
                            model._async_client._client
                            if asynchronous
                            else model._client._client
                        )
                    )
                    client._mounts.clear()
                    client._transport = httpx.MockTransport(handler)
                    message = (
                        asyncio.run(model.ainvoke("OK"))
                        if asynchronous
                        else model.invoke("OK")
                    )
                    self.assertEqual(message.content, "OK")
                    self.assertEqual(
                        len(ledger._usage_total([message])["twt_trace_ids"]), 1
                    )
                    if asynchronous:
                        asyncio.run(client.aclose())
                    else:
                        client.close()
            self.assertEqual(len(scope.ids), 4)
        finally:
            receipts.current.reset(token)
