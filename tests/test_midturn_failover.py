"""Mid-turn Nebius->local failover tests (#649). No live LLM.

Part A runs under system python (predicate matrix + helper
semantics, stdlib-only). Part B needs the deep-agent venv and wires
each of the three model.invoke sites with stubbed models.
Run: python3 tests/test_midturn_failover.py  (A only, B skipped)
     deep-agent/.venv/bin/python tests/test_midturn_failover.py  (A + B)"""
import base64
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import model_router

try:
    import deep_chat
    import langchain_core  # noqa: F401
    from langgraph.errors import GraphRecursionError  # noqa: F401
    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def b64(text):
    return base64.b64encode(text.encode()).decode()


class CodedError(Exception):
    """OpenAI-compat shaped error: status attr + message."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code


class NestedError(Exception):
    """httpx/openai nesting: code lives on .response."""

    def __init__(self, status_code, message):
        super().__init__(message)
        self.response = SimpleNamespace(status_code=status_code)


NEBIUS_SPEC = {"provider": "nebius", "model": "n-model",
               "base_url": "http://cloud"}
OLLAMA_SPEC = {"provider": "ollama", "model": "qwen3:8b",
               "base_url": "http://local"}


class TestRetryablePredicate(unittest.TestCase):
    def test_retryable_status_codes(self):
        for code in (408, 425, 429, 500, 502, 503, 504, 529, 599):
            with self.subTest(code=code):
                self.assertTrue(model_router.is_retryable_error(
                    CodedError(code, f"boom {code}")))

    def test_non_retryable_status_codes(self):
        for code, msg in (
            (400, "invalid_request_error"),
            (400, "content filtered by policy"),
            (401, "unauthorized"),
            (402, "payment required"),
            (403, "forbidden"),
            (404, "model not found"),
            (405, "method not allowed"),
            (409, "conflict"),
            (422, "validation failed"),
        ):
            with self.subTest(code=code):
                self.assertFalse(model_router.is_retryable_error(
                    CodedError(code, msg)))

    def test_status_wins_over_message(self):
        # A coded auth error never fails over, even when its text
        # mentions something transient-sounding.
        self.assertFalse(model_router.is_retryable_error(
            CodedError(401, "token timeout, please log in and try again")))
        self.assertFalse(model_router.is_retryable_error(
            CodedError(404, "not found, try again with another model")))
        self.assertTrue(model_router.is_retryable_error(
            CodedError(503, "upstream hiccup")))

    def test_numeric_string_status(self):
        self.assertTrue(model_router.is_retryable_error(
            CodedError("503", "str code")))
        self.assertFalse(model_router.is_retryable_error(
            CodedError("401", "str code")))

    def test_nested_response_status(self):
        self.assertTrue(model_router.is_retryable_error(
            NestedError(503, "nested")))
        self.assertFalse(model_router.is_retryable_error(
            NestedError(404, "nested")))

    def test_none_status_falls_through_to_message(self):
        self.assertTrue(model_router.is_retryable_error(
            CodedError(None, "model overloaded, try again later")))
        self.assertFalse(model_router.is_retryable_error(
            CodedError(None, "bad request")))

    def test_retryable_type_names(self):
        for name in ("RateLimitError", "APITimeoutError",
                     "APIConnectionError", "InternalServerError",
                     "ServiceUnavailableError", "OverloadedError",
                     "ConnectError", "ReadTimeout", "TimeoutError",
                     "ConnectionError", "BadGateway", "GatewayTimeout"):
            cls = type(name, (Exception,), {})
            with self.subTest(name=name):
                self.assertTrue(model_router.is_retryable_error(
                    cls("something broke")))

    def test_builtin_timeout_and_connection(self):
        self.assertTrue(model_router.is_retryable_error(
            TimeoutError("timed out")))
        self.assertTrue(model_router.is_retryable_error(
            ConnectionError("reset by peer")))

    def test_subclass_of_transient_matches(self):
        class MyWobble(ConnectionError):
            pass

        self.assertTrue(model_router.is_retryable_error(
            MyWobble("custom name, transient base")))

    def test_retryable_messages_without_code(self):
        for msg in (
            "Rate limit exceeded, retry in 2s",
            "rate_limit_exceeded",
            "429 too many requests",
            "The model is overloaded",
            "Request timed out after 30s",
            "Connection reset by peer",
            "Connection refused",
            "temporarily unavailable",
            "Service Unavailable",
            "Internal Server Error",
            "Bad gateway from upstream",
            "Gateway Timeout",
            "please try again later",
            "connectionerror while reading response",
        ):
            with self.subTest(msg=msg):
                self.assertTrue(model_router.is_retryable_error(
                    RuntimeError(msg)))

    def test_non_retryable_messages_without_code(self):
        for msg in (
            "boom",
            "invalid prompt",
            "unauthorized: bad key",
            "model not found",
            "content filtered by moderation",
            "blocked by content policy",
            "Nebius spend cap hit: $0.0008 of $0.0000 used",
            "Ollama unreachable at http://ollama:11434",
            "bad provider 'x': want 'ollama' or 'nebius'",
        ):
            with self.subTest(msg=msg):
                self.assertFalse(model_router.is_retryable_error(
                    RuntimeError(msg)))


class TestWithLocalFallback(unittest.TestCase):
    def test_primary_success_touches_nothing(self):
        make_local = mock.Mock()
        note = mock.Mock()
        out = model_router.with_local_fallback(
            lambda: "primary-ok", make_local, note)
        self.assertEqual(out, "primary-ok")
        make_local.assert_not_called()
        note.assert_not_called()

    def test_retryable_falls_back_with_same_input(self):
        seen = []
        primary_err = CodedError(503, "Service Unavailable")

        def primary():
            seen.append("primary")
            raise primary_err

        def make_local():
            seen.append("built")
            return lambda: seen.append("fallback") or "local-ok"

        notes = []
        out = model_router.with_local_fallback(
            primary, make_local, notes.append)
        self.assertEqual(out, "local-ok")
        self.assertEqual(seen, ["primary", "built", "fallback"])
        self.assertEqual(notes, [primary_err])

    def test_non_retryable_never_builds_local(self):
        primary_err = CodedError(401, "unauthorized")
        make_local = mock.Mock()
        note = mock.Mock()
        def primary():
            raise primary_err

        with self.assertRaises(Exception) as ctx:
            model_router.with_local_fallback(
                primary, make_local, note)
        self.assertIs(ctx.exception, primary_err)
        make_local.assert_not_called()
        note.assert_not_called()

    def test_local_primary_never_retries(self):
        primary_err = CodedError(503, "Service Unavailable")
        make_local = mock.Mock()
        note = mock.Mock()
        def primary():
            raise primary_err

        with self.assertRaises(Exception) as ctx:
            model_router.with_local_fallback(
                primary, make_local, note, primary_is_local=True)
        self.assertIs(ctx.exception, primary_err)
        make_local.assert_not_called()
        note.assert_not_called()

    def test_fallback_failure_raises_original(self):
        primary_err = CodedError(503, "Service Unavailable")
        fallback_err = RuntimeError("local blew up")
        notes = []
        def primary():
            raise primary_err

        def make_local():
            def fallback():
                raise fallback_err
            return fallback

        with self.assertRaises(Exception) as ctx:
            model_router.with_local_fallback(
                primary, make_local, notes.append)
        self.assertIs(ctx.exception, primary_err)
        self.assertIs(ctx.exception.__cause__, fallback_err)
        self.assertEqual(notes, [primary_err])

    def test_local_unreachable_raises_original_without_note(self):
        primary_err = CodedError(503, "Service Unavailable")
        build_err = RuntimeError("Ollama unreachable")
        note = mock.Mock()

        def primary():
            raise primary_err

        def make_local():
            raise build_err

        with self.assertRaises(Exception) as ctx:
            model_router.with_local_fallback(
                primary, make_local, note)
        self.assertIs(ctx.exception, primary_err)
        self.assertIs(ctx.exception.__cause__, build_err)
        note.assert_not_called()


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestTrivialTurnFailover(unittest.TestCase):
    """deep_chat trivial path: the whole turn is one model.invoke, so
    the ledger label switches to the leg that served."""

    def _run_trivial(self, tmp, model, seen):
        trace_path = os.path.join(tmp, "trivial.jsonl")

        def fake_record(script, model_name, thread_id, messages,
                        started, extra=None, usage=None, calls=None):
            seen["model"] = model_name
            seen["extra"] = extra
            return {}

        with mock.patch.object(deep_chat.ledger, "record",
                               side_effect=fake_record), \
             mock.patch.object(deep_chat.observe,
                               "default_trace_path",
                               return_value=trace_path):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = deep_chat._cmd_trivial_turn(
                    "arii/remote-server", "t-failover", "hi", 0.0,
                    model, dict(NEBIUS_SPEC))
        with open(trace_path, encoding="utf-8") as f:
            events = [json.loads(line) for line in f if line.strip()]
        return code, out.getvalue(), err.getvalue(), events

    def test_retryable_fails_over_to_local(self):
        import tempfile

        cloud_calls, local_calls = [], []

        class Cloud:
            def invoke(self, messages):
                cloud_calls.append(messages)
                raise CodedError(429, "Rate limit exceeded")

        def local_invoke(messages):
            local_calls.append(messages)
            return SimpleNamespace(content="hey local!",
                                   usage_metadata=None)

        local = SimpleNamespace(invoke=local_invoke)
        seen = {}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                    deep_chat, "build_model",
                    return_value=(local, dict(OLLAMA_SPEC))) as builder:
                code, out, err, events = self._run_trivial(
                    tmp, Cloud(), seen)
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "hey local!")
        builder.assert_called_once_with(prefer="ollama")
        # Same messages on both legs.
        self.assertEqual(len(cloud_calls), 1)
        self.assertEqual(len(local_calls), 1)
        self.assertIs(local_calls[0], cloud_calls[0])
        # Visible note + local leg recorded.
        self.assertIn("retrying locally", err)
        self.assertIn("nebius", err)
        self.assertEqual(seen["model"], "ollama:qwen3:8b")
        failover = seen["extra"]["failover"]
        self.assertEqual(failover["from"], "nebius:n-model")
        self.assertEqual(failover["to"], "ollama:qwen3:8b")
        self.assertIn("Rate limit", failover["error"])
        kinds = [e["type"] for e in events]
        self.assertIn("model_failover", kinds)
        evt = next(e for e in events if e["type"] == "model_failover")
        self.assertEqual(evt["to"], "ollama:qwen3:8b")

    def test_non_retryable_never_builds_local(self):
        import tempfile

        model = SimpleNamespace(invoke=mock.Mock(
            side_effect=CodedError(401, "unauthorized")))
        seen = {}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(deep_chat, "build_model") as builder:
                code, out, err, _ = self._run_trivial(
                    tmp, model, seen)
        self.assertEqual(code, 1)
        self.assertIn("Sorry, the assistant failed", out)
        builder.assert_not_called()
        self.assertNotIn("retrying locally", err)
        self.assertNotIn("failover", seen["extra"])

    def test_local_down_raises_original(self):
        import tempfile

        model = SimpleNamespace(invoke=mock.Mock(
            side_effect=CodedError(503, "503 Service Unavailable")))
        seen = {}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(
                    deep_chat, "build_model",
                    side_effect=RuntimeError("Ollama unreachable")):
                code, out, err, _ = self._run_trivial(
                    tmp, model, seen)
        self.assertEqual(code, 1)
        self.assertIn("503", out)
        self.assertNotIn("Ollama unreachable", out)
        self.assertNotIn("retrying locally", err)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestStreamSalvageFailover(unittest.TestCase):
    """deep_chat._stream salvage pass fails over; the turn label stays
    primary (the agent stream ran there) with a failover annotation."""

    def test_salvage_fails_over_with_spec(self):
        from langchain_core.messages import AIMessage

        class LoopingAgent:
            def stream(self, *a, **k):
                raise GraphRecursionError("limit of 40 reached")

        cloud_calls, local_calls = [], []

        class Cloud:
            def invoke(self, messages):
                cloud_calls.append(messages)
                raise CodedError(503, "Service Unavailable")

        class Local:
            def invoke(self, messages):
                local_calls.append(messages)
                return AIMessage(content="local salvage")

        failover = {}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(
                deep_chat, "build_model",
                return_value=(Local(), dict(OLLAMA_SPEC))):
            with redirect_stdout(out), redirect_stderr(err):
                reply, _, _, salvaged = deep_chat._stream(
                    LoopingAgent(),
                    {"messages": [{"role": "user", "content": "hi"}]},
                    {}, model=Cloud(), spec=dict(NEBIUS_SPEC),
                    failover=failover)
        self.assertTrue(salvaged)
        self.assertEqual(reply, "local salvage")
        self.assertEqual(len(cloud_calls), 1)
        self.assertEqual(len(local_calls), 1)
        self.assertIs(local_calls[0], cloud_calls[0])
        self.assertIn("retrying locally", err.getvalue())
        self.assertEqual(failover["from"], "nebius:n-model")
        self.assertEqual(failover["to"], "ollama:qwen3:8b")

    def test_salvage_without_spec_never_fails_over(self):
        class LoopingAgent:
            def stream(self, *a, **k):
                raise GraphRecursionError("limit of 40 reached")

        class Cloud:
            def invoke(self, messages):
                raise CodedError(503, "Service Unavailable")

        with mock.patch.object(deep_chat, "build_model") as builder:
            with self.assertRaises(GraphRecursionError):
                deep_chat._stream(
                    LoopingAgent(), {"messages": []}, {},
                    model=Cloud())
        builder.assert_not_called()


if __name__ == "__main__":
    unittest.main()
