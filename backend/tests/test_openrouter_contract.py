"""Local/TwT defaults and explicit OpenRouter diagnostic contracts.

Run from the checkout root with PYTHONPATH=./deep-agent. No live keys,
provider requests, shared ledger, or optional model SDK dependencies.
OPENROUTER_MODEL overrides the model; OPENROUTER_ALLOW_PAID=1 explicitly
opts out of the default free-only policy. Live probe/scorecard receipts
remain an operator acceptance check, not fabricated unit-test evidence.
"""

import contextlib
import io
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

import ledger
import model_router
import pricing
import runs


FREE_MODEL = "contract/reasoner:free"
PAID_MODEL = "contract/reasoner"
OR_KEY = "synthetic-openrouter-credential"
NEBIUS_KEY = "synthetic-nebius-credential"
USAGE = {"input_tokens": 23, "output_tokens": 7, "total_tokens": 30}


class OpenRouterContract(unittest.TestCase):
    def setUp(self):
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(temporary) / "ledger.jsonl"
        self.enterContext(mock.patch.object(ledger, "LEDGER", str(self.path)))
        self.enterContext(mock.patch.object(runs, "LEDGER", str(self.path)))
        self.enterContext(mock.patch.object(ledger.time, "time", return_value=100.0))
        self.probe = self.enterContext(
            mock.patch.object(model_router, "ollama_reachable", return_value=False)
        )
        self.enterContext(
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))
        )
        self.enterContext(
            mock.patch("socket.socket.connect", side_effect=AssertionError("network"))
        )
        self.logs = io.StringIO()
        self.enterContext(contextlib.redirect_stderr(self.logs))
        handler = logging.StreamHandler(self.logs)
        model_router.logger.addHandler(handler)
        self.addCleanup(model_router.logger.removeHandler, handler)
        self.addCleanup(model_router.logger.setLevel, model_router.logger.level)
        model_router.logger.setLevel(logging.DEBUG)

    def resolve(self, prefer=None):
        try:
            return model_router.resolve_llm(prefer=prefer)
        except Exception as exc:
            self.fail(
                f"#1294 expected a configured route, got {type(exc).__name__}: {exc}"
            )

    def record(self, model, provider=None):
        return ledger.record(
            script="provider-probe",
            model=model,
            provider=provider,
            thread_id="provider-proof-1294",
            messages=[{"type": "ai", "content": "OK", "usage_metadata": USAGE}],
            started=99.0,
        )

    def assert_rejected(self, prefer, pattern):
        with self.assertRaises((RuntimeError, ValueError)) as caught:
            model_router.resolve_llm(prefer=prefer)
        self.assertRegex(str(caught.exception).lower(), pattern)

    def test_local_wins_even_with_both_remote_credentials(self):
        """#1294: keyed remote providers must not displace available local inference."""
        os.environ.update(OPENROUTER_API_KEY=OR_KEY, NEBIUS_API_KEY=NEBIUS_KEY)
        self.probe.return_value = True
        self.assertEqual(self.resolve()["provider"], "ollama")
        self.assertEqual(runs.total_metered_spend(), {"spent": 0.0, "unknown": 0})
        self.assertFalse(self.path.exists())

    def test_default_remote_is_gateway_not_a_direct_provider(self):
        os.environ.update(OPENROUTER_API_KEY=OR_KEY, NEBIUS_API_KEY=NEBIUS_KEY)
        spec = self.resolve()
        self.assertEqual(spec["provider"], "twt")
        self.assertEqual(spec["base_url"], model_router.DEFAULT_TWT_BASE_URL)
        self.assertEqual(spec["model"], "default")
        serialized = json.dumps(spec) + self.logs.getvalue()
        self.assertNotIn(OR_KEY, serialized)
        self.assertNotIn(NEBIUS_KEY, serialized)
        if self.path.exists():
            for line in self.path.read_text().splitlines():
                row = json.loads(line)
                self.assertNotEqual(row.get("provider"), "nebius")
                self.assertFalse(row.get("model", "").startswith("nebius:"))

    def test_openrouter_does_not_require_a_nebius_key(self):
        """#1294: the free remote is usable independently of paid credentials."""
        os.environ["OPENROUTER_API_KEY"] = OR_KEY
        self.assertEqual(self.resolve("openrouter")["provider"], "openrouter")

    def test_model_override_is_honored(self):
        """#1294: OPENROUTER_MODEL chooses a free reasoning model explicitly."""
        os.environ.update(OPENROUTER_API_KEY=OR_KEY, OPENROUTER_MODEL=FREE_MODEL)
        spec = self.resolve("openrouter")
        self.assertEqual((spec["provider"], spec["model"]), ("openrouter", FREE_MODEL))

    def test_empty_model_override_keeps_a_free_default(self):
        """#1294: a blank override must not produce an empty or paid model."""
        os.environ.update(OPENROUTER_API_KEY=OR_KEY, OPENROUTER_MODEL="  ")
        spec = self.resolve("openrouter")
        self.assertEqual(spec["provider"], "openrouter")
        self.assertTrue(
            spec["model"].endswith(":free") or spec["model"] == "openrouter/free"
        )

    def test_default_gateway_does_not_require_client_provider_keys(self):
        for key in (None, "", "  "):
            with self.subTest(key=key):
                self.logs.seek(0)
                self.logs.truncate()
                os.environ.clear()
                os.environ["NEBIUS_API_KEY"] = NEBIUS_KEY
                if key is not None:
                    os.environ["OPENROUTER_API_KEY"] = key
                self.assertEqual(self.resolve()["provider"], "twt")
                self.assertIn("using twt", self.logs.getvalue())
                self.assertNotIn(NEBIUS_KEY, self.logs.getvalue())

    def test_keyless_client_uses_configured_gateway_without_spending(self):
        self.assertEqual(model_router.resolve_llm()["provider"], "twt")
        self.assertIn("using twt", self.logs.getvalue())
        self.assertEqual(runs.total_metered_spend(), {"spent": 0.0, "unknown": 0})

    def test_explicit_openrouter_uses_the_existing_strict_preference_interface(self):
        """#1294: provider selection uses the canonical resolver, without a new ladder."""
        os.environ.update(OPENROUTER_API_KEY=OR_KEY, OPENROUTER_MODEL=FREE_MODEL)
        self.assertEqual(self.resolve("openrouter")["provider"], "openrouter")
        self.probe.assert_not_called()

    def test_explicit_openrouter_without_key_refuses_instead_of_paid_reroute(self):
        """#1294: an explicit keyless provider fails clearly even when Nebius is keyed."""
        os.environ["NEBIUS_API_KEY"] = NEBIUS_KEY
        self.assert_rejected("openrouter", r"openrouter_api_key")

    def test_strict_local_mode_blocks_remotes_even_if_keys_leak_into_environment(self):
        """#1294/keyless proof: OLLAMA_STRICT still denies every default cloud fallback."""
        os.environ.update(
            OLLAMA_STRICT="1", OPENROUTER_API_KEY=OR_KEY, NEBIUS_API_KEY=NEBIUS_KEY
        )
        self.assert_rejected(None, r"ollama_strict")
        self.assertFalse(self.path.exists())

    def test_paid_override_is_rejected_without_explicit_opt_in(self):
        """#1294: a model override alone must not silently turn free routing into paid."""
        os.environ.update(
            OPENROUTER_API_KEY=OR_KEY,
            OPENROUTER_MODEL=PAID_MODEL,
            NEBIUS_API_KEY=NEBIUS_KEY,
        )
        self.assert_rejected("openrouter", r"free|paid")
        self.assertFalse(self.path.exists())

    def test_explicit_paid_opt_in_allows_the_selected_openrouter_model(self):
        """#1294: paid OpenRouter models require deliberate configuration."""
        os.environ.update(
            OPENROUTER_API_KEY=OR_KEY,
            OPENROUTER_MODEL=PAID_MODEL,
            OPENROUTER_ALLOW_PAID="1",
            METERED_RATE_IN_PER_1M="1",
            METERED_RATE_OUT_PER_1M="2",
        )
        spec = self.resolve("openrouter")
        self.assertEqual((spec["provider"], spec["model"]), ("openrouter", PAID_MODEL))

    def test_builder_binds_openrouter_key_and_preserves_stream_usage(self):
        """#1294: the shared builder uses the selected provider's auth and usage path."""
        os.environ.update(
            OPENROUTER_API_KEY=OR_KEY,
            OPENROUTER_MODEL=FREE_MODEL,
            NEBIUS_API_KEY=NEBIUS_KEY,
        )
        sdk = types.ModuleType("langchain_openai")
        constructor = mock.Mock()

        class ChatOpenAI:
            def __init__(self, **kwargs):
                constructor(**kwargs)

        sdk.ChatOpenAI = ChatOpenAI
        with mock.patch.dict(sys.modules, {"langchain_openai": sdk}):
            model, spec = model_router.build_chat_model(self.resolve("openrouter"))
        self.assertEqual(spec["provider"], "openrouter")
        constructor.assert_called_once()
        kwargs = constructor.call_args.kwargs
        self.assertEqual(kwargs["api_key"], OR_KEY)
        self.assertEqual(kwargs["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(kwargs["model"], FREE_MODEL)
        self.assertTrue(kwargs["stream_usage"])
        self.assertIsInstance(model, sdk.ChatOpenAI)
        self.assertNotIn("api_key", model_router.chat_kwargs(spec))

    def test_zero_spend_cap_blocks_the_free_remote_too(self):
        """#1294: OpenRouter does not bypass the existing global remote-spend choke."""
        os.environ.update(OPENROUTER_API_KEY=OR_KEY, NEBIUS_SPEND_CAP_USD="0")
        self.assert_rejected("openrouter", r"cap|spend")

    def test_spent_cap_blocks_both_remotes_from_the_same_ledger(self):
        """#1294: provider selection never resets lifetime spend or bypasses its cap."""
        os.environ.update(
            OPENROUTER_API_KEY=OR_KEY,
            NEBIUS_API_KEY=NEBIUS_KEY,
            NEBIUS_SPEND_CAP_USD="0.000001",
        )
        self.record("nebius:zai-org/GLM-5.3-Flash")
        self.assertGreater(runs.total_metered_spend()["spent"], 0.000001)
        for provider in ("openrouter", "nebius"):
            with self.subTest(provider=provider):
                self.assert_rejected(provider, r"cap|spend")

    def test_unrated_openrouter_spend_blocks_both_remote_routes(self):
        """#1294: unknown OpenRouter cost stays fail-closed through the existing gate."""
        os.environ.update(OPENROUTER_API_KEY=OR_KEY, NEBIUS_API_KEY=NEBIUS_KEY)
        self.record("openrouter:contract/unrated-paid")
        self.assertEqual(runs.total_metered_spend()["unknown"], 1)
        for provider in ("openrouter", "nebius"):
            with self.subTest(provider=provider):
                self.assert_rejected(provider, r"unverifiable|unrated|no rate|unknown")

    def test_free_openrouter_usage_has_a_zero_quote_without_rate_overrides(self):
        """#1294: free provider usage cannot poison lifetime spend as unrated traffic."""
        for model in (FREE_MODEL, "openrouter/free", "zai-org/GLM-5.3-Flash:free"):
            with self.subTest(model=model):
                cost, _ = pricing.quote(f"openrouter:{model}", USAGE)
                self.assertEqual(cost, 0.0)

    def test_paid_openrouter_usage_uses_the_existing_rate_override(self):
        """#1294: optional paid usage remains metered at the configured token rates."""
        os.environ.update(METERED_RATE_IN_PER_1M="1", METERED_RATE_OUT_PER_1M="2")
        cost, metered = pricing.quote(f"openrouter:{PAID_MODEL}", USAGE)
        self.assertTrue(metered)
        self.assertAlmostEqual(cost, 0.000037)

    def test_probe_receipts_retain_provider_tokens_and_free_cost_after_reload(self):
        """#1294: synthetic probe receipts retain usage for each provider, not fake zeros."""
        for label in (
            "ollama:qwen3:8b",
            f"openrouter:{FREE_MODEL}",
            "nebius:zai-org/GLM-5.3-Flash",
        ):
            self.record(label)
        rows = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertEqual(
            [row["provider"] for row in rows], ["ollama", "openrouter", "nebius"]
        )
        self.assertTrue(all(row["usage"] == USAGE for row in rows))
        self.assertEqual(rows[0]["cost_usd"], 0.0)
        self.assertEqual(rows[1]["cost_usd"], 0.0)
        self.assertGreater(rows[2]["cost_usd"], 0.0)
        self.assertEqual(runs.total_metered_spend()["unknown"], 0)

    def test_explicit_provider_receipt_is_not_discarded(self):
        """#1294: explicit OpenRouter identity survives durable ledger serialization."""
        row = self.record(f"openrouter:{FREE_MODEL}", provider="openrouter")
        self.assertEqual(row["provider"], "openrouter")
        self.assertEqual(row["usage"], USAGE)
        self.assertEqual(row["cost_usd"], 0.0)

    def test_rate_limit_uses_one_existing_local_hop_and_never_paid_retry(self):
        """#1294: OpenRouter 429 keeps the existing single-hop local fallback semantics."""
        os.environ.update(OPENROUTER_API_KEY=OR_KEY, NEBIUS_API_KEY=NEBIUS_KEY)
        self.assertEqual(self.resolve("openrouter")["provider"], "openrouter")
        error = RuntimeError("free quota exhausted")
        error.status_code = 429
        primary = mock.Mock(side_effect=error)
        local = mock.Mock(return_value="local answer")
        make_local = mock.Mock(return_value=local)
        note = mock.Mock()
        with mock.patch.object(
            model_router, "resolve_llm", side_effect=AssertionError("reroute")
        ):
            result = model_router.with_local_fallback(primary, make_local, note)
        self.assertEqual(result, "local answer")
        primary.assert_called_once_with()
        make_local.assert_called_once_with()
        local.assert_called_once_with()
        note.assert_called_once_with(error)


class OpenRouterKeylessProofContract(unittest.TestCase):
    @unittest.skipUnless(shutil.which("bash"), "keyless proof requires Bash")
    def test_real_proof_prelude_scrubs_openrouter_before_any_python_surface(self):
        """#1294/keyless CI: proof children inherit no OpenRouter key, even if initially set."""
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            observed = directory / "observed"
            python = directory / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "printf '%s|%s|%s' \"${OPENROUTER_API_KEY+present}\" "
                '"${NEBIUS_API_KEY+present}" "${OLLAMA_STRICT-unset}" '
                '>"$PROOF_OBSERVATION"\n'
                "exit 79\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            result = subprocess.run(
                ["bash", "scripts/cutoff-drill.sh", "--proof"],
                env={
                    "PATH": str(directory) + os.pathsep + os.defpath,
                    "HOME": temporary,
                    "OPENROUTER_API_KEY": OR_KEY,
                    "NEBIUS_API_KEY": NEBIUS_KEY,
                    "NEBIUS_ONLY": "1",
                    "PROOF_OBSERVATION": str(observed),
                },
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(result.returncode, 79, result.stdout + result.stderr)
            self.assertEqual(observed.read_text(), "||1")
            self.assertNotIn(OR_KEY, result.stdout + result.stderr)
            self.assertNotIn(NEBIUS_KEY, result.stdout + result.stderr)
