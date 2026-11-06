import contextlib
import io
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
                with mock.patch.object(model_router.ledger, "record"):
                    spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "nebius")
        self.assertEqual(spec["model"], "zai-org/GLM-5.3-Flash")

    def test_fallback_announces_outage_signal(self):
        # Ollama-down default resolve is always visible: one log
        # line for journals, one stderr line for run output. Model
        # names and URLs only — never the spec (no-secrets rule).
        with env(NEBIUS_API_KEY="k"):
            with mock.patch.object(model_router, "ollama_reachable",
                                   return_value=False):
                with self.assertLogs("model_router",
                                     level="WARNING") as logs:
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        with mock.patch.object(
                                model_router.ledger, "record"):
                            spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "nebius")
        self.assertEqual(len(logs.output), 1)
        self.assertIn("ollama unreachable", logs.output[0])
        self.assertIn("metered nebius", logs.output[0])
        self.assertIn("ollama unreachable", err.getvalue())
        self.assertIn("metered nebius", err.getvalue())

    def test_explicit_nebius_stays_silent(self):
        # prefer="nebius" is a choice, not a downgrade: no signal.
        with env(NEBIUS_API_KEY="k"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                with self.assertNoLogs("model_router", level="WARNING"):
                    spec = model_router.resolve_llm(prefer="nebius")
        self.assertEqual(spec["provider"], "nebius")
        self.assertEqual(err.getvalue(), "")

    def test_local_path_stays_silent(self):
        # Ollama serving means no fallback: no signal.
        with env():
            with mock.patch.object(model_router, "ollama_reachable",
                                   return_value=True):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    with self.assertNoLogs("model_router",
                                           level="WARNING"):
                        spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "ollama")
        self.assertEqual(err.getvalue(), "")

    def test_fallback_writes_ledger_entry(self):
        # #885: failover leaves an audit marker (reason + model).
        with env(NEBIUS_API_KEY="k"):
            with mock.patch.object(model_router, "ollama_reachable",
                                   return_value=False):
                with mock.patch.object(model_router.ledger,
                                       "record") as rec:
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        model_router.resolve_llm()
        rec.assert_called_once()
        kwargs = rec.call_args.kwargs
        self.assertEqual(kwargs["script"], "model_router")
        self.assertIn("zai-org/GLM-5.3-Flash", kwargs["model"])
        self.assertEqual(kwargs["extra"]["event"], "metered-failover")
        self.assertEqual(kwargs["extra"]["reason"],
                         "ollama unreachable")
        self.assertEqual(kwargs["usage"]["total_tokens"], 0)

    def test_fallback_ledger_row_shape(self):
        # End to end through the real record(): the marker row
        # carries timestamp, reason, and model, and costs $0.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            with mock.patch.object(model_router.ledger,
                                   "LEDGER", path):
                with mock.patch.object(model_router.runs,
                                       "LEDGER", path):
                    with env(NEBIUS_API_KEY="k"):
                        with mock.patch.object(
                                model_router, "ollama_reachable",
                                return_value=False):
                            err = io.StringIO()
                            with contextlib.redirect_stderr(err):
                                model_router.resolve_llm()
            with open(path, encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines()
                         if ln.strip()]
        self.assertEqual(len(lines), 1)
        row = json.loads(lines[0])
        self.assertIn("ts", row)
        self.assertEqual(row["script"], "model_router")
        self.assertIn("zai-org/GLM-5.3-Flash", row["model"])
        self.assertEqual(row["extra"]["reason"],
                         "ollama unreachable")
        self.assertEqual(row["cost_usd"], 0.0)

    def test_explicit_nebius_writes_no_ledger_entry(self):
        with env(NEBIUS_API_KEY="k"):
            with mock.patch.object(model_router.ledger,
                                   "record") as rec:
                model_router.resolve_llm(prefer="nebius")
        rec.assert_not_called()

    def test_local_path_writes_no_ledger_entry(self):
        with env():
            with mock.patch.object(model_router, "ollama_reachable",
                                   return_value=True):
                with mock.patch.object(model_router.ledger,
                                       "record") as rec:
                    model_router.resolve_llm()
        rec.assert_not_called()

    def test_effective_cap_logged_on_metered_init(self):
        # #885: the effective cap is visible in logs whenever the
        # metered path initializes (explicit set wins here).
        with env(NEBIUS_API_KEY="k", NEBIUS_SPEND_CAP_USD="7.50"):
            with self.assertLogs("model_router",
                                 level="INFO") as logs:
                model_router.resolve_llm(prefer="nebius")
        self.assertTrue(any("7.50" in line for line in logs.output))

    def test_default_cap_logged_when_unset(self):
        with env(NEBIUS_API_KEY="k"):
            with self.assertLogs("model_router",
                                 level="INFO") as logs:
                model_router.resolve_llm(prefer="nebius")
        self.assertTrue(any("5.00" in line for line in logs.output))

    def test_nebius_model_override(self):
        with env(NEBIUS_API_KEY="k", NEBIUS_MODEL="custom/model"):
            with mock.patch.object(model_router, "ollama_reachable", return_value=False):
                with mock.patch.object(model_router.ledger, "record"):
                    spec = model_router.resolve_llm()
        self.assertEqual(spec["model"], "custom/model")

    def test_generate_suffix_stripped_from_probe(self):
        # OLLAMA_URL ships with the /api/generate path in compose;
        # the tags probe must hit the bare host (#968 — the suffix
        # made a healthy Ollama look down).
        with env(OLLAMA_URL="http://ollama:11434/api/generate"):
            with mock.patch("urllib.request.urlopen") as u:
                u.return_value.__enter__.return_value.status = 200
                self.assertTrue(model_router.ollama_reachable())
        self.assertEqual(u.call_args[0][0],
                         "http://ollama:11434/api/tags")

    def test_spec_base_url_strips_generate_suffix(self):
        with env(OLLAMA_URL="http://ollama:11434/api/generate"):
            with mock.patch.object(model_router, "ollama_reachable",
                                   return_value=True):
                spec = model_router.resolve_llm()
        self.assertEqual(spec["base_url"], "http://ollama:11434")

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


