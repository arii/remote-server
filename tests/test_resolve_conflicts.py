"""Resolver metering, bounds, and tracing (#525) + prefer selection (#647).

Stub-imports resolve_conflicts (heavy agent deps faked; real
cli/ledger/observe/model_router) and drives main() with a stubbed
model. Stubs are scoped to this module's import only.
--prefer=ollama|nebius|auto selects the backend (mirrors #622
fanout); auto (default) uses Nebius when keyed, else the local
model with a stderr note.
"""
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
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


# Resolver MODEL_ID is read at import from ambient NEBIUS_MODEL and is
# blank-intolerant (the :28 twin, reported separately — not this
# issue). Pin it in the prefer harness so selection tests don't
# depend on ambient env.
PIN = "nvidia/Nemotron-3_5-Lightning"


def _ollama_stub(calls):
    """Fake langchain_ollama module recording ChatOllama kwargs."""

    class _ChatOllama:
        def __init__(self, *args, **kwargs):
            calls.append(kwargs)

    return _stub_module("langchain_ollama", ChatOllama=_ChatOllama)


def _run_prefer(args, env, spec):
    """Drive rc.main() with a canned resolve spec.

    '<WT>' in args is replaced with a fresh worktree path (with
    .git). Returns (code, stdout, stderr, resolve_mock, chat_cls,
    ollama_calls, record_mock).
    """
    agent = _StubAgent(
        behavior=lambda: {"messages": [_FakeMsg("done")]})
    ollama_calls = []
    out, err = io.StringIO(), io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        worktree = os.path.join(d, "wt")
        os.makedirs(os.path.join(worktree, ".git"))
        argv = ["resolve_conflicts.py"] + [
            worktree if a == "<WT>" else a for a in args]
        mods = _runtime_stubs()
        mods["langchain_ollama"] = _ollama_stub(ollama_calls)
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.dict(sys.modules, mods), \
                mock.patch.object(
                    rc, "create_deep_agent", return_value=agent), \
                mock.patch.object(rc, "ChatOpenAI") as chat_cls, \
                mock.patch.object(rc, "MODEL_ID", PIN), \
                mock.patch.object(
                    rc.model_router, "resolve_llm",
                    return_value=spec) as resolve_mock, \
                mock.patch.object(ledger, "record") as record_mock, \
                mock.patch.object(
                    observe, "default_trace_path",
                    return_value=os.path.join(d, "trace.jsonl")), \
                redirect_stdout(out), redirect_stderr(err):
            code = rc.main()
    return (code, out.getvalue(), err.getvalue(), resolve_mock,
            chat_cls, ollama_calls, record_mock)


