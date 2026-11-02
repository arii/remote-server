import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import model_router


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

    def test_chat_kwargs_rejects_unknown(self):
        with self.assertRaises(ValueError):
            model_router.chat_kwargs({"provider": "wat"})


if __name__ == "__main__":
    unittest.main()