def _unknown_ledger(directory):
    path = os.path.join(directory, "ledger.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(
            {"kind": "deep-agent-run",
             "model": "nebius:uncarded/future-9z",
             "usage": {"input_tokens": 2000, "output_tokens": 1000,
                       "total_tokens": 3000}}) + "\n")
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
                        with mock.patch.object(
                                model_router.ledger, "record"):
                            spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "nebius")

    def test_default_cap_allows_small_spend(self):
        # #885: no more uncapped — unset now means the $5.00
        # default, which this $0.0008 ledger fits under.
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(runs, "LEDGER",
                                   _spent_ledger(d)):
                with env(NEBIUS_API_KEY="k"):
                    with mock.patch.object(
                            model_router, "ollama_reachable",
                            return_value=False):
                        with mock.patch.object(
                                model_router.ledger, "record"):
                            spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "nebius")

    def test_cap_blocks_on_unknown_rates(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(runs, "LEDGER",
                                   _unknown_ledger(d)):
                with env(NEBIUS_API_KEY="k",
                         NEBIUS_SPEND_CAP_USD="5.00"):
                    with mock.patch.object(
                            model_router, "ollama_reachable",
                            return_value=False):
                        with self.assertRaises(RuntimeError) as ctx:
                            model_router.resolve_llm()
        self.assertIn("rate", str(ctx.exception).lower())

    def test_unknown_allowed_with_env_rate_override(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(runs, "LEDGER",
                                   _unknown_ledger(d)):
                with env(NEBIUS_API_KEY="k",
                         NEBIUS_SPEND_CAP_USD="5.00",
                         METERED_RATE_IN_PER_1M="0.15",
                         METERED_RATE_OUT_PER_1M="0.50"):
                    with mock.patch.object(
                            model_router, "ollama_reachable",
                            return_value=False):
                        with mock.patch.object(
                                model_router.ledger, "record"):
                            spec = model_router.resolve_llm()
        self.assertEqual(spec["provider"], "nebius")

    def test_default_cap_blocks_on_unknown_rates(self):
        # #885: unset used to ignore unrated spend; the $5.00
        # default fails closed on it like any explicit cap.
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(runs, "LEDGER",
                                   _unknown_ledger(d)):
                with env(NEBIUS_API_KEY="k"):
                    with mock.patch.object(
                            model_router, "ollama_reachable",
                            return_value=False):
                        with self.assertRaises(RuntimeError) as ctx:
                            model_router.resolve_llm()
        self.assertIn("rate", str(ctx.exception).lower())

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
        # Issue #1040: default reasoning effort burns the output
        # budget on long turns; low keeps content for ~1 token.
        self.assertEqual(kwargs["reasoning_effort"], "low")
        ollama = model_router.chat_kwargs(
            {"provider": "ollama", "model": "m",
             "base_url": "http://x"})
        self.assertNotIn("stream_usage", ollama)


if __name__ == "__main__":
    unittest.main()
