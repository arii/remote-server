import json
import tempfile
import threading
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

    def test_failed_receipt_write_leaves_original_ledger_intact(self):
        scope = receipts.ReceiptRun("atomic")
        trace_id = scope.request()
        ledger.record(
            "coder.py",
            "default",
            "atomic",
            [],
            0,
            provider="twt",
            usage={"twt_trace_ids": [trace_id]},
        )
        before = Path(ledger.LEDGER).read_bytes()
        with mock.patch.object(receipts.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                receipts.merge_receipts(
                    {trace_id}, {trace_id: self.reply(trace_id)}, "atomic", 0
                )
        self.assertEqual(Path(ledger.LEDGER).read_bytes(), before)

    def test_concurrent_append_targets_replaced_ledger(self):
        scope = receipts.ReceiptRun("atomic")
        trace_id = scope.request()
        ledger.record(
            "coder.py",
            "default",
            "atomic",
            [],
            0,
            provider="twt",
            usage={"twt_trace_ids": [trace_id]},
        )
        waiting = threading.Event()
        writer = threading.Thread(
            target=lambda: ledger.record(
                "chat.py", "local", "append", [], 0, provider="ollama"
            )
        )
        original_replace = receipts.os.replace
        original_flock = receipts.fcntl.flock

        def flock(fd, operation):
            if (
                threading.current_thread() is writer
                and operation == receipts.fcntl.LOCK_EX
            ):
                waiting.set()
            return original_flock(fd, operation)

        def replace(source, destination):
            writer.start()
            self.assertTrue(waiting.wait(2))
            return original_replace(source, destination)

        with (
            mock.patch.object(receipts.fcntl, "flock", side_effect=flock),
            mock.patch.object(receipts.os, "replace", side_effect=replace),
        ):
            try:
                receipts.merge_receipts(
                    {trace_id}, {trace_id: self.reply(trace_id)}, "atomic", 0
                )
            finally:
                writer.join(2)
        self.assertFalse(writer.is_alive())
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["cost_usd"], 0.012)
        self.assertEqual(rows[1]["thread_id"], "append")

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

    def test_audit_call_rows_and_non_billable_summary_settle_idempotently(self):
        scope = receipts.ReceiptRun("approved-audit")
        ids = [scope.request(), scope.request()]
        for trace_id in ids:
            ledger.record(
                "auditor.py",
                "default",
                "approved-audit",
                [],
                0,
                provider="twt",
                usage={"twt_trace_ids": [trace_id], "reported_cost_usd": 0.012},
                extra={"stage": "audit", "phase": "semantic", "llm_call": True},
            )
        ledger.record(
            "auditor.py",
            "default",
            "approved-audit",
            [],
            0,
            provider="twt",
            usage={"twt_trace_ids": ids, "reported_cost_usd": 0.024},
            extra={"stage": "audit", "phase": "summary", "llm_call": False},
        )
        summary = self.rows()[-1]
        for _ in range(2):
            scope.reconcile(lambda *_: [self.reply(trace_id) for trace_id in ids])
            self.assertIsNone(scope.error)
            rows = self.rows()
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[-1], summary)
            self.assertEqual(sum(row["cost_usd"] for row in rows), 0.024)
            self.assertTrue(
                all(row["extra"]["twt_receipt_reconciled"] for row in rows[:2])
            )

    def test_provenance_alone_does_not_own_or_refresh_receipts(self):
        scope = receipts.ReceiptRun("summary-only")
        trace_id = scope.request()
        ledger.record(
            "auditor.py",
            "default",
            "summary-only",
            [],
            0,
            provider="twt",
            usage={"twt_trace_ids": [trace_id, "other-run"]},
            extra={"llm_call": False},
        )
        summary = self.rows()[0]
        scope.reconcile(lambda *_: [self.reply(trace_id)])
        self.assertIsNone(scope.error)
        self.assertEqual(self.rows()[0], summary)
        self.assertEqual(self.rows()[1]["cost_usd"], 0.012)

    def test_billable_duplicates_and_cross_run_ownership_still_fail(self):
        for trace_ids in ((["id"], ["id"]), (["id", "other-run"],)):
            with self.subTest(trace_ids=trace_ids):
                rows = [
                    {"metered": True, "usage": {"twt_trace_ids": ids}}
                    for ids in trace_ids
                ]
                Path(ledger.LEDGER).write_text(json.dumps(rows[0]) + "\n")
                if len(rows) > 1:
                    with Path(ledger.LEDGER).open("a") as handle:
                        handle.write(json.dumps(rows[1]) + "\n")
                original = Path(ledger.LEDGER).read_text()
                with self.assertRaisesRegex(ValueError, "overlap"):
                    receipts.merge_receipts({"id"}, {"id": self.reply("id")}, "run", 0)
                self.assertEqual(Path(ledger.LEDGER).read_text(), original)

    def test_unmetered_llm_call_owns_its_free_receipt(self):
        scope = receipts.ReceiptRun("free-local")
        trace_id = scope.request()
        ledger.record(
            "coder.py",
            "local",
            "free-local",
            [],
            0,
            provider="ollama",
            usage={"twt_trace_ids": [trace_id]},
            extra={"llm_call": True},
        )
        scope.reconcile(lambda *_: [self.reply(trace_id, cost=0)])
        (row,) = self.rows()
        self.assertFalse(row["metered"])
        self.assertEqual(row["cost_usd"], 0)
        self.assertTrue(row["extra"]["twt_receipt_reconciled"])

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
                    SimpleNamespace(
                        message=SimpleNamespace(response_metadata={}),
                        generation_info=None,
                    )
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
                        return httpx.Response(
                            200, json=body, headers={"x-cost-usd": "0.012"}
                        )

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
                    if provider == "twt":
                        self.assertEqual(ledger._reported_cost([message]), 0.012)
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
