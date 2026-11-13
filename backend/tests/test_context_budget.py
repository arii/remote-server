"""Context budget tests (D2): one derived budget, loud overflow.

Covers the budget math (shares, num_ctx env contract, token
estimate), the loud gates (require_fit, history trim carve), the
static-input report, and the deep_chat integration seams — including
that chat.py's composer cap provably fits the derived prompt share.
"""

import os
import unittest
from unittest import mock

from backend.exec import context_budget as cb


def fake_message(role, content, mid):
    """Duck-typed checkpoint message. Class name carries the role:
    deep_chat._message_role keys on HumanMessage/SystemMessage."""

    class _M:
        pass

    _M.__name__ = {"human": "HumanMessage", "system": "SystemMessage"}.get(
        role, "AIMessage"
    )
    instance = _M()
    instance.content = content
    instance.id = mid
    return instance


class FakeAgent:
    """get_state/update_state seam matching the checkpointer shape."""

    def __init__(self, messages):
        self._messages = messages
        self.updates = []

    def get_state(self, _config):
        return type("Snap", (), {"values": {"messages": self._messages}})()

    def update_state(self, _config, values):
        self.updates.append(values)


class DeriveBudgetTests(unittest.TestCase):
    def test_shares_sum_to_one(self):
        self.assertAlmostEqual(sum(cb.SHARES.values()), 1.0)

    def test_derive_budget_math(self):
        budget = cb.derive_budget(1000)
        self.assertEqual(budget["system"], 300)
        self.assertEqual(budget["tools"], 200)
        self.assertEqual(budget["history"], 300)
        self.assertEqual(budget["skills"], 50)
        self.assertEqual(budget["prompt"], 150)

    def test_all_five_shares_present(self):
        self.assertEqual(
            set(cb.derive_budget(1000)),
            {"system", "tools", "history", "skills", "prompt"},
        )

    def test_bad_share_sum_fails_loudly(self):
        with mock.patch.object(cb, "SHARES", {"a": 0.5, "b": 0.4}):
            with self.assertRaises(ValueError):
                cb.derive_budget(1000)

    def test_default_num_ctx_is_16384(self):
        self.assertEqual(cb.DEFAULT_NUM_CTX, 16384)

    def test_num_ctx_env_contract(self):
        cases = [
            ("16384", 16384),
            ("8192", 8192),
            ("", cb.DEFAULT_NUM_CTX),
            ("  ", cb.DEFAULT_NUM_CTX),
            ("garbage", cb.DEFAULT_NUM_CTX),
            ("0", cb.DEFAULT_NUM_CTX),
            ("-5", cb.DEFAULT_NUM_CTX),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                with mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: raw}):
                    self.assertEqual(cb.num_ctx(), expected)

    def test_num_ctx_unset_uses_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cb.num_ctx(), cb.DEFAULT_NUM_CTX)


class TokenEstimateTests(unittest.TestCase):
    def test_empty_is_zero(self):
        self.assertEqual(cb.token_estimate(""), 0)

    def test_ceil_to_four_chars_per_token(self):
        self.assertEqual(cb.token_estimate("x" * 8), 2)
        self.assertEqual(cb.token_estimate("x" * 9), 3)
        self.assertEqual(cb.token_estimate("x"), 1)


class RequireFitTests(unittest.TestCase):
    def test_within_budget_passes(self):
        with mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: "16384"}):
            cb.require_fit("prompt", "x" * 100)

    def test_over_budget_raises_naming_share(self):
        with mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: "1000"}):
            with self.assertRaises(cb.ContextBudgetError) as ctx:
                cb.require_fit("prompt", "x" * 800)  # 200 tok > 150 budget
            err = ctx.exception
            self.assertEqual(err.share, "prompt")
            self.assertEqual(err.measured, 200)
            self.assertEqual(err.budget, 150)
            self.assertIn("prompt", str(err))
            self.assertIn("150", str(err))

    def test_error_names_num_ctx(self):
        with mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: "1000"}):
            with self.assertRaises(cb.ContextBudgetError) as ctx:
                cb.require_fit("skills", "x" * 400)  # 100 tok > 50 budget
            self.assertIn("1000", str(ctx.exception))


