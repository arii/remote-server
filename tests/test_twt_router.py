import asyncio
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ledger
import model_router
import observe
import pricing
import runs

if importlib.util.find_spec("twt"):
    import httpx
    import twt_service
    from twt.store import Store
else:
    twt_service = None

if importlib.util.find_spec("langchain_ollama") and importlib.util.find_spec(
    "langchain_openai"
):
    from langchain_ollama import ChatOllama
    from langchain_openai import ChatOpenAI
else:
    ChatOllama = ChatOpenAI = None


def message(cost=None, usage_cost=None):
    return SimpleNamespace(
        type="ai",
        content="OK",
        tool_calls=[],
        usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
        response_metadata={
            "headers": {"X-Cost-Usd": cost},
            "token_usage": {"cost": usage_cost},
        },
    )


class TestTwTRouter(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(
            ledger, "LEDGER", str(Path(directory.name, "ledger.jsonl"))
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        reader = mock.patch.object(runs, "LEDGER", ledger.LEDGER)
        reader.start()
        self.addCleanup(reader.stop)

    def test_default_and_local_paths_are_both_gateway_urls(self):
        with mock.patch.dict(
            os.environ, {"TWT_BASE_URL": "http://localhost:18000/v1/"}, clear=True
        ):
            with mock.patch.object(model_router, "ollama_reachable", return_value=True):
                local = model_router.resolve_llm()
            hosted = model_router.resolve_llm(prefer="twt")
        self.assertEqual(local["provider"], "ollama")
        self.assertEqual(local["base_url"], "http://localhost:18000/local")
        self.assertEqual(hosted["provider"], "twt")
        self.assertEqual(hosted["model"], "default")
        kwargs = model_router.chat_kwargs(hosted)
        self.assertTrue(kwargs["include_response_headers"])
        self.assertTrue(kwargs["disable_streaming"])
        self.assertEqual(kwargs["max_retries"], 0)
        self.assertEqual(model_router.api_key(hosted), "twt-loopback")

    def test_provider_keys_never_change_automatic_gateway_fallback(self):
        with (
            mock.patch.dict(
                os.environ,
                {"OPENROUTER_API_KEY": "unused", "NEBIUS_API_KEY": "unused"},
                clear=True,
            ),
            mock.patch.object(model_router, "ollama_reachable", return_value=False),
            mock.patch.object(ledger, "record"),
        ):
            self.assertEqual(model_router.resolve_llm()["provider"], "twt")
        with mock.patch.dict(os.environ, {"NEBIUS_ONLY": "1"}, clear=True):
            self.assertEqual(model_router.resolve_llm()["provider"], "twt")

    def test_gateway_local_only_never_uses_hosted_path(self):
        with (
            mock.patch.dict(os.environ, {"S5_LOCAL_ONLY": "1"}, clear=True),
            mock.patch.object(model_router, "ollama_reachable", return_value=True),
        ):
            spec = model_router.resolve_llm(prefer="twt")
        self.assertEqual(spec["provider"], "ollama")
        self.assertTrue(spec["base_url"].endswith("/local"))


class TestTwTCost(unittest.TestCase):
    def record(self, messages, usage=None):
        return ledger.record(
            "proof",
            "default",
            "gateway-proof",
            messages,
            time.time(),
            provider="twt",
            usage=usage,
        )

    def test_header_usage_fallback_and_aggregate_reach_spend_gate(self):
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(ledger, "LEDGER", str(Path(root, "ledger.jsonl"))),
        ):
            for msg, expected in (
                (message("0.002", 9), 0.002),
                (message("unknown", 0.003), 0.003),
                (message("0"), 0),
            ):
                row = self.record([msg])
                self.assertEqual(row["cost_usd"], expected)
                self.assertTrue(row["metered"])
            recorder = observe.TraceRecorder(str(Path(root, "trace.jsonl")))
            recorder.model_response(ledger._usage_total([message("0.004")]))
            recorder.model_response(ledger._usage_total([message(usage_cost=0.001)]))
            usage, _ = recorder.totals()
            recorder.close()
            self.assertEqual(self.record([], usage)["cost_usd"], 0.005)
            total = runs.total_metered_spend(ledger.LEDGER)
            self.assertAlmostEqual(total["spent"], 0.01)
            self.assertEqual(total["unknown"], 0)

    def test_empty_missing_malformed_and_partial_cost_fail_closed(self):
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(ledger, "LEDGER", str(Path(root, "ledger.jsonl"))),
        ):
            for messages in (
                [],
                [message()],
                [message("NaN")],
                [message("-1")],
                [message(True)],
                [message("inf")],
                [message("0"), message()],
            ):
                self.assertIsNone(self.record(messages)["cost_usd"])
            recorder = observe.TraceRecorder(str(Path(root, "trace.jsonl")))
            recorder.model_response(ledger._usage_total([message("0")]))
            recorder.model_response(ledger._usage_total([message()]))
            usage, _ = recorder.totals()
            recorder.close()
            self.assertIsNone(self.record([], usage)["cost_usd"])
            self.assertEqual(runs.total_metered_spend(ledger.LEDGER)["unknown"], 8)
        with mock.patch.dict(
            os.environ, {"METERED_RATE_IN_PER_1M": "0", "METERED_RATE_OUT_PER_1M": "0"}
        ):
            self.assertEqual(pricing.quote("twt:default", {}), (None, True))


