import tempfile
import unittest
from unittest import mock

import coder


class TestStallEscalation(unittest.TestCase):
    def test_stall_then_escalate_then_edit(self):
        """When the first attempt hits the initial stall cap (5 calls),
        it escalates once through TwT routing to the strong route and retries.
        The escalated attempt produces edits and completes successfully.
        """
        meter = {}
        calls = []

        def fake_attempt(*args, **kwargs):
            calls.append(kwargs)
            tier = kwargs.get("model_tier")
            if tier != "strong":
                meter["calls"] = meter.get("calls", 0) + 5
                raise coder.CoderBudgetExceeded(
                    "BLOCKED: Initial implementation stalled after 5 model calls with neither a plan nor an edit",
                    recoverable_stall=True,
                )
            meter["calls"] = meter.get("calls", 0) + 3
            # Escalated attempt succeeds
            return ("DONE #1740", {}, 3, 0.0, "twt:route:strong", "/tmp/trace", True, 1)

        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(coder, "_run_coder_attempt", side_effect=fake_attempt),
        ):
            result = coder.run_coder(
                root,
                "Fix issue #1740",
                "system",
                "thread-1",
                30,
                meter=meter,
                call_budget=20,
            )

        self.assertEqual(len(calls), 2)
        # First attempt was default/fast tier
        self.assertIsNone(calls[0].get("model_tier"))
        # Second attempt was escalated to strong tier
        self.assertEqual(calls[1].get("model_tier"), "strong")
        self.assertEqual(calls[1].get("model_override"), "route:strong")
        # Existing budget was respected across both attempts (5 + 3 = 8)
        self.assertEqual(meter["calls"], 8)
        self.assertEqual(meter["stall_escalations"], 1)
        self.assertEqual(meter["strong_escalations"], 1)
        self.assertTrue(result[6])  # verified True

    def test_stall_then_escalate_then_stall_then_block(self):
        """When the initial attempt stalls, escalates once to strong route,
        and the escalated attempt also stalls, execution blocks with the measured reason.
        It does not escalate a second time.
        """
        meter = {}
        calls = []

        def fake_attempt(*args, **kwargs):
            calls.append(kwargs)
            meter["calls"] = meter.get("calls", 0) + 5
            raise coder.CoderBudgetExceeded(
                "BLOCKED: Initial implementation stalled after 5 model calls with neither a plan nor an edit",
                recoverable_stall=True,
            )

        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(coder, "_run_coder_attempt", side_effect=fake_attempt),
            self.assertRaises(coder.CoderBudgetExceeded) as ctx,
        ):
            coder.run_coder(
                root,
                "Fix issue #1740",
                "system",
                "thread-1",
                30,
                meter=meter,
                call_budget=20,
            )

        # Stalled on attempt 1, escalated once, stalled on attempt 2, then blocked
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0].get("model_tier"))
        self.assertEqual(calls[1].get("model_tier"), "strong")
        self.assertIn(
            "stalled after 5 model calls with neither a plan nor an edit",
            str(ctx.exception),
        )
        # Total calls across both attempts: 5 + 5 = 10
        self.assertEqual(meter["calls"], 10)
        self.assertEqual(meter["stall_escalations"], 1)
        self.assertEqual(meter["strong_escalations"], 1)

    def test_no_escalation_when_first_attempt_plans_or_edits_normally(self):
        """When the first attempt plans or edits normally, no escalation occurs."""
        meter = {}
        calls = []

        def fake_attempt(*args, **kwargs):
            calls.append(kwargs)
            meter["calls"] = meter.get("calls", 0) + 4
            return ("DONE #1740", {}, 4, 0.0, "twt:default", "/tmp/trace", True, 1)

        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(coder, "_run_coder_attempt", side_effect=fake_attempt),
        ):
            result = coder.run_coder(
                root,
                "Fix issue #1740",
                "system",
                "thread-1",
                30,
                meter=meter,
                call_budget=20,
            )

        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0].get("model_tier"))
        self.assertEqual(meter.get("stall_escalations", 0), 0)
        self.assertEqual(meter.get("strong_escalations", 0), 0)
        self.assertEqual(meter["calls"], 4)
        self.assertTrue(result[6])

    def test_repeated_tool_call_stall_escalates_to_strong(self):
        """When the first attempt hits a repeated-tool-call stall, it escalates once
        to route:strong and completes successfully.
        """
        meter = {}
        calls = []

        def fake_attempt(*args, **kwargs):
            calls.append(kwargs)
            if kwargs.get("model_tier") != "strong":
                meter["calls"] = meter.get("calls", 0) + 3
                raise coder.CoderBudgetExceeded(
                    "BLOCKED: Initial implementation repeated unchanged tool call: edit_file {}",
                    recoverable_stall=True,
                )
            meter["calls"] = meter.get("calls", 0) + 2
            return ("DONE #1740", {}, 2, 0.0, "twt:route:strong", "/tmp/trace", True, 1)

        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(coder, "_run_coder_attempt", side_effect=fake_attempt),
        ):
            result = coder.run_coder(
                root,
                "Fix issue #1740",
                "system",
                "thread-1",
                30,
                meter=meter,
                call_budget=20,
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].get("model_tier"), "strong")
        self.assertEqual(calls[1].get("model_override"), "route:strong")
        self.assertEqual(meter["stall_escalations"], 1)
        self.assertTrue(result[6])


if __name__ == "__main__":
    unittest.main()
