import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import model_router
import runs


def env(**kwargs):
    return mock.patch.dict(os.environ, kwargs, clear=True)


class TestModelRouter(unittest.TestCase):
    def test_default_ollama_when_reachable(self):
        with env():
            with mock.patch.object(model_router, "ollama_reachable", return_value=True):
                spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "ollama")
        self.assertEqual(spec["model"], "qwen3:8b")
        self.assertEqual(spec["base_url"], "http://ollama:11434")

    def test_ask_model_honored(self):
        with env(ASK_MODEL="qwen3:14b"):
            with mock.patch.object(model_router, "ollama_reachable", return_value=True):
                spec = model_router.resolve_llm()
        self.assertEqual(spec["model"], "qwen3:14b")

    def test_empty_env_means_unset(self):
        with env(ASK_MODEL="  ", NEBIUS_API_KEY=""):
            with mock.patch.object(model_router, "ollama_reachable", return_value=True) as probe:
                spec = model_router.resolve_llm()
        self.assertEqual(spec["model"], "qwen3:8b")
        probe.assert_called_once_with()

    def test_fallback_to_nebius(self):
        with env(NEBIUS_API_KEY="k"):
            with mock.patch.object(model_router, "ollama_reachable", return_value=False):
                spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "nebius")
        self.assertEqual(spec["model"], "zai-org/GLM-5.3-Flash")

    def test_nebius_model_override(self):
        with env(NEBIUS_API_KEY="k", NEBIUS_MODEL="custom/model"):
            with mock.patch.object(model_router, "ollama_reachable", return_value=False):
                spec = model_router.resolve_llm()
        self.assertEqual(spec["model"], "custom/model")

    def test_no_key_no_fallback(self):
        with env():
            with mock.patch.object(model_router, "ollama_reachable", return_value=False):
                with self.assertRaises(RuntimeError):
                    model_router.resolve_llm()

    def test_strict_means_no_fallback(self):
        with env(NEBIUS_API_KEY="k", OLLAMA_STRICT="1"):
            with mock.patch.object(model_router, "ollama_reachable", return_value=False):
                with self.assertRaises(RuntimeError):
                    model_router.resolve_llm()

    def test_prefer_nebius_skips_probe(self):
        with env(NEBIUS_API_KEY="k"):
            with mock.patch.object(model_router, "ollama_reachable") as probe:
                spec = model_router.resolve_llm(prefer="nebius")
        probe.assert_not_called()
        self.assertEqual(spec["provider"], "nebius")

    def test_prefer_ollama_is_strict(self):
        with env():
            with mock.patch.object(model_router, "ollama_reachable", return_value=False):
                with self.assertRaises(RuntimeError):
                    model_router.resolve_llm(prefer="ollama")

    def test_bad_prefer_rejected(self):
        with env():
            with self.assertRaises(ValueError):
                model_router.resolve_llm(prefer="Muse")

    def test_chat_kwargs_have_no_secrets(self):
        for spec in (
            {"provider": "ollama", "model": "m", "base_url": "http://x"},
            {"provider": "nebius", "model": "m", "base_url": "http://x"},
        ):
            kwargs = model_router.chat_kwargs(spec)
            self.assertEqual(kwargs["model"], "m")
            self.assertEqual(kwargs["base_url"], "http://x")
            self.assertEqual(kwargs["temperature"], 0)
            self.assertNotIn("api_key", kwargs)


def _spent_ledger(directory):
    path = os.path.join(directory, "ledger.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(
            {"kind": "deep-agent-run",
             "model": "nebius:zai-org/GLM-5.3-Flash",
             "usage": {"input_tokens": 2000, "output_tokens": 1000,
                       "total_tokens": 3000},
             "cost_usd": 0.0008, "metered": True}) + "\n")
    return path


class TestSpendCapGate(unittest.TestCase):
    def test_cap_zero_blocks_metered(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(runs, "LEDGER",
                                   _spent_ledger(d)):
                with env(NEBIUS_API_KEY="k",
                         NEBIUS_SPEND_CAP_USD="0"):
                    with mock.patch.object(
                            model_router, "ollama_reachable",
                            return_value=False):
                        with self.assertRaises(RuntimeError) as ctx:
                            model_router.resolve_llm()
        self.assertIn("cap", str(ctx.exception).lower())
        self.assertIn("0.0008", str(ctx.exception))

    def test_cap_blocks_at_spend(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(runs, "LEDGER",
                                   _spent_ledger(d)):
                with env(NEBIUS_API_KEY="k",
                         NEBIUS_SPEND_CAP_USD="0.0001"):
                    with mock.patch.object(
                            model_router, "ollama_reachable",
                            return_value=False):
                        with self.assertRaises(RuntimeError):
                            model_router.resolve_llm()

    def test_cap_allows_under_spend(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(runs, "LEDGER",
                                   _spent_ledger(d)):
                with env(NEBIUS_API_KEY="k",
                         NEBIUS_SPEND_CAP_USD="5.00"):
                    with mock.patch.object(
                            model_router, "ollama_reachable",
                            return_value=False):
                        spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "nebius")

    def test_uncapped_ignores_spend(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(runs, "LEDGER",
                                   _spent_ledger(d)):
                with env(NEBIUS_API_KEY="k"):
                    with mock.patch.object(
                            model_router, "ollama_reachable",
                            return_value=False):
                        spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "nebius")

    def test_cap_leaves_local_path_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(runs, "LEDGER",
                                   _spent_ledger(d)):
                with env(NEBIUS_API_KEY="k",
                         NEBIUS_SPEND_CAP_USD="0"):
                    with mock.patch.object(
                            model_router, "ollama_reachable",
                            return_value=True):
                        spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "ollama")

    def test_chat_kwargs_rejects_unknown(self):
        with self.assertRaises(ValueError):
            model_router.chat_kwargs({"provider": "wat"})

    def test_nebius_kwargs_request_stream_usage(self):
        # The tokenfactory gateway omits usage_metadata from stream
        # chunks unless stream_usage is set; without it every Nebius
        # run ledgers zeros (spend tracking blind).
        kwargs = model_router.chat_kwargs(
            {"provider": "nebius", "model": "m",
             "base_url": "http://x"})
        self.assertTrue(kwargs["stream_usage"])
        ollama = model_router.chat_kwargs(
            {"provider": "ollama", "model": "m",
             "base_url": "http://x"})
        self.assertNotIn("stream_usage", ollama)


if __name__ == "__main__":
    unittest.main()
