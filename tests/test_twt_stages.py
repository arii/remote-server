import os
import unittest
from unittest import mock

import model_router


class TestTwtStageModels(unittest.TestCase):
    def test_stage_policy_reaches_openai_request_body_without_selecting_paid_route(
        self,
    ):
        spec = {
            "provider": "twt",
            "model": "default",
            "base_url": "http://127.0.0.1:8000/v1",
        }
        for stage in ("spec", "coder", "audit"):
            with self.subTest(stage=stage), mock.patch.dict(os.environ, {}, clear=True):
                model, resolved = model_router.build_chat_model(spec=spec, stage=stage)
                body = model._get_request_payload(
                    [{"role": "user", "content": "hello"}]
                )
                self.assertEqual(body["model"], "default")
                self.assertEqual(
                    body["extra_body"],
                    {"provider_models": {"nebius": "zai-org/GLM-5.3-Flash"}},
                )
                self.assertEqual(
                    resolved["provider_models"], body["extra_body"]["provider_models"]
                )
                self.assertNotIn("provider_models", spec)

    def test_unstaged_request_keeps_default_route(self):
        model, spec = model_router.build_chat_model(
            spec={
                "provider": "twt",
                "model": "default",
                "base_url": "http://127.0.0.1:8000/v1",
            }
        )
        self.assertEqual(model.extra_body, None)
        self.assertNotIn("provider_models", spec)

    def test_local_stage_never_gets_paid_model_selection(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            model, spec = model_router.build_chat_model(
                spec=model_router._ollama_spec(), stage="coder"
            )
        self.assertNotIn("provider_models", spec)
        self.assertEqual(model.model, spec["model"])

    def test_unknown_stage_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unknown pipeline model stage"):
            model_router.build_chat_model(spec={"provider": "twt"}, stage="typo")

    def test_per_stage_override_is_centralized(self):
        with mock.patch.dict(os.environ, {"TWT_AUDIT_NEBIUS_MODEL": "other"}):
            _, spec = model_router.build_chat_model(
                spec={
                    "provider": "twt",
                    "model": "default",
                    "base_url": "http://127.0.0.1:8000/v1",
                },
                stage="audit",
            )
        self.assertEqual(spec["provider_models"], {"nebius": "other"})
