import importlib.util
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
import runs
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

    def bounded(self, trace_id, bound=0.6):
        return {
            **self.reply(trace_id, None),
            "prompt_tokens": None,
            "completion_tokens": None,
            "http_status": 502,
            "cost_basis": "upper_bound",
            "cost_upper_bound_usd": bound,
        }

    def test_nebius_529_bound_is_reserved_and_enforces_spend_cap(self):
        import model_router

        scope = receipts.ReceiptRun("nebius-529")
        trace_id = scope.request()
        receipt = self.bounded(trace_id)
        scope.reconcile(lambda *_: [receipt])
        self.assertIsNone(scope.error)
        (row,) = self.rows()
        self.assertEqual(row["cost_basis"], "upper_bound")
        self.assertEqual(row["cost_upper_bound_usd"], 0.6)
        self.assertEqual(row["cost_usd"], 0.6)
        self.assertNotIn("reported_cost_usd", row["usage"])
        self.assertFalse(receipt["cost_complete"])
        self.assertEqual(
            runs.total_metered_spend(ledger.LEDGER), {"spent": 0.6, "unknown": 0}
        )
        with (
            mock.patch.object(model_router, "api_key"),
            mock.patch.object(runs, "LEDGER", ledger.LEDGER),
            mock.patch.object(model_router.pricing, "spend_cap", return_value=0.5),
        ):
            with self.assertRaisesRegex(RuntimeError, "spend cap hit"):
                model_router._remote_spec("twt", "default", "http://localhost/v1")
        record = json.loads(scope.path.with_suffix(".settlement.json").read_text())
        self.assertEqual(record["missing"], [])
        self.assertIsNone(record["error"])
        self.assertFalse(record["receipts"][0]["cost_complete"])

    def test_bounded_reservation_survives_outage_and_upgrades_to_actual(self):
        scope = receipts.ReceiptRun("reservation")
        trace_id = scope.request()
        scope.reconcile(lambda *_: [self.bounded(trace_id)])
        recovered = receipts.ReceiptRun("reservation")
        recovered.ids, recovered.path = scope.ids, scope.path
        recovered.reconcile(mock.Mock(side_effect=OSError("offline")))
        self.assertIsNone(recovered.error)
        self.assertEqual(self.rows()[0]["cost_usd"], 0.6)
        recovered.settled = False
        recovered.reconcile(lambda *_: [self.reply(trace_id, 0.01)])
        self.assertIsNone(recovered.error)
        (row,) = self.rows()
        self.assertEqual(row["cost_usd"], 0.01)
        self.assertNotIn("cost_basis", row)
        self.assertNotIn("cost_upper_bound_usd", row)
        recovered.reconcile(mock.Mock(side_effect=AssertionError("already exact")))
        self.assertEqual(len(self.rows()), 1)

    def test_mixed_exact_and_bounded_receipts_add_without_double_counting(self):
        scope = receipts.ReceiptRun("mixed-bounds")
        first, second = scope.request(), scope.request()
        ledger.record(
            "chat.py",
            "default",
            "mixed-bounds",
            [],
            0,
            provider="twt",
            usage={"twt_trace_ids": [first, second]},
            extra={"llm_call": True},
        )
        scope.reconcile(lambda *_: [self.reply(first, 0.1), self.bounded(second, 0.2)])
        self.assertIsNone(scope.error)
        self.assertAlmostEqual(self.rows()[0]["cost_usd"], 0.3)
        self.assertAlmostEqual(runs.total_metered_spend(ledger.LEDGER)["spent"], 0.3)
        scope.reconcile(mock.Mock(side_effect=AssertionError("idempotent")))
        self.assertEqual(len(self.rows()), 1)

    def test_invalid_or_unmarked_bounds_still_fail_closed(self):
        for bound in (None, -1, float("inf"), float("nan"), True, "0.1"):
            with self.subTest(bound=bound):
                receipt = self.bounded("id", bound)
                self.assertIsNone(receipts.receipt_cost(receipt))
                self.assertEqual(
                    runs._row_cost(
                        {
                            "cost_basis": "upper_bound",
                            "cost_usd": 0,
                            "cost_upper_bound_usd": bound,
                        }
                    ),
                    (None, True),
                )
        receipt = self.bounded("id")
        receipt.pop("cost_basis")
        self.assertIsNone(receipts.receipt_cost(receipt))
        scope = receipts.ReceiptRun("unbounded")
        trace_id = scope.request()
        scope.reconcile(lambda *_: [{**receipt, "trace_id": trace_id}])
        self.assertIsNotNone(scope.error)
        self.assertEqual(runs.total_metered_spend(ledger.LEDGER)["unknown"], 1)

    def test_deleted_local_run_is_not_resurrected_by_journal_recovery(self):
        scope = receipts.ReceiptRun("deleted-local")
        trace_id = scope.request(local_only=True)
        ledger.record(
            "chat.py",
            "qwen3:8b",
            "deleted-local",
            [],
            0,
            provider="ollama",
            usage={"twt_trace_ids": [trace_id]},
            extra={"llm_call": True, "prompt": "private text"},
        )
        scope.reconcile(lambda *_: [self.reply(trace_id, 0)])
        self.assertTrue(runs.delete_run("run-0", ledger_path=ledger.LEDGER))
        self.assertEqual(runs.list_runs(ledger_path=ledger.LEDGER), [])
        with (
            mock.patch("sys.argv", ["twt_receipts.py", str(scope.path)]),
            mock.patch.object(
                receipts, "fetch", return_value=[self.reply(trace_id, 0)]
            ),
        ):
            receipts.main()
            receipts.main()
        self.assertEqual(runs.list_runs(ledger_path=ledger.LEDGER), [])
        self.assertEqual(len(self.rows()), 1)
        (row,) = self.rows()
        self.assertTrue(row["deleted"])
        self.assertNotIn("prompt", row["extra"])
        self.assertEqual(row["cost_usd"], 0)

    def test_deleted_unsettled_paid_run_can_settle_without_becoming_visible(self):
        scope = receipts.ReceiptRun("deleted-paid")
        trace_id = scope.request()
        scope.reconcile(lambda *_: [], timeout=0)
        self.assertTrue(runs.delete_run("run-0", ledger_path=ledger.LEDGER))
        with (
            mock.patch("sys.argv", ["twt_receipts.py", str(scope.path)]),
            mock.patch.object(receipts, "fetch", return_value=[self.reply(trace_id)]),
        ):
            receipts.main()
        self.assertEqual(runs.list_runs(ledger_path=ledger.LEDGER), [])
        (row,) = self.rows()
        self.assertTrue(row["deleted"])
        self.assertEqual(row["cost_usd"], 0.012)

    def test_partial_settlement_survives_scope_exit_and_gateway_outage(self):
        with mock.patch.object(receipts, "fetch") as fetch:
            with receipts.run("partial") as scope:
                first, second = scope.request(), scope.request()
                fetch.side_effect = [[self.reply(first)], OSError("gateway down")]
                scope.reconcile(timeout=0)
                known = next(
                    row
                    for row in self.rows()
                    if row["usage"]["twt_trace_ids"] == [first]
                )
            rows = {row["usage"]["twt_trace_ids"][0]: row for row in self.rows()}
            self.assertEqual(rows[first], known)
            self.assertIsNone(rows[second]["cost_usd"])
            self.assertEqual(fetch.call_args.args[1], [second])
            self.assertIsNotNone(scope.error)
        recovered = receipts.ReceiptRun("partial")
        recovered.ids, recovered.path = scope.ids, scope.path
        fetch = mock.Mock(return_value=[self.reply(second)])
        recovered.reconcile(fetch, timeout=0)
        self.assertIsNone(recovered.error)
        self.assertEqual(fetch.call_args.args[1], [second])
        self.assertEqual(sum(row["cost_usd"] for row in self.rows()), 0.024)
        recovered.reconcile(fetch)
        self.assertEqual(fetch.call_count, 1)

    def test_missing_receipt_does_not_erase_known_response_cost(self):
        scope = receipts.ReceiptRun("known-response")
        trace_id = scope.request()
        ledger.record(
            "chat.py",
            "default",
            "known-response",
            [],
            0,
            provider="twt",
            usage={"twt_trace_ids": [trace_id], "reported_cost_usd": 0.05},
            extra={"llm_call": True},
        )
        scope.reconcile(lambda *_: [], timeout=0)
        (row,) = self.rows()
        self.assertEqual(row["cost_usd"], 0.05)
        self.assertEqual(row["usage"]["reported_cost_usd"], 0.05)
        self.assertIsNotNone(scope.error)

    def test_malformed_history_is_preserved_without_aborting_merge(self):
        prefix = 'broken\nnull\n[]\n\n{"metered": true, "usage": null, "extra": null}\n'
        unrelated = {"thread_id": "legacy", "metered": False}
        Path(ledger.LEDGER).write_text(prefix + json.dumps(unrelated) + "\n")
        scope = receipts.ReceiptRun("historical-corruption")
        trace_id = scope.request()
        scope.reconcile(lambda *_: [self.reply(trace_id)])
        self.assertIsNone(scope.error)
        lines = Path(ledger.LEDGER).read_text().splitlines(keepends=True)
        self.assertEqual(lines[:4], prefix.splitlines(keepends=True)[:4])
        self.assertEqual(json.loads(lines[5]), unrelated)
        self.assertEqual(json.loads(lines[6])["cost_usd"], 0.012)

    def test_truncated_history_does_not_swallow_appended_receipt(self):
        Path(ledger.LEDGER).write_text('{"unfinished"')
        scope = receipts.ReceiptRun("truncated-history")
        trace_id = scope.request()
        scope.reconcile(lambda *_: [self.reply(trace_id)])
        self.assertIsNone(scope.error)
        lines = Path(ledger.LEDGER).read_text().splitlines()
        self.assertEqual(lines[0], '{"unfinished"')
        self.assertEqual(json.loads(lines[1])["cost_usd"], 0.012)

    def test_missing_native_receipt_is_free_but_missing_hosted_is_not(self):
        scope = receipts.ReceiptRun("mixed-routes")
        token = receipts.current.set(scope)
        try:
            requests = [
                SimpleNamespace(url=SimpleNamespace(path=path), headers={})
                for path in ("/local/api/chat", "/v1/chat/completions")
            ]
            for request in requests:
                receipts.capture(request)
        finally:
            receipts.current.reset(token)
        scope.reconcile(lambda *_: [], timeout=0)
        by_id = {row["usage"]["twt_trace_ids"][0]: row for row in self.rows()}
        local_id, hosted_id = [request.headers["X-Trace-Id"] for request in requests]
        self.assertEqual(by_id[local_id]["cost_usd"], 0)
        self.assertFalse(by_id[local_id]["metered"])
        self.assertIsNone(by_id[hosted_id]["cost_usd"])
        self.assertTrue(by_id[hosted_id]["metered"])
        settled = json.loads(scope.path.with_suffix(".settlement.json").read_text())
        self.assertEqual(settled["missing"], [hosted_id])

    def test_native_route_provenance_survives_journal_recovery(self):
        scope = receipts.ReceiptRun("native-recovery")
        token = receipts.current.set(scope)
        try:
            receipts.capture(
                SimpleNamespace(
                    url=SimpleNamespace(path="/prefix/local/api/chat"), headers={}
                )
            )
        finally:
            receipts.current.reset(token)
        original = receipts.ReceiptRun.reconcile

        def reconcile(recovered):
            return original(recovered, lambda *_: [], timeout=0)

        with (
            mock.patch("sys.argv", ["twt_receipts.py", str(scope.path)]),
            mock.patch.object(receipts.ReceiptRun, "reconcile", reconcile),
        ):
            self.assertEqual(receipts.main(), 0)
        (row,) = self.rows()
        self.assertEqual(row["cost_usd"], 0)
        self.assertFalse(row["metered"])

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

    @unittest.skipUnless(
        all(
            importlib.util.find_spec(name)
            for name in ("langchain_openai", "langchain_ollama")
        ),
        "needs deep-agent venv",
    )
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
