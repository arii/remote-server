import asyncio
import contextlib
import importlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.shared import ledger
import coder
import deep_chat
import llm_usage
from backend.shared import model_router

try:
    from langchain_core.messages import (
        AIMessage,
        HumanMessage,
        SystemMessage,
        ToolMessage,
    )
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_openai import ChatOpenAI

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class BoundsTests(unittest.TestCase):
    def test_client_side_history_rejection_records_no_dispatch_zero_cost(self):
        for asynchronous in (False, True):
            with (
                self.subTest(asynchronous=asynchronous),
                tempfile.TemporaryDirectory() as root,
                mock.patch.object(ledger, "LEDGER", str(Path(root, "ledger.jsonl"))),
                mock.patch.dict(os.environ, {"DEEP_AGENT_STATE_DIR": root}),
            ):
                model, _ = model_router.build_chat_model(
                    {
                        "provider": "twt",
                        "model": "default",
                        "base_url": "http://localhost/v1",
                    }
                )
                callback = llm_usage.usage_callback(
                    {"provider": "twt", "model": "default"},
                    "rejected",
                    script="proof.py",
                    stage="spec",
                    phase="draft",
                )
                with self.assertRaisesRegex(ValueError, "history exceeds"):
                    if asynchronous:
                        asyncio.run(
                            model.ainvoke(
                                [HumanMessage("x" * 100000)],
                                config={"callbacks": [callback]},
                            )
                        )
                    else:
                        model.invoke(
                            [HumanMessage("x" * 100000)],
                            config={"callbacks": [callback]},
                        )
                row = json.loads(Path(ledger.LEDGER).read_text().splitlines()[-1])
                self.assertEqual(row["cost_usd"], 0)
                self.assertFalse(row["metered"])
                self.assertFalse(row["extra"]["llm_call"])
                self.assertTrue(row["usage"]["not_dispatched"])

    def test_coder_setup_failure_records_no_dispatch_zero_cost(self):
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(ledger, "LEDGER", str(Path(root, "ledger.jsonl"))),
            mock.patch.dict(os.environ, {"DEEP_AGENT_STATE_DIR": root}),
            mock.patch.object(
                coder,
                "build_model",
                return_value=(object(), {"provider": "twt", "model": "default"}),
            ),
            mock.patch.object(
                deep_chat, "open_checkpointer", side_effect=RuntimeError("setup failed")
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "setup failed"):
                coder.run_coder(root, "brief", "system", "setup", 1)
            row = json.loads(Path(ledger.LEDGER).read_text().splitlines()[-1])
            self.assertEqual(row["cost_usd"], 0)
            self.assertFalse(row["metered"])
            self.assertFalse(row["extra"]["llm_call"])

    def test_fanout_and_resolver_merge_explicit_output_budget(self):
        for name, budget in (("triage_fanout", 2000), ("resolve_conflicts", 4000)):
            module = importlib.import_module(name)
            for ceiling in (8192, 1500):
                with (
                    self.subTest(entrypoint=name, ceiling=ceiling),
                    tempfile.TemporaryDirectory() as root,
                ):
                    Path(root, ".git").mkdir()
                    args = [
                        name + ".py",
                        "arii/remote-server" if name == "triage_fanout" else root,
                    ]
                    spec = {
                        "provider": "twt",
                        "model": "default",
                        "base_url": "http://localhost/v1",
                    }
                    agent = mock.Mock()
                    agent.invoke.return_value = {"messages": [AIMessage("done")]}
                    with (
                        mock.patch.object(sys, "argv", args),
                        mock.patch.dict(
                            os.environ, {"DEEP_AGENT_STATE_DIR": root}, clear=True
                        ),
                        mock.patch.object(module, "load_dotenv"),
                        mock.patch.object(
                            model_router, "resolve_llm", return_value=spec
                        ),
                        mock.patch.object(model_router, "PIPELINE_MAX_TOKENS", ceiling),
                        mock.patch.object(
                            module, "build_agent", return_value=agent
                        ) as build_agent,
                        mock.patch.object(ledger, "record"),
                        contextlib.redirect_stdout(io.StringIO()),
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(module.main(), 0)
                    (model,) = [
                        arg
                        for arg in build_agent.call_args.args
                        if isinstance(arg, ChatOpenAI)
                    ]
                    self.assertEqual(model.max_tokens, min(budget, ceiling))

    def test_history_empty_normal_and_oversized(self):
        self.assertEqual(model_router.bounded_history([]), [])
        normal = [SystemMessage("rules"), HumanMessage("task")]
        self.assertEqual(model_router.bounded_history(normal), normal)
        messages = normal + [
            AIMessage("", tool_calls=[{"id": "read", "name": "read", "args": {}}]),
            ToolMessage("x" * 100000, tool_call_id="read"),
        ]
        bounded = model_router.bounded_history(messages)
        self.assertEqual(bounded[:2], normal)
        self.assertEqual(bounded[-1].tool_call_id, "read")
        self.assertIn("[history trimmed]", bounded[-1].content)
        self.assertEqual(len(messages[-1].content), 100000)

    def test_drop_complete_old_tool_groups_not_instructions(self):
        messages = [SystemMessage("rules"), HumanMessage("task")]
        for i in range(40):
            messages += [
                AIMessage("", tool_calls=[{"id": str(i), "name": "read", "args": {}}]),
                ToolMessage("x" * 4000, tool_call_id=str(i)),
            ]
        result = model_router.bounded_history(messages)
        self.assertEqual(result[:2], messages[:2])
        self.assertEqual(result[-1].tool_call_id, "39")
        self.assertLess(sum(len(json.dumps(m.model_dump())) for m in result), 64000)
        for index, message in enumerate(result):
            if message.type == "tool":
                self.assertEqual(
                    result[index - 1].tool_calls[0]["id"], message.tool_call_id
                )
        with self.assertRaises(ValueError):
            model_router.bounded_history([HumanMessage("x" * 100000)])

    def test_sync_async_models_bound_history_and_output(self):
        model, _ = model_router.build_chat_model(
            {
                "provider": "twt",
                "model": "default",
                "base_url": "http://localhost:8000/v1",
            }
        )
        self.assertEqual(model.max_tokens, 8192)
        self.assertEqual(
            model._get_request_payload([HumanMessage("task")])["max_tokens"], 8192
        )
        messages = [HumanMessage("task"), AIMessage("x" * 100000)]
        result = ChatResult(generations=[ChatGeneration(message=AIMessage("OK"))])
        with mock.patch.object(
            ChatOpenAI, "_generate", return_value=result
        ) as generate:
            model.bind(max_tokens=999999).invoke(messages)
            self.assertLess(len(generate.call_args.args[0][-1].content), 5000)
            self.assertEqual(generate.call_args.kwargs["max_tokens"], 8192)
        with mock.patch.object(
            ChatOpenAI, "_agenerate", return_value=result
        ) as generate:
            asyncio.run(model.ainvoke(messages))
            self.assertLess(len(generate.call_args.args[0][-1].content), 5000)
        local = model_router.chat_kwargs(
            {
                "provider": "ollama",
                "model": "qwen3:8b",
                "base_url": "http://localhost/local",
            }
        )
        self.assertEqual(local["num_predict"], 8192)

    def test_error_headers_reach_ledger_without_fabricating_usage(self):
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(ledger, "LEDGER", str(Path(root, "ledger.jsonl"))),
            mock.patch.dict(os.environ, {"DEEP_AGENT_STATE_DIR": root}),
        ):
            for index, headers in enumerate(
                ({"x-cost-usd": "0.007"}, {}, {"x-cost-usd": "NaN"})
            ):
                callback = llm_usage.usage_callback(
                    {"provider": "twt", "model": "default"},
                    "proof-errors",
                    script="proof.py",
                    stage="spec",
                    phase="investigation",
                )
                callback.on_chat_model_start({}, [[HumanMessage("task")]], run_id=index)
                error = RuntimeError("502")
                error.response = SimpleNamespace(headers=headers)
                callback.on_llm_error(error, run_id=index)
            rows = [
                json.loads(line)
                for line in Path(root, "ledger.jsonl").read_text().splitlines()
            ]
            self.assertEqual([row["cost_usd"] for row in rows], [0.007, None, None])
            self.assertTrue(all(row["extra"]["usage_missing"] for row in rows))
            self.assertTrue(all(row["extra"]["error"] for row in rows))