class TestPreferSelection(unittest.TestCase):
    LOCAL_SPEC = {"provider": "ollama", "model": "qwen3:8b",
                  "base_url": "http://ollama:11434"}
    CLOUD_SPEC = {"provider": "nebius", "model": "x", "base_url": "y"}
    NOTE = "resolve: no Nebius key — resolve on local model (weaker)\n"
    PIN = "nvidia/Nemotron-3_5-Lightning"
    STUDIO = "https://api.studio.nebius.com/v1"

    def test_extract_prefer_defaults_to_auto(self):
        self.assertEqual(
            rc.extract_prefer(["resolve_conflicts.py"]), "auto")
        self.assertEqual(
            rc.extract_prefer(
                ["resolve_conflicts.py", "/tmp/wt"]),
            "auto")

    def test_extract_prefer_explicit_honored(self):
        for value in ("ollama", "nebius", "auto"):
            self.assertEqual(
                rc.extract_prefer(
                    ["resolve_conflicts.py", f"--prefer={value}"]),
                value)

    def test_extract_prefer_normalizes_case_and_space(self):
        self.assertEqual(
            rc.extract_prefer(
                ["resolve_conflicts.py", "--prefer= Nebius "]),
            "nebius")

    def test_extract_prefer_invalid_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            rc.extract_prefer(
                ["resolve_conflicts.py", "--prefer=bogus"])
        self.assertIn("bad --prefer='bogus'", str(ctx.exception))

    def test_keyed_auto_uses_nebius_pin(self):
        (code, _, stderr, resolve_mock, chat_cls, ollama_calls,
         record_mock) = _run_prefer(
            ["<WT>"], {"NEBIUS_API_KEY": "dummy"},
            dict(self.CLOUD_SPEC))
        self.assertEqual(code, 0)
        resolve_mock.assert_called_once_with(prefer="nebius")
        self.assertEqual(chat_cls.call_args.kwargs["model"], self.PIN)
        self.assertEqual(
            chat_cls.call_args.kwargs["base_url"], self.STUDIO)
        self.assertEqual(ollama_calls, [])
        self.assertNotIn("no Nebius key", stderr)
        self.assertEqual(
            record_mock.call_args.args[1], f"nebius:{self.PIN}")
        self.assertEqual(
            record_mock.call_args.kwargs["extra"]["base"],
            "origin/main")

    def test_keyless_auto_uses_local_spec_with_note(self):
        (code, _, stderr, resolve_mock, chat_cls, ollama_calls,
         record_mock) = _run_prefer(
            ["<WT>"], {}, dict(self.LOCAL_SPEC))
        self.assertEqual(code, 0)
        resolve_mock.assert_called_once_with(prefer=None)
        chat_cls.assert_not_called()
        self.assertEqual(len(ollama_calls), 1)
        self.assertEqual(ollama_calls[0]["model"], "qwen3:8b")
        self.assertEqual(
            ollama_calls[0]["base_url"], "http://ollama:11434")
        self.assertEqual(
            ollama_calls[0]["model_kwargs"], {"think": False})
        self.assertIn(self.NOTE, stderr)
        self.assertEqual(
            record_mock.call_args.args[1], "ollama:qwen3:8b")

    def test_explicit_prefer_ollama_honored_without_note(self):
        (code, _, stderr, resolve_mock, chat_cls, ollama_calls,
         _) = _run_prefer(
            ["--prefer=ollama", "<WT>"],
            {"NEBIUS_API_KEY": "dummy"}, dict(self.LOCAL_SPEC))
        self.assertEqual(code, 0)
        resolve_mock.assert_called_once_with(prefer="ollama")
        self.assertEqual(len(ollama_calls), 1)
        chat_cls.assert_not_called()
        self.assertNotIn("no Nebius key", stderr)

    def test_prefer_flag_does_not_shift_positionals(self):
        (code, _, _, _, _, _, record_mock) = _run_prefer(
            ["<WT>", "origin/dev", "--prefer=ollama"],
            {"NEBIUS_API_KEY": "dummy"}, dict(self.LOCAL_SPEC))
        self.assertEqual(code, 0)
        extra = record_mock.call_args.kwargs["extra"]
        self.assertEqual(extra["base"], "origin/dev")
        self.assertEqual(os.path.basename(extra["worktree"]), "wt")

    def test_resolver_think_opt_in_skips_no_think(self):
        (_, _, _, _, _, ollama_calls, _) = _run_prefer(
            ["--prefer=ollama", "<WT>"],
            {"NEBIUS_API_KEY": "dummy", "RESOLVER_THINK": "1"},
            dict(self.LOCAL_SPEC))
        self.assertEqual(len(ollama_calls), 1)
        self.assertNotIn("model_kwargs", ollama_calls[0])

    def test_prefer_nebius_keyless_refuses(self):
        (code, stdout, stderr, _, _, _, _) = _run_prefer(
            ["--prefer=nebius", "<WT>"], {}, dict(self.CLOUD_SPEC))
        self.assertEqual(code, 1)
        self.assertEqual(stderr, "NEBIUS_API_KEY is not set\n")
        self.assertEqual(stdout, "")

    def test_prefer_bogus_is_usage_error(self):
        (code, _, stderr, _, _, _, _) = _run_prefer(
            ["--prefer=bogus", "<WT>"],
            {"NEBIUS_API_KEY": "dummy"}, dict(self.CLOUD_SPEC))
        self.assertEqual(code, 2)
        self.assertIn("bad --prefer='bogus'", stderr)

    def test_flag_only_is_usage_error(self):
        (code, _, stderr, _, _, _, _) = _run_prefer(
            ["--prefer=ollama"], {"NEBIUS_API_KEY": "dummy"},
            dict(self.LOCAL_SPEC))
        self.assertEqual(code, 2)
        self.assertIn("usage:", stderr)
