"""deep-agent/pricing.py unit tests. Stdlib-only, no langchain."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import pricing


class TestQuote(unittest.TestCase):
    def test_ollama_is_free(self):
        cost, metered = pricing.quote(
            "ollama:qwen3:8b",
            {"input_tokens": 1000, "output_tokens": 500,
             "total_tokens": 1500})
        self.assertEqual(cost, 0.0)
        self.assertFalse(metered)

    def test_default_nebius_model_math(self):
        # zai-org/GLM-5.3-Flash: $0.15/M in, $0.50/M out.
        cost, metered = pricing.quote(
            "nebius:zai-org/GLM-5.3-Flash",
            {"input_tokens": 2000, "output_tokens": 1000,
             "total_tokens": 3000})
        self.assertTrue(metered)
        self.assertAlmostEqual(cost, 0.0008)

    def test_unknown_metered_model_has_no_rate(self):
        cost, metered = pricing.quote(
            "nebius:Some/Future-Model",
            {"input_tokens": 100, "output_tokens": 100,
             "total_tokens": 200})
        self.assertIsNone(cost)
        self.assertTrue(metered)

    def test_unknown_provider_is_metered(self):
        cost, metered = pricing.quote("?", {"total_tokens": 10})
        self.assertIsNone(cost)
        self.assertTrue(metered)

    def test_env_override_prices_unknown_models(self):
        env = {"METERED_RATE_IN_PER_1M": "1.00",
               "METERED_RATE_OUT_PER_1M": "2.00"}
        with mock.patch.dict(os.environ, env, clear=False):
            cost, metered = pricing.quote(
                "nebius:Some/Future-Model",
                {"input_tokens": 1000, "output_tokens": 1000,
                 "total_tokens": 2000})
        self.assertTrue(metered)
        self.assertAlmostEqual(cost, 0.003)

    def test_total_only_bills_at_output_rate(self):
        cost, _ = pricing.quote(
            "nebius:zai-org/GLM-5.3-Flash",
            {"input_tokens": 0, "output_tokens": 0,
             "total_tokens": 1000})
        self.assertAlmostEqual(cost, 0.0005)

    def test_never_raises_on_junk(self):
        self.assertEqual(pricing.quote(None, None), (None, True))
        self.assertEqual(pricing.quote(42, "junk"), (None, True))
        cost, metered = pricing.quote(
            "nebius:zai-org/GLM-5.3-Flash", {})
        self.assertEqual(cost, 0.0)
        self.assertTrue(metered)


class TestSpendCap(unittest.TestCase):
    def test_unset_means_default_cap(self):
        # #885: unset fails closed to the conservative default —
        # an Ollama outage must not route unlimited paid traffic.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(pricing.spend_cap(),
                             pricing.DEFAULT_SPEND_CAP_USD)
            self.assertEqual(pricing.spend_cap(), 5.0)

    def test_zero_blocks_everything(self):
        with mock.patch.dict(os.environ,
                             {"NEBIUS_SPEND_CAP_USD": "0"}, clear=True):
            self.assertEqual(pricing.spend_cap(), 0.0)

    def test_parses_dollars(self):
        with mock.patch.dict(os.environ,
                             {"NEBIUS_SPEND_CAP_USD": "5.50"}, clear=True):
            self.assertEqual(pricing.spend_cap(), 5.5)

    def test_blank_means_default_cap(self):
        for raw in ("", "  "):
            with mock.patch.dict(
                    os.environ, {"NEBIUS_SPEND_CAP_USD": raw},
                    clear=True):
                self.assertEqual(pricing.spend_cap(), 5.0)

    def test_junk_cap_blocks(self):
        for raw in ("free", "-3"):
            with mock.patch.dict(
                    os.environ, {"NEBIUS_SPEND_CAP_USD": raw},
                    clear=True):
                self.assertEqual(pricing.spend_cap(), 0.0)


class TestRateFor(unittest.TestCase):
    def test_card_rate_returned(self):
        self.assertEqual(pricing.rate_for("nebius:zai-org/GLM-5.3-Flash"),
                         (0.15, 0.50))

    def test_free_model_has_no_rate(self):
        self.assertIsNone(pricing.rate_for("ollama:qwen3:8b"))

    def test_unrated_model_has_no_rate(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(pricing.rate_for("nebius:Some/Future-Model"))

    def test_env_override_covers_uncarded(self):
        with mock.patch.dict(
                os.environ,
                {"METERED_RATE_IN_PER_1M": "0.11",
                 "METERED_RATE_OUT_PER_1M": "0.22"},
                clear=True):
            self.assertEqual(pricing.rate_for("nebius:Some/Future-Model"),
                             (0.11, 0.22))

    def test_junk_never_raises(self):
        for bad in (None, "", 123, {"m": 1}):
            self.assertIsNone(pricing.rate_for(bad))


if __name__ == "__main__":
    unittest.main()
