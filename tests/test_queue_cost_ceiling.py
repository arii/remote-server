"""Hard per-issue cap: the queue's budget guard sends each call's headroom to TwT."""

import asyncio
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

from backend.shared import ledger
from backend.shared import model_router
from backend.shared import runs
from backend.shared import twt_provenance
from backend.shared import twt_receipts
from backend.shared import twt_transport
from scripts import queue_runner as runner

BASE = "http://127.0.0.1:1234/v1"


def reply(trace):
    return httpx.Response(
        200,
        json={
            "id": "reply",
            "object": "chat.completion",
            "created": 1,
            "model": "default",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OK"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        },
        headers={"x-trace-id": trace},
    )


class FakeTwT:
    """Honors X-Max-Cost-Usd the way the gateway does: refuse if the worst case exceeds it."""

    def __init__(self, rng=None):
        self.rng = rng or random.Random(0)
        self.receipts = {}
        self.ceilings = []
        self.worst = 0.1
        self.overload = False
        self.during_call = None

    def handler(self, request):
        trace = request.headers["x-trace-id"]
        ceiling = request.headers.get("x-max-cost-usd")
        self.ceilings.append(ceiling)
        if self.during_call:
            self.during_call()
        worst = self.worst() if callable(self.worst) else self.worst
        if ceiling is not None and worst > float(ceiling):
            self.receipts[trace] = {
                "trace_id": trace,
                "cost_usd": None,
                "cost_complete": False,
                "billing_status": "none",
                "http_status": 503,
            }
            message = f"No provider available. p (m): request cost ceiling: worst case ${worst:.9f}"
            return httpx.Response(503, json={"error": {"message": message}})
        if self.overload:
            # Billing unknown: the gateway keeps the attempt's worst case.
            self.receipts[trace] = {
                "trace_id": trace,
                "cost_usd": None,
                "cost_complete": False,
                "cost_basis": "upper_bound",
                "cost_upper_bound_usd": worst,
                "http_status": 502,
            }
            return httpx.Response(502, json={"error": {"message": "overloaded"}})
        self.receipts[trace] = {
            "trace_id": trace,
            "cost_usd": self.rng.uniform(0, worst),
            "cost_complete": True,
            "http_status": 200,
        }
        return reply(trace)

    def fetch(self, base_url, ids, uds=None):
        return [self.receipts[trace] for trace in ids if trace in self.receipts]

    def charged(self):
        return sum(twt_receipts.receipt_cost(row) for row in self.receipts.values())


class CostCeilingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        history = model_router._BoundedHistory
        for target, name in (
            (ledger, "LEDGER"),
            (runs, "LEDGER"),
            (model_router, "_remote_spec"),
            (history, "_generate"),
            (history, "_agenerate"),
            (history, "_bounded_kwargs"),
            (twt_receipts, "ceiling_required"),
        ):
            value = (
                str(self.root / "ledger.jsonl")
                if name == "LEDGER"
                else getattr(target, name)
            )
            patch = mock.patch.object(target, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(ledger, "state_dir", return_value=str(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        self.twt = FakeTwT()
        patch = mock.patch.object(twt_receipts, "fetch", self.twt.fetch)
        patch.start()
        self.addCleanup(patch.stop)
        self.feature = mock.patch.object(twt_provenance, "require_gateway_feature")
        self.require = self.feature.start()
        self.addCleanup(self.feature.stop)
        self.scope = twt_receipts.ReceiptRun("ceiling", BASE)
        token = twt_receipts.current.set(self.scope)
        self.addCleanup(twt_receipts.current.reset, token)
        self.fatal, self.stops = [], []

    def install(self, cap=1.0):
        runner.install_budget_guard(cap, self.root / "issue", self.fatal, self.stops)

    def model(self):
        model, _ = model_router.build_chat_model(
            {"provider": "twt", "model": "default", "base_url": BASE}
        )
        for client in (model.http_client, model.http_async_client):
            client._mounts.clear()
            client._transport = httpx.MockTransport(self.twt.handler)
        return model

    def test_ceiling_text_rounds_down_and_stops_at_zero(self):
        self.assertEqual(runner.ceiling_text(1.0, 0.3333333337), "0.666666666")
        self.assertEqual(runner.ceiling_text(1.0, 0), "1.000000000")
        self.assertIsNone(runner.ceiling_text(1.0, 1.0))
        self.assertIsNone(runner.ceiling_text(1.0, 1.2))
        self.assertIsNone(runner.ceiling_text(1.0, 0.9999999999))

    def test_each_call_sends_cap_minus_settled_spend(self):
        self.install(0.5)
        model = self.model()
        self.twt.rng = mock.Mock(uniform=lambda low, high: 0.125)
        model.invoke("one")
        model.invoke("two")
        asyncio.run(model.ainvoke("three"))
        self.assertEqual(
            self.twt.ceilings, ["0.500000000", "0.375000000", "0.250000000"]
        )
        self.require.assert_called_once_with(
            "request_cost_ceiling", BASE, twt_transport.uds_path()
        )

    def test_upper_bound_receipt_counts_at_its_worst_case(self):
        self.install(1.0)
        model = self.model()
        self.twt.overload, self.twt.worst = True, 0.4
        with self.assertRaises(Exception):
            model.invoke("overloaded")
        self.twt.overload = False
        model.invoke("next")
        self.assertEqual(self.twt.ceilings[-1], "0.600000000")

    def test_refused_call_is_a_budget_stop_and_receipt_is_free(self):
        self.install(1.0)
        model = self.model()
        self.twt.worst = 1.5
        with self.assertRaises(runner.BudgetStop) as caught:
            model.invoke("too big")
        self.assertIn(caught.exception.twt_trace_id, self.scope.ids)
        self.assertEqual(self.stops, ["cap"])
        self.assertEqual(self.fatal, [])
        self.assertEqual(runner.check_budget(self.scope, 1.0), 0.0)

    def test_no_headroom_sends_nothing(self):
        self.install(0.2)
        model = self.model()
        self.twt.rng = mock.Mock(uniform=lambda low, high: 0.2)
        self.twt.worst = 0.2
        model.invoke("spend it all")
        with self.assertRaises(runner.BudgetStop):
            model.invoke("over")
        self.assertEqual(len(self.twt.ceilings), 1)
        self.assertEqual(self.stops, ["cap"])

    def test_gateway_without_feature_halts_before_any_request(self):
        self.require.side_effect = twt_provenance.TwTPreflightError("no ceiling")
        self.install()
        with self.assertRaises(runner.SafetyStop):
            self.model().invoke("x")
        self.assertEqual(self.twt.ceilings, [])
        self.assertEqual(self.fatal, ["no ceiling"])

    def test_feature_check_reads_gateway_models_features(self):
        self.feature.stop()
        for payload, ok in (
            ({"data": [], "features": ["request_cost_ceiling"]}, True),
            ({"data": []}, False),
            ({"data": [], "features": "request_cost_ceiling"}, False),
        ):
            with (
                self.subTest(payload=payload),
                mock.patch.object(
                    twt_provenance, "_models_payload", return_value=payload
                ),
            ):
                if ok:
                    twt_provenance.require_gateway_feature("request_cost_ceiling")
                else:
                    with self.assertRaises(twt_provenance.TwTPreflightError):
                        twt_provenance.require_gateway_feature("request_cost_ceiling")
        self.feature.start()

    def test_concurrent_call_is_refused(self):
        self.install()
        model = self.model()
        errors = []

        def nested():
            self.twt.during_call = None
            try:
                model.invoke("second")
            except runner.SafetyStop as error:
                errors.append(error)

        self.twt.during_call = nested
        model.invoke("first")
        self.assertEqual(len(errors), 1)
        self.assertEqual(len(self.twt.ceilings), 1)
        self.assertIn("concurrent", self.fatal[0])

    def test_hosted_request_without_ceiling_is_refused_under_the_guard(self):
        self.install()
        request = httpx.Request("POST", BASE + "/chat/completions")
        with self.assertRaises(RuntimeError):
            twt_receipts.capture(request)
        local = httpx.Request("POST", "http://127.0.0.1:1234/local/api/chat")
        twt_receipts.capture(local)
        self.assertNotIn("x-max-cost-usd", local.headers)

    def test_second_request_in_one_call_resettles_spend(self):
        self.install(1.0)
        model = self.model()
        self.twt.rng = mock.Mock(uniform=lambda low, high: 0.25)
        original = model.http_client._transport.handle_request

        def twice(request):
            first = original(request)
            retry = httpx.Request(
                request.method, request.url, headers={}, content=request.content
            )
            twt_receipts.capture(retry)
            original(retry)
            return first

        model.http_client._transport.handle_request = twice
        model.invoke("retried")
        self.assertEqual(self.twt.ceilings, ["1.000000000", "0.750000000"])

    def test_randomized_calls_never_push_recorded_spend_over_the_cap(self):
        rng = random.Random(1724)
        for trial in range(25):
            with self.subTest(trial=trial):
                self.twt.receipts.clear()
                self.twt.rng = rng
                self.scope = twt_receipts.ReceiptRun(f"trial-{trial}", BASE)
                twt_receipts.current.set(self.scope)
                cap = rng.choice([0.05, 0.3, 1.0])
                self.fatal, self.stops = [], []
                self.install(cap)
                model = self.model()
                self.twt.worst = lambda: rng.uniform(0, 0.4)
                stopped = False
                for _ in range(400):
                    self.twt.overload = rng.random() < 0.2
                    try:
                        model.invoke("work")
                    except runner.BudgetStop:
                        stopped = True
                        break
                    except Exception:
                        continue
                self.assertTrue(stopped)
                self.assertLessEqual(self.twt.charged(), cap)
                self.assertEqual(self.fatal, [])
                history = model_router._BoundedHistory
                history._generate = history._generate.__wrapped__
                history._agenerate = history._agenerate.__wrapped__
                model_router._remote_spec = model_router._remote_spec.__wrapped__


class ReceiptCostTests(unittest.TestCase):
    def test_trace_that_called_no_provider_costs_nothing(self):
        receipt = {"cost_usd": None, "cost_complete": False, "billing_status": "none"}
        self.assertEqual(twt_receipts.receipt_cost(receipt), 0.0)
        self.assertIsNone(
            twt_receipts.receipt_cost(
                {**receipt, "billing_status": "unknown_paid_attempt"}
            )
        )
        self.assertIsNone(
            twt_receipts.receipt_cost({**receipt, "billing_status": None})
        )


if __name__ == "__main__":
    unittest.main()
