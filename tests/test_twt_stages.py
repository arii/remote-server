import asyncio
import importlib.util
import json
import os
import unittest
from unittest import mock

from backend.shared import model_router


class TestTwtStageModels(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("langchain_openai"), "needs deep-agent venv"
    )
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
                    {
                        "provider_models": {
                            "nvidia": "z-ai/glm-5.3-flash",
                            "nebius": "zai-org/GLM-5.3-Flash",
                        },
                        "provider_reasoning_effort": {"nvidia": "low", "nebius": "low"},
                    },
                )
                self.assertEqual(
                    resolved["provider_models"], body["extra_body"]["provider_models"]
                )
                self.assertNotIn("provider_models", spec)

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_openai"), "needs deep-agent venv"
    )
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

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_ollama"), "needs deep-agent venv"
    )
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

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_openai"), "needs deep-agent venv"
    )
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
        self.assertEqual(spec["provider_models"]["nebius"], "other")

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_openai"), "needs deep-agent venv"
    )
    def test_per_stage_free_provider_override_supported(self):
        env = {
            "TWT_CODER_GROQ_MODEL": "llama-3.3-70b-versatile",
            "TWT_CODER_OPENROUTER_MODEL": "qwen/qwen-2.5-coder-32b-instruct:free",
        }
        with mock.patch.dict(os.environ, env):
            _, spec = model_router.build_chat_model(
                spec={
                    "provider": "twt",
                    "model": "default",
                    "base_url": "http://127.0.0.1:8000/v1",
                },
                stage="coder",
            )
        self.assertEqual(spec["provider_models"]["groq"], "llama-3.3-70b-versatile")
        self.assertEqual(
            spec["provider_models"]["openrouter"],
            "qwen/qwen-2.5-coder-32b-instruct:free",
        )

    @unittest.skipUnless(importlib.util.find_spec("openai"), "needs deep-agent venv")
    def test_stage_controls_on_actual_sync_and_async_sdk_wire(self):
        import httpx
        from openai import AsyncOpenAI, OpenAI

        sent = []

        def handler(request):
            body = json.loads(request.content)
            sent.append(body)
            self.assertEqual(body["model"], "default")
            self.assertEqual(body["max_tokens"], 8192)
            self.assertEqual(body["provider_models"]["nvidia"], "z-ai/glm-5.3-flash")
            self.assertEqual(body["provider_models"]["nebius"], "zai-org/GLM-5.3-Flash")
            self.assertEqual(
                body["provider_reasoning_effort"], {"nvidia": "low", "nebius": "low"}
            )
            return httpx.Response(
                200,
                json={
                    "id": "wire",
                    "object": "chat.completion",
                    "model": "glm",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )

        for stage in ("spec", "coder", "audit"):
            for tools in (False, True):
                with self.subTest(stage=stage, tools=tools):
                    model, _ = model_router.build_chat_model(
                        {
                            "provider": "twt",
                            "model": "default",
                            "base_url": "http://gateway.test/v1",
                        },
                        stage=stage,
                    )
                    model.client = OpenAI(
                        api_key="test",
                        base_url="http://gateway.test/v1",
                        http_client=httpx.Client(
                            transport=httpx.MockTransport(handler)
                        ),
                    ).chat.completions
                    model.async_client = AsyncOpenAI(
                        api_key="test",
                        base_url="http://gateway.test/v1",
                        http_client=httpx.AsyncClient(
                            transport=httpx.MockTransport(handler)
                        ),
                    ).chat.completions
                    bound = (
                        model.bind_tools(
                            [
                                {
                                    "type": "function",
                                    "function": {
                                        "name": "read",
                                        "parameters": {
                                            "type": "object",
                                            "properties": {},
                                        },
                                    },
                                }
                            ]
                        )
                        if tools
                        else model
                    )
                    bound.invoke("task")
                    asyncio.run(bound.ainvoke("task"))
                    self.assertEqual(bool(sent[-1].get("tools")), tools)
        self.assertEqual(len(sent), 12)

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_openai"), "needs deep-agent venv"
    )
    def test_concrete_model_sends_no_provider_controls_default_route_keeps_them(self):
        base = {"provider": "twt", "base_url": "http://127.0.0.1:8000/v1"}
        concrete = "nebius/Qwen/Qwen3-235B-A22B-Instruct-2507"
        cases = (("default", True), ("route:default", True), (concrete, False))
        for model_id, expects_controls in cases:
            with (
                self.subTest(model=model_id),
                mock.patch.dict(os.environ, {}, clear=True),
            ):
                model, spec = model_router.build_chat_model(
                    spec=dict(base, model=model_id), stage="coder"
                )
                body = model._get_request_payload(
                    [{"role": "user", "content": "hello"}]
                )
                self.assertEqual(body["model"], model_id)
                extra_body = body.get("extra_body") or {}
                for key in ("provider_models", "provider_reasoning_effort"):
                    self.assertEqual(key in extra_body, expects_controls)
                    self.assertEqual(key in spec, expects_controls)

    @unittest.skipUnless(
        importlib.util.find_spec("langchain_openai"), "needs deep-agent venv"
    )
    def test_concrete_pin_strips_inherited_provider_models(self):
        spec = {
            "provider": "twt",
            "model": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
            "base_url": "http://127.0.0.1:8000/v1",
            "provider_models": {"nebius": "zai-org/GLM-5.3-Flash"},
            "provider_reasoning_effort": {"nebius": "low"},
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            model, resolved = model_router.build_chat_model(spec=spec, stage="coder")
        body = model._get_request_payload([{"role": "user", "content": "hello"}])
        extra_body = body.get("extra_body") or {}
        self.assertNotIn("provider_models", extra_body)
        self.assertNotIn("provider_reasoning_effort", extra_body)
        self.assertNotIn("provider_models", resolved)
        self.assertNotIn("provider_reasoning_effort", resolved)

    def test_preflight_strips_provider_models_on_explicit_pin_and_keeps_route_default(
        self,
    ):
        from backend.shared import twt_provenance

        calls = []

        def fake_verify(**kwargs):
            calls.append(kwargs)

        with (
            mock.patch.object(
                twt_provenance, "verify_gateway_capabilities", side_effect=fake_verify
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            model_router.preflight_gateway(
                spec={
                    "provider": "twt",
                    "model": "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
                    "base_url": "http://127.0.0.1:8000/v1",
                    "provider_models": {"nebius": "zai-org/GLM-5.3-Flash"},
                },
                stage="coder",
            )
            model_router.preflight_gateway(
                spec={
                    "provider": "twt",
                    "model": "route:default",
                    "base_url": "http://127.0.0.1:8000/v1",
                },
                stage="coder",
            )
        self.assertEqual(calls[0]["provider_models"], {})
        self.assertEqual(
            calls[0]["model_ids"], ["openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"]
        )
        self.assertEqual(
            calls[1]["provider_models"],
            {"nvidia": "z-ai/glm-5.3-flash", "nebius": "zai-org/GLM-5.3-Flash"},
        )
        self.assertNotIn("model_ids", calls[1])
