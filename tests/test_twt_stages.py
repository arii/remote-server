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