@unittest.skipUnless(
    twt_service is not None, "install pinned tokens-with-taste to test service adapter"
)
class TestTwTService(unittest.TestCase):
    def scenario(self, invalid=False, tool_only=False, sdk=False):
        async def run():
            native_calls, hosted_calls = [], []

            def native(request):
                body = json.loads(request.content)
                native_calls.append((request.url.path, body))
                if request.url.path == "/api/show":
                    return httpx.Response(
                        200,
                        json={
                            "capabilities": ["completion", "tools"],
                            **(
                                {"remote_host": "https://ollama.com"} if invalid else {}
                            ),
                        },
                    )
                self.assertEqual(request.url.path, "/api/chat")
                msg = {"role": "assistant", "content": "" if tool_only else "OK"}
                if tool_only:
                    msg["tool_calls"] = [
                        {"function": {"name": "read", "arguments": {"path": "a.py"}}}
                    ]
                return httpx.Response(
                    200,
                    json={
                        "model": "qwen3:8b",
                        "message": msg,
                        "done": True,
                        "prompt_eval_count": 10,
                        "eval_count": 2,
                    },
                )

            def hosted(request):
                hosted_calls.append(request.url.path)
                return httpx.Response(
                    200,
                    json={
                        "id": "hosted-proof",
                        "object": "chat.completion",
                        "created": 1,
                        "model": "test:free",
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": "HOSTED"},
                            }
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    },
                )

            with (
                tempfile.TemporaryDirectory() as root,
                mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "fake"}, clear=True),
            ):
                store = Store(Path(root, "db.sqlite3"))
                store.replace_models(
                    "openrouter",
                    [
                        {
                            "provider": "openrouter",
                            "id": "test:free",
                            "chat": True,
                            "free": True,
                            "tools": True,
                            "input_price": 0.0,
                            "output_price": 0.0,
                            "context_length": None,
                        }
                    ],
                )
                async with (
                    httpx.AsyncClient(transport=httpx.MockTransport(native)) as local,
                    httpx.AsyncClient(transport=httpx.MockTransport(hosted)) as remote,
                ):
                    app = twt_service.create_app(store, local, remote)
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app), base_url="http://gateway"
                    ) as client:
                        body = {
                            "model": "qwen3:8b",
                            "messages": [{"role": "user", "content": "Hello"}],
                            "stream": True,
                            "think": False,
                            "options": {"num_ctx": 16384},
                            "format": {"type": "object"},
                        }
                        response = await client.post("/local/api/chat", json=body)
                        self.assertEqual(response.status_code, 502 if invalid else 200)
                        self.assertEqual(hosted_calls, [])
                        trace = store.trace(response.headers["x-trace-id"])
                        self.assertEqual(
                            [a["provider"] for a in trace["attempts"]], ["ollama-local"]
                        )
                        if not invalid:
                            self.assertEqual(response.json()["done"], True)
                            self.assertEqual(trace["provider"], "ollama-local")
                            self.assertEqual(trace["cost_usd"], 0)
                            sent = native_calls[-1][1]
                            self.assertEqual(sent, {**body, "stream": False})
                            if sdk:
                                native_model = ChatOllama(
                                    model="qwen3:8b",
                                    base_url="http://gateway/local",
                                    reasoning=False,
                                    async_client_kwargs={
                                        "transport": httpx.ASGITransport(app)
                                    },
                                )
                                native_reply = await native_model.ainvoke("Hello")
                                self.assertEqual(native_reply.content, "OK")
                                hosted_model = ChatOpenAI(
                                    api_key="fake",
                                    http_async_client=client,
                                    **model_router.chat_kwargs(
                                        {
                                            "provider": "twt",
                                            "model": "default",
                                            "base_url": "http://gateway/v1",
                                        }
                                    ),
                                )
                                hosted_reply = await hosted_model.ainvoke("Hello")
                                self.assertEqual(hosted_reply.content, "HOSTED")
                                self.assertEqual(
                                    ledger._reported_cost([hosted_reply]), 0
                                )
                            hosted_response = await client.post(
                                "/v1/chat/completions",
                                json={"model": "default", "messages": body["messages"]},
                            )
                            self.assertEqual(hosted_response.status_code, 200)
                            self.assertEqual(
                                {t["provider"] for t in store.traces()},
                                {"ollama-local", "openrouter"},
                            )
                        for bad in ({}, [], {**body, "model": "cloud-model"}):
                            response = await client.post("/local/api/chat", json=bad)
                            self.assertEqual(response.status_code, 400)
                        response = await client.post(
                            "/local/api/chat",
                            content="{",
                            headers={"content-type": "application/json"},
                        )
                        self.assertEqual(response.status_code, 400)
                        response = await client.post(
                            "/local/api/chat",
                            json=body,
                            headers={"origin": "https://evil.example"},
                        )
                        self.assertEqual(response.status_code, 403)
                store.close()

        asyncio.run(run())

    def test_local_and_hosted_share_native_twt_traces(self):
        self.scenario()

    def test_cloud_model_metadata_rejected_without_hosted_fallback(self):
        self.scenario(invalid=True)

    def test_native_tool_only_reply_preserved_and_audited(self):
        self.scenario(tool_only=True)

    @unittest.skipUnless(ChatOllama and ChatOpenAI, "LangChain SDKs required")
    def test_real_chat_sdks_and_gateway_cost_headers(self):
        self.scenario(sdk=True)

    def test_remote_destination_and_cloud_names_rejected(self):
        for base, model in (
            ("https://ollama.com", "qwen3:8b"),
            ("http://127.0.0.1:11434", "model-cloud"),
        ):
            with self.assertRaises(ValueError):
                twt_service.LocalOllama(None, model, base)
