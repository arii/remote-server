"""Resolver metering, bounds, and tracing (#525).

Stub-imports resolve_conflicts (heavy agent deps faked; real
cli/ledger/observe/model_router) and drives main() with a stubbed
model. Stubs are scoped to this module's import only.
"""
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))


def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


def _install_stubs():
    def _tool(func=None, **kwargs):
        return func if func else (lambda g: g)

    class _ChatOpenAI:
        def __init__(self, *args, **kwargs):
            self.kwargs = kwargs

    class _Dummy:
        def __init__(self, *args, **kwargs):
            pass

    langchain = _stub_module("langchain")
    agents_pkg = _stub_module("langchain.agents")
    middleware = _stub_module(
        "langchain.agents.middleware",
        before_model=lambda f: f,
        after_model=lambda f: f,
        wrap_tool_call=lambda f: f,
    )
    return {
        "dotenv": _stub_module(
            "dotenv", load_dotenv=lambda *a, **k: None),
        "langchain_core": _stub_module("langchain_core"),
        "langchain_core.tools": _stub_module(
            "langchain_core.tools", tool=_tool),
        "langchain_openai": _stub_module(
            "langchain_openai", ChatOpenAI=_ChatOpenAI),
        "langgraph": _stub_module("langgraph"),
        "langgraph.checkpoint": _stub_module("langgraph.checkpoint"),
        "langgraph.checkpoint.memory": _stub_module(
            "langgraph.checkpoint.memory", MemorySaver=_Dummy),
        "deepagents": _stub_module(
            "deepagents",
            create_deep_agent=lambda *a, **k: None),
        "deepagents.backends": _stub_module(
            "deepagents.backends", FilesystemBackend=_Dummy),
        "langchain": langchain,
        "langchain.agents": agents_pkg,
        "langchain.agents.middleware": middleware,
    }


# Manual sys.modules management: mock.patch.dict restores (and drops)
# every real module imported inside its block, which would detach
# resolve_conflicts' ledger/observe from the patched copies.
_STUBS = _install_stubs()
sys.modules.update(_STUBS)
try:
    import resolve_conflicts as rc
finally:
    for _key in _STUBS:
        sys.modules.pop(_key, None)

import ledger
import observe


def _runtime_stubs():
    """langchain middleware stubs, needed when main() runs."""
    full = _install_stubs()
    return {k: full[k] for k in (
        "langchain", "langchain.agents",
        "langchain.agents.middleware")}


class _FakeMsg:
    def __init__(self, content):
        self.content = content


class _StubAgent:
    def __init__(self, behavior):
        self.behavior = behavior
        self.configs = []

    def invoke(self, messages, config=None):
        self.configs.append(config)
        return self.behavior()


def _run_main(worktree, env, agent):
    argv = ["resolve_conflicts.py", worktree]
    with mock.patch.object(sys, "argv", argv):
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.dict(sys.modules, _runtime_stubs()):
                with mock.patch.object(
                        rc, "create_deep_agent",
                        return_value=agent):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        code = rc.main()
    return code, buf.getvalue()


class TestResolverMetering(unittest.TestCase):
    def _worktree(self, directory):
        worktree = os.path.join(directory, "wt")
        os.makedirs(os.path.join(worktree, ".git"))
        return worktree

    def test_hung_invoke_terminates_and_ledgers(self):
        gate = threading.Event()
        agent = _StubAgent(behavior=lambda: gate.wait(60))
        with tempfile.TemporaryDirectory() as d:
            worktree = self._worktree(d)
            ledger_path = os.path.join(d, "ledger.jsonl")
            trace_path = os.path.join(d, "trace.jsonl")
            env = {"NEBIUS_API_KEY": "k"}
            outcome = {}
            with mock.patch.object(
                    ledger, "LEDGER", ledger_path):
                with mock.patch.object(
                        observe, "default_trace_path",
                        return_value=trace_path):
                    with mock.patch.object(
                            rc, "_timeout", return_value=1,
                            create=True):
                        def _target():
                            outcome["rc"], _ = _run_main(
                                worktree, env, agent)
                        worker = threading.Thread(
                            target=_target, daemon=True)
                        worker.start()
                        worker.join(15)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcome["rc"], 1)
            with open(ledger_path, encoding="utf-8") as f:
                row = json.loads(f.read().strip().splitlines()[-1])
            self.assertIn("timed out", row["extra"]["error"])

    def test_success_records_usage_trace_and_bounds(self):
        usage = {"input_tokens": 100, "output_tokens": 50,
                 "total_tokens": 150}
        calls = [{"name": "git_status", "args": ""}]
        agent = _StubAgent(
            behavior=lambda: {"messages": [_FakeMsg("done")]})
        with tempfile.TemporaryDirectory() as d:
            worktree = self._worktree(d)
            ledger_path = os.path.join(d, "ledger.jsonl")
            trace_path = os.path.join(d, "trace.jsonl")
            env = {"NEBIUS_API_KEY": "k"}
            with mock.patch.object(ledger, "LEDGER", ledger_path):
                with mock.patch.object(
                        observe, "default_trace_path",
                        return_value=trace_path):
                    with mock.patch.object(
                            observe.TraceRecorder, "totals",
                            return_value=(usage, calls)):
                        with mock.patch.object(
                                rc, "MODEL_ID",
                                "zai-org/GLM-5.3-Flash"):
                            code, out = _run_main(
                                worktree, env, agent)
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "done")
            self.assertEqual(
                agent.configs[0]["recursion_limit"], 100)
            with open(ledger_path, encoding="utf-8") as f:
                row = json.loads(f.read().strip().splitlines()[-1])
            self.assertEqual(row["usage"], usage)
            self.assertEqual(row["tool_calls"], calls)
            self.assertTrue(row["metered"])
            self.assertGreater(row["cost_usd"], 0)
            self.assertEqual(row["extra"]["trace"], trace_path)
            with open(trace_path, encoding="utf-8") as f:
                trace = f.read()
            self.assertIn("run_start", trace)
            self.assertIn("run_end", trace)

    def test_cap_zero_refuses_before_model(self):
        agent = _StubAgent(
            behavior=lambda: {"messages": [_FakeMsg("done")]})
        with tempfile.TemporaryDirectory() as d:
            worktree = self._worktree(d)
            env = {"NEBIUS_API_KEY": "k",
                   "NEBIUS_SPEND_CAP_USD": "0"}
            with mock.patch.object(
                    rc, "create_deep_agent") as factory:
                code, _ = _run_main(worktree, env, agent)
        self.assertEqual(code, 1)
        factory.assert_not_called()

    def test_timeout_default_and_clamp(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rc._timeout(), 1800)
        with mock.patch.dict(
                os.environ, {"RESOLVER_TIMEOUT": "5"}, clear=True):
            self.assertEqual(rc._timeout(), 60)
        with mock.patch.dict(
                os.environ, {"RESOLVER_TIMEOUT": "3600"},
                clear=True):
            self.assertEqual(rc._timeout(), 3600)
        with mock.patch.dict(
                os.environ, {"RESOLVER_TIMEOUT": "soon"},
                clear=True):
            self.assertEqual(rc._timeout(), 1800)