class PlanGroupTrimTests(unittest.TestCase):
    def test_within_budget_drops_nothing(self):
        drop, fits = cb.plan_group_trim([10, 10, 10], 100)
        self.assertEqual((drop, fits), (0, True))

    def test_drops_oldest_until_fit(self):
        drop, fits = cb.plan_group_trim([40, 40, 40], 50)
        self.assertEqual(drop, 2)
        self.assertTrue(fits)

    def test_newest_group_alone_over_budget_fails(self):
        drop, fits = cb.plan_group_trim([10, 10, 90], 50)
        self.assertEqual(drop, 2)
        self.assertFalse(fits)

    def test_single_group_over_budget_fails(self):
        drop, fits = cb.plan_group_trim([90], 50)
        self.assertEqual(drop, 0)
        self.assertFalse(fits)


class CheckStaticTests(unittest.TestCase):
    def test_within_shares_is_clean(self):
        warnings, fatal = cb.check_static("s" * 100, "t" * 100, ctx=16384)
        self.assertEqual(warnings, [])
        self.assertIsNone(fatal)

    def test_share_overage_warns_not_fatal(self):
        # System share of 1000 ctx = 300 tok = 1200 chars; exceed it
        # while the window still fits the total.
        warnings, fatal = cb.check_static("s" * 2400, "t" * 100, ctx=1000)
        self.assertEqual(len(warnings), 1)
        self.assertIn("system prompt", warnings[0])
        self.assertIsNone(fatal)

    def test_impossible_window_is_fatal(self):
        _warnings, fatal = cb.check_static("s" * 3200, "t" * 1600, ctx=1000)
        self.assertIsNotNone(fatal)
        self.assertIn("can never fit", fatal)


class HistoryWindowTests(unittest.TestCase):
    """deep_chat.history_window: count trim + human-boundary snap."""

    def test_within_count_keeps_all(self):
        import deep_chat

        messages = [
            fake_message("human", "one", "m1"),
            fake_message("ai", "reply", "m2"),
        ]
        kept, drops = deep_chat.history_window(messages, keep=40)
        self.assertEqual(kept, messages)
        self.assertEqual(drops, [])

    def test_count_trim_drops_oldest(self):
        import deep_chat

        messages = [fake_message("human", f"turn {i}", f"m{i}") for i in range(10)]
        kept, drops = deep_chat.history_window(messages, keep=3)
        self.assertEqual([m.id for m in kept], ["m7", "m8", "m9"])
        self.assertEqual(drops, ["m0", "m1", "m2", "m3", "m4", "m5", "m6"])

    def test_window_never_orphans_a_tool_message(self):
        import deep_chat

        # The newest window starts on an AI tool-call message; the snap
        # backs up to the human turn that opened it.
        messages = [
            fake_message("system", "sys", "s0"),
            fake_message("human", "old", "m0"),
            fake_message("ai", "old reply", "m1"),
            fake_message("other", "tool result", "m2"),
            fake_message("human", "new", "m3"),
            fake_message("ai", "new reply", "m4"),
        ]
        kept, drops = deep_chat.history_window(messages, keep=2)
        self.assertEqual([m.id for m in kept], ["m3", "m4"])
        self.assertEqual(drops, ["m0", "m1", "m2"])

    def test_no_human_boundary_trims_nothing(self):
        import deep_chat

        messages = [fake_message("other", "tool result", f"m{i}") for i in range(6)]
        _kept, drops = deep_chat.history_window(messages, keep=2)
        self.assertEqual(drops, [])


