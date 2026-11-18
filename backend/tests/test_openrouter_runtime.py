import contextlib
import io
import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import ledger
import model_router
import runs


class OpenRouterSkipVisibilityTest(unittest.TestCase):
    def test_missing_keys_are_visible_with_logging_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / "ledger.jsonl")
            with (
                mock.patch.object(ledger, "LEDGER", path),
                mock.patch.object(runs, "LEDGER", path),
                mock.patch.object(model_router, "ollama_reachable", return_value=False),
                mock.patch.object(model_router.logger, "disabled", True),
            ):
                for key in (None, "", "  "):
                    env = {"NEBIUS_API_KEY": "synthetic-nebius"}
                    if key is not None:
                        env["OPENROUTER_API_KEY"] = key
                    with self.subTest(key=key):
                        output = io.StringIO()
                        with (
                            mock.patch.dict(os.environ, env, clear=True),
                            contextlib.redirect_stderr(output),
                        ):
                            spec = model_router.resolve_llm()
                        self.assertEqual(spec["provider"], "nebius")
                        self.assertIn("OpenRouter skipped", output.getvalue())


class OpenRouterLedgerIdentityTest(unittest.TestCase):
    def setUp(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(root) / "ledger.jsonl"
        self.enterContext(mock.patch.dict(os.environ, {}, clear=True))
        self.enterContext(mock.patch.object(ledger, "LEDGER", str(self.path)))
        self.enterContext(mock.patch.object(runs, "LEDGER", str(self.path)))

    def record(self, model, script="auditor.py", usage=None):
        return ledger.record(
            script,
            model,
            "openrouter-identity-proof",
            [],
            0,
            provider="openrouter",
            usage=usage,
        )

    def test_graph_style_bare_free_models_do_not_poison_spend(self):
        for script in ("issue_generator.py", "auditor.py"):
            for model in ("openrouter/free", "reasoner:free"):
                for usage in ({}, {"input_tokens": 23, "output_tokens": 7}):
                    with self.subTest(script=script, model=model, usage=usage):
                        row = self.record(model, script, usage)
                        self.assertEqual(row["model"], model)
                        self.assertEqual(row["provider"], "openrouter")
                        self.assertEqual((row["cost_usd"], row["metered"]), (0.0, True))
        self.assertEqual(runs.total_metered_spend(), {"spent": 0.0, "unknown": 0})
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}):
            self.assertEqual(
                model_router.resolve_llm("openrouter")["provider"], "openrouter"
            )

    def test_paid_bare_model_does_not_borrow_nebius_rate(self):
        row = self.record("zai-org/GLM-5.3-Flash", usage={"input_tokens": 23})
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(runs.total_metered_spend()["unknown"], 1)

    def test_missing_model_stays_unrated(self):
        row = self.record(None)
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(runs.total_metered_spend()["unknown"], 1)

    def test_old_unrated_free_row_uses_recorded_provider_for_requote(self):
        self.path.write_text(
            json.dumps(
                {
                    "kind": "deep-agent-run",
                    "model": "openrouter/free",
                    "provider": "openrouter",
                    "usage": {"input_tokens": 23},
                    "cost_usd": None,
                    "metered": True,
                }
            )
            + "\n"
        )
        self.assertEqual(runs.total_metered_spend(), {"spent": 0.0, "unknown": 0})


class OpenRouterFinalModelPolicyTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic"}, clear=True)
        )
        self.enterContext(
            mock.patch.object(
                runs, "total_metered_spend", return_value={"spent": 0.0, "unknown": 0}
            )
        )
        self.sdk = types.ModuleType("langchain_openai")
        self.sdk.ChatOpenAI = mock.Mock()
        self.enterContext(
            mock.patch.dict("sys.modules", {"langchain_openai": self.sdk})
        )
        self.spec = model_router.resolve_llm("openrouter")

    def test_paid_post_resolution_override_is_rejected_before_construction(self):
        os.environ["S5_AUDIT_MODEL"] = "contract/paid-reasoner"
        final = dict(self.spec, model=os.environ["S5_AUDIT_MODEL"])
        with self.assertRaisesRegex(RuntimeError, "OPENROUTER_ALLOW_PAID"):
            model_router.build_chat_model(spec=final)
        self.sdk.ChatOpenAI.assert_not_called()

    def test_structured_builder_cannot_bypass_paid_override_policy(self):
        with self.assertRaisesRegex(RuntimeError, "OPENROUTER_ALLOW_PAID"):
            model_router.build_structured_chat_model(
                spec=dict(self.spec, model="contract/paid-reasoner"), schema=object()
            )
        self.sdk.ChatOpenAI.assert_not_called()

    def test_free_and_empty_environment_overrides_build_free_models(self):
        for override in ("contract/reasoner:free", "openrouter/free", "", "  "):
            with self.subTest(override=override):
                final = (
                    dict(self.spec, model=override.strip())
                    if override.strip()
                    else self.spec
                )
                _, spec = model_router.build_chat_model(spec=final)
                self.assertEqual(spec["model"], override.strip() or self.spec["model"])
                self.assertEqual(
                    self.sdk.ChatOpenAI.call_args.kwargs["model"], spec["model"]
                )

    def test_explicit_paid_opt_in_allows_the_final_override(self):
        os.environ["OPENROUTER_ALLOW_PAID"] = "1"
        _, final = model_router.build_chat_model(
            spec=dict(self.spec, model="contract/paid-reasoner")
        )
        self.assertEqual(final["model"], "contract/paid-reasoner")
        self.assertEqual(self.sdk.ChatOpenAI.call_args.kwargs["api_key"], "synthetic")
