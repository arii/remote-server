"""observe.py tests. Needs the deep-agent venv (trace_middleware
imports langchain hooks). No model calls: middleware is driven with
fake requests/handlers/state. Recorder core is stdlib-only.
Run: deep-agent/.venv/bin/python tests/test_observe.py"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import observe


class TestRecorder(unittest.TestCase):
    def test_events_are_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            rec = observe.TraceRecorder(path)
            rec.run_start("s.py", "thread-1", "ollama:qwen3:8b")
            rec.model_request(3)
            rec.model_response({"input_tokens": 10})
            rec.tool_call("grep_repo", {"pattern": "x"}, 0.5, True,
                          result="hit")
            rec.tool_call("bad", {}, 0.1, False, error="boom")
            rec.run_end(True)
            rec.close()
            lines = [json.loads(line) for line in open(path)]
            types = [e["type"] for e in lines]
            self.assertEqual(
                types, ["run_start", "model_request", "model_response",
                        "tool_call", "tool_call", "run_end"])
            self.assertTrue(all("ts" in e for e in lines))
            self.assertEqual(lines[2]["usage"]["input_tokens"], 10)
            self.assertEqual(lines[2]["usage"]["output_tokens"], 0)
            self.assertTrue(lines[3]["ok"])
            self.assertFalse(lines[4]["ok"])
            self.assertEqual(lines[4]["error"], "boom")

    def test_preview_truncates(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "t.jsonl")
            rec = observe.TraceRecorder(path)
            rec.tool_call("t", "y" * 3000, 0.0, True, result="z" * 3000)
            rec.close()
            event = json.loads(open(path).readline())
            self.assertLess(len(event["args_preview"]), 2100)
            self.assertIn("truncated", event["result_preview"])

    def test_never_raises(self):
        rec = observe.TraceRecorder("/nonexistent-dir-xyz/t.jsonl")
        rec.run_start("s", "t", "m")
        rec.run_end()
        rec.close()

    def test_default_path(self):
        path = observe.default_trace_path("ask_investigator", "ask-a-b")
        self.assertIn("deep-agent/traces", path)
        self.assertIn("ask_investigator_ask-a-b_", path)
        self.assertTrue(os.path.isdir(os.path.dirname(path)))
        evil = observe.default_trace_path("s", "../../etc/x")
        self.assertNotIn("..", evil)


class TestMiddleware(unittest.TestCase):
    def test_wrap_tool_call_ok_and_error(self):
        with tempfile.TemporaryDirectory() as d:
            rec = observe.TraceRecorder(os.path.join(d, "t.jsonl"))
            _, _, wrap_mw = observe.trace_middleware(rec)
            wrap = wrap_mw.wrap_tool_call
            req = SimpleNamespace(
                tool_call={"name": "grep_repo",
                           "args": {"pattern": "x"}})
            out = wrap(req, lambda r: "hit!")
            self.assertEqual(out, "hit!")
            with self.assertRaises(RuntimeError):
                def boom(r):
                    raise RuntimeError("nope")
                wrap(req, boom)
            rec.close()
            events = [json.loads(line) for line in
                      open(os.path.join(d, "t.jsonl"))]
            calls = [e for e in events if e["type"] == "tool_call"]
            self.assertEqual(len(calls), 2)
            self.assertTrue(calls[0]["ok"])
            self.assertIn("hit!", calls[0]["result_preview"])
            self.assertFalse(calls[1]["ok"])
            self.assertIn("nope", calls[1]["error"])

    def test_model_hooks(self):
        with tempfile.TemporaryDirectory() as d:
            rec = observe.TraceRecorder(os.path.join(d, "t.jsonl"))
            before_mw, after_mw, _ = observe.trace_middleware(rec)
            before_mw.before_model({"messages": [{}, {}]}, None)
            msg = SimpleNamespace(
                usage_metadata={"input_tokens": 5, "output_tokens": 7,
                                "total_tokens": 12})
            after_mw.after_model({"messages": [msg]}, None)
            after_mw.after_model({}, None)
            rec.close()
            events = [json.loads(line) for line in
                      open(os.path.join(d, "t.jsonl"))]
            types = [e["type"] for e in events]
            self.assertEqual(
                types, ["model_request", "model_response",
                        "model_response"])
            self.assertEqual(events[0]["n_messages"], 2)
            self.assertEqual(events[1]["usage"]["total_tokens"], 12)


    def test_tool_trace_through_real_agent(self):
        from langchain.agents import create_agent
        from langchain_core.language_models.chat_models import (
            BaseChatModel,
        )
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain_core.tools import tool
        from pydantic import Field

        class QueueChatModel(BaseChatModel):
            replies: list = Field(default_factory=list)

            @property
            def _llm_type(self):
                return "queue-fake"

            def _generate(self, messages, stop=None, run_manager=None,
                          **kwargs):
                msg = self.replies.pop(0) if self.replies \
                    else AIMessage(content="")
                return ChatResult(
                    generations=[ChatGeneration(message=msg)])

            def bind_tools(self, tools, **kwargs):
                return self

        @tool
        def probe(q: str) -> str:
            """Probe tool.

            Args:
                q: query.
            """
            return f"pong:{q}"

        model = QueueChatModel(replies=[
            AIMessage(content="", tool_calls=[{
                "name": "probe", "args": {"q": "x"}, "id": "call-1",
                "type": "tool_call"}]),
            AIMessage(content="done"),
        ])
        with tempfile.TemporaryDirectory() as d:
            rec = observe.TraceRecorder(os.path.join(d, "t.jsonl"))
            agent = create_agent(
                model=model, tools=[probe],
                middleware=observe.trace_middleware(rec))
            agent.invoke({"messages": [{"role": "user", "content": "go"}]},
                         config={"recursion_limit": 25})
            rec.close()
            events = [json.loads(line) for line in
                      open(os.path.join(d, "t.jsonl"))]
            calls = [e for e in events if e["type"] == "tool_call"]
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["name"], "probe")
            self.assertTrue(calls[0]["ok"])
            self.assertIn("pong:x", calls[0]["result_preview"])


if __name__ == "__main__":
    unittest.main()