class TrimThreadHistoryTests(unittest.TestCase):
    """Integration: count trim, token carve, loud overflow."""

    def test_under_budget_trims_nothing(self):
        import deep_chat

        agent = FakeAgent(
            [fake_message("human", "hi", "m1"), fake_message("ai", "yo", "m2")]
        )
        self.assertEqual(deep_chat.trim_thread_history(agent, {}), 0)
        self.assertEqual(agent.updates, [])

    def test_count_trim_updates_state(self):
        import deep_chat

        messages = [fake_message("human", f"turn {i}", f"m{i}") for i in range(10)]
        agent = FakeAgent(messages)
        with (
            mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: "1000"}),
            mock.patch.object(
                deep_chat, "_remove_message", lambda mid: {"removed": mid}
            ),
        ):
            dropped = deep_chat.trim_thread_history(agent, {}, keep=3)
        self.assertEqual(dropped, 7)
        removed = agent.updates[0]["messages"]
        self.assertEqual(len(removed), 7)

    def test_token_carve_drops_oldest_turns(self):
        import deep_chat

        # keep=40 (no count trim) but one huge old turn overflows the
        # history share: the token carve drops it oldest-first.
        messages = [
            fake_message("human", "x" * 40000, "big0"),  # ~10000 tok
            fake_message("ai", "ok", "a0"),
            fake_message("human", "small", "m1"),
            fake_message("ai", "ok", "a1"),
        ]
        agent = FakeAgent(messages)
        with (
            mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: "1000"}),
            mock.patch.object(
                deep_chat, "_remove_message", lambda mid: {"removed": mid}
            ),
        ):
            dropped = deep_chat.trim_thread_history(agent, {})
        self.assertEqual(dropped, 2)
        self.assertEqual(len(agent.updates[0]["messages"]), 2)

    def test_newest_turn_over_share_raises_loudly(self):
        import deep_chat

        # One turn alone exceeds the whole history share: no trim can
        # fix it — the failure must surface, not silently evict.
        messages = [
            fake_message("human", "x" * 400000, "huge"),  # ~100000 tok
            fake_message("ai", "ok", "a0"),
        ]
        agent = FakeAgent(messages)
        with mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: "1000"}):
            with self.assertRaises(cb.ContextBudgetError) as ctx:
                deep_chat.trim_thread_history(agent, {})
        self.assertEqual(ctx.exception.share, "history")

    def test_other_failures_stay_best_effort(self):
        import deep_chat

        class Boom:
            def get_state(self, _config):
                raise RuntimeError("checkpointer gone")

        self.assertEqual(deep_chat.trim_thread_history(Boom(), {}), 0)


class PromptGateTests(unittest.TestCase):
    """deep_chat._prompt_budget_gate: derived share replaces the cap."""

    def test_within_share_passes(self):
        import deep_chat

        with mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: "1000"}):
            self.assertIsNone(deep_chat._prompt_budget_gate("hello"))

    def test_over_share_rejects_with_reason(self):
        import deep_chat

        with mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: "1000"}):
            exit_code = deep_chat._prompt_budget_gate("x" * 800)
        self.assertEqual(exit_code, 2)

    def test_rejection_reason_names_share_and_budget(self):
        import deep_chat

        with mock.patch.dict(os.environ, {cb.NUM_CTX_ENV: "1000"}):
            with mock.patch("cli.usage_error") as usage:
                deep_chat._prompt_budget_gate("x" * 800)
        message = usage.call_args[0][0]
        self.assertIn("prompt", message)
        self.assertIn("150", message)


class ComposerCapFitsPromptShareTests(unittest.TestCase):
    def test_chat_max_prompt_fits_derived_share(self):
        # The composer input cap (chat.py CHAT_MAX_PROMPT) is a product
        # decision layered on the budget: every accepted composer input
        # must provably fit the derived prompt share.
        from backend.api.routers import chat

        with mock.patch.dict(os.environ, {}, clear=True):
            cb.require_fit("prompt", "x" * chat.CHAT_MAX_PROMPT)


if __name__ == "__main__":
    unittest.main()
