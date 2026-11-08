"""Delegation-containment guard (#539).

create_deep_agent auto-adds a general-purpose subagent (deepagents
0.7.32 graph.py) that inherits the parent model, tools, and backend
with a full FilesystemMiddleware and no approval gates. That defeats
coder's read-only-until-submit contract (task hop with write_file /
edit_file around the submit scope gate) and adds unrequested metered
delegation to resolver/triage. Every builder must disable the
default: coder/resolver drop task entirely, triage keeps task with
only its deliberate repo_scout type, chat keeps its task exclusion
(#306, locked here as the precedent).

Part A runs under system python with stubbed agent deps: each
builder registers the expected harness-profile gate for its spec and
passes the expected subagents to create_deep_agent.
Part B needs the deep-agent venv and builds each harness for real
(no invoke, no network), asserting the compiled tool surface.
Run: python3 tests/test_delegation_guard.py  (A only under system python)
     deep-agent/.venv/bin/python tests/test_delegation_guard.py  (A + B)
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import coder
import deep_chat

try:
    from deepagents import create_deep_agent  # noqa: F401
    from langchain_ollama import ChatOllama  # noqa: F401
    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def _stub_module(name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    return mod


# Part A recording stubs: create/register calls land here so each
# test can assert its builder's exact gate wiring.
CREATED = []
REGISTERED = []


def _recording_stubs():
    def _tool(func=None, **kwargs):
        def _wrap(g):
            g.description = g.__doc__ or ""
            return g
        return _wrap(func) if func else _wrap

    class _Dummy:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _Permission(_Dummy):
        pass

    def _create(*args, **kwargs):
        CREATED.append((args, kwargs))
        return object()

    def _register(key, profile):
        REGISTERED.append((key, profile))

    return {
        "dotenv": _stub_module(
            "dotenv", load_dotenv=lambda *a, **k: None),
        "langchain_core": _stub_module("langchain_core"),
        "langchain_core.tools": _stub_module(
            "langchain_core.tools", tool=_tool),
        "langchain_core.messages": _stub_module(
            "langchain_core.messages", ToolMessage=_Dummy,
            SystemMessage=_Dummy),
        "langchain_openai": _stub_module(
            "langchain_openai", ChatOpenAI=_Dummy),
        "langchain_ollama": _stub_module(
            "langchain_ollama", ChatOllama=_Dummy),
        "langchain": _stub_module("langchain"),
        "langchain.agents": _stub_module("langchain.agents"),
        "langchain.agents.middleware": _stub_module(
            "langchain.agents.middleware",
            before_model=lambda f: f,
            after_model=lambda f: f,
            wrap_tool_call=lambda f: f),
        "langchain.agents.middleware.types": _stub_module(
            "langchain.agents.middleware.types",
            AgentMiddleware=object),
        "langgraph": _stub_module("langgraph"),
        "langgraph.checkpoint": _stub_module("langgraph.checkpoint"),
        "langgraph.checkpoint.memory": _stub_module(
            "langgraph.checkpoint.memory", MemorySaver=_Dummy),
        "deepagents": _stub_module(
            "deepagents",
            create_deep_agent=_create,
            HarnessProfileConfig=_Dummy,
            GeneralPurposeSubagentProfile=_Dummy,
            register_harness_profile=_register),
        "deepagents.backends": _stub_module(
            "deepagents.backends", FilesystemBackend=_Dummy),
        "deepagents.middleware": _stub_module("deepagents.middleware"),
        "deepagents.middleware.filesystem": _stub_module(
            "deepagents.middleware.filesystem",
            FilesystemPermission=_Permission),
    }


def _import_maybe_stubbed(name):
    """Real import when deps exist, else stub-import (system python).

    Part A patches builder attrs + sys.modules either way; Part B
    needs the real binding, so prefer it whenever importable. A
    stub-bound module is unhooked from sys.modules after import
    (the object stays usable via the returned reference) so
    venv-gated files importing the same name still see a clean
    ImportError under discover.
    """
    try:
        return __import__(name)
    except ImportError:
        pass
    stubs = _recording_stubs()
    sys.modules.update(stubs)
    try:
        return __import__(name)
    finally:
        for _key in stubs:
            sys.modules.pop(_key, None)
        sys.modules.pop(name, None)


rc = _import_maybe_stubbed("resolve_conflicts")
tf = _import_maybe_stubbed("triage_fanout")


def _record_create(*args, **kwargs):
    CREATED.append((args, kwargs))
    return object()


def _gp_disabled(profile):
    """True when a stub profile carries enabled=False GP config."""
    sub = profile.kwargs.get("general_purpose_subagent")
    return (sub is not None
            and sub.kwargs.get("enabled") is False)


class _StubbedCase(unittest.TestCase):
    def setUp(self):
        del CREATED[:]
        del REGISTERED[:]
        patcher = mock.patch.dict(sys.modules, _recording_stubs())
        patcher.start()
        self.addCleanup(patcher.stop)


class TestCoderImplementGate(_StubbedCase):
    def test_registers_gp_disable_for_spec_key(self):
        with tempfile.TemporaryDirectory() as d:
            coder.build_agent(
                d, coder.build_system(d, ""), model=object(),
                spec={"provider": "ollama", "model": "q8"})
        self.assertEqual(len(REGISTERED), 1)
        key, profile = REGISTERED[0]
        self.assertEqual(key, "ollama:q8")
        self.assertTrue(_gp_disabled(profile))
        self.assertEqual(len(CREATED), 1)

    def test_nebius_spec_translates_to_openai_key(self):
        with tempfile.TemporaryDirectory() as d:
            coder.build_agent(
                d, "SYS", model=object(),
                spec={"provider": "nebius", "model": "nv"})
        self.assertEqual(REGISTERED[0][0], "openai:nv")
        self.assertTrue(_gp_disabled(REGISTERED[0][1]))

    def test_tools_still_just_run_check(self):
        with tempfile.TemporaryDirectory() as d:
            coder.build_agent(
                d, "SYS", model=object(),
                spec={"provider": "ollama", "model": "q8"})
        _args, kwargs = CREATED[0]
        self.assertEqual(
            [getattr(t, "__name__", "") for t in kwargs["tools"]],
            ["run_check"])
        self.assertNotIn("subagents", kwargs)


class TestCoderPlanGate(_StubbedCase):
    def test_plan_agent_gated_with_writes_denied(self):
        with tempfile.TemporaryDirectory() as d:
            coder.build_plan_agent(
                d, "SYS", model=object(),
                spec={"provider": "ollama", "model": "q8"})
        self.assertEqual(len(REGISTERED), 1)
        self.assertEqual(REGISTERED[0][0], "ollama:q8")
        self.assertTrue(_gp_disabled(REGISTERED[0][1]))
        _args, kwargs = CREATED[0]
        self.assertIn("propose_plan", kwargs["interrupt_on"])
        self.assertEqual(len(kwargs["permissions"]), 1)
        self.assertEqual(
            [getattr(t, "__name__", "") for t in kwargs["tools"]],
            ["propose_plan"])


class TestResolverGate(_StubbedCase):
    def test_registers_gp_disable_and_passes_no_subagents(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(
                    rc, "create_deep_agent", _record_create):
                rc.build_agent(
                    d, object(),
                    {"provider": "nebius", "model": "nv-flash"})
        self.assertEqual(len(REGISTERED), 1)
        key, profile = REGISTERED[0]
        self.assertEqual(key, "openai:nv-flash")
        self.assertTrue(_gp_disabled(profile))
        self.assertEqual(len(CREATED), 1)
        _args, kwargs = CREATED[0]
        self.assertNotIn("subagents", kwargs)
        self.assertNotIn("middleware", kwargs)
        self.assertEqual(len(kwargs["tools"]), 4)

    def test_main_wires_spec_through_to_builder(self):
        from contextlib import redirect_stdout
        from types import SimpleNamespace
        import io

        seen = {}

        def _spy(worktree, model, spec, recorder=None):
            seen["spec"] = spec
            seen["worktree"] = worktree
            agent = mock.Mock()
            agent.invoke.return_value = {"messages": [
                SimpleNamespace(content="done")]}
            return agent

        with tempfile.TemporaryDirectory() as d:
            worktree = os.path.join(d, "wt")
            os.makedirs(os.path.join(worktree, ".git"))
            argv = ["resolve_conflicts.py", worktree]
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ,
                                    {"NEBIUS_API_KEY": "k"}, clear=True), \
                    mock.patch.object(
                        rc.model_router, "resolve_llm",
                        return_value={"provider": "nebius",
                                      "model": "x",
                                      "base_url": "y"}), \
                    mock.patch.object(rc, "build_agent",
                                      side_effect=_spy), \
                    mock.patch.object(rc.ledger, "record"), \
                    mock.patch.object(
                        rc.observe, "default_trace_path",
                        return_value=os.path.join(d, "t.jsonl")), \
                    redirect_stdout(io.StringIO()):
                self.assertEqual(rc.main(), 0)
        self.assertEqual(seen["spec"]["provider"], "nebius")
        self.assertIn("model", seen["spec"])
        self.assertEqual(seen["worktree"], worktree)


class TestTriageGate(_StubbedCase):
    def test_scout_kept_gp_disabled(self):
        with mock.patch.object(tf, "create_deep_agent", _record_create):
            tf.build_agent(
                object(), ["arii/boomtick", "arii/tech-dancer"],
                {"provider": "nebius", "model": "nv-flash"})
        self.assertEqual(len(REGISTERED), 1)
        key, profile = REGISTERED[0]
        self.assertEqual(key, "openai:nv-flash")
        self.assertTrue(_gp_disabled(profile))
        self.assertEqual(len(CREATED), 1)
        _args, kwargs = CREATED[0]
        subs = kwargs["subagents"]
        self.assertEqual([s["name"] for s in subs], ["repo_scout"])
        self.assertIn("arii/boomtick", kwargs["system_prompt"])
        self.assertIn("arii/tech-dancer", kwargs["system_prompt"])

    def test_main_wires_spec_through_to_builder(self):
        from contextlib import redirect_stderr, redirect_stdout
        from types import SimpleNamespace
        import io

        seen = {}

        def _spy(model, repos, spec, recorder=None):
            seen["spec"] = spec
            seen["repos"] = repos
            agent = mock.Mock()
            agent.invoke.return_value = {"messages": [
                SimpleNamespace(content="ok")]}
            return agent

        argv = ["triage_fanout.py", "arii/boomtick"]
        with tempfile.TemporaryDirectory() as d:
            env = {"NEBIUS_API_KEY": "k",
                   "DEEP_AGENT_STATE_DIR": d}
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(
                        tf.model_router, "resolve_llm",
                        return_value={"provider": "nebius",
                                      "model": "x",
                                      "base_url": "y"}), \
                    mock.patch.object(tf, "build_agent",
                                      side_effect=_spy), \
                    mock.patch.object(tf.ledger, "record"), \
                    mock.patch.object(
                        tf.observe, "default_trace_path",
                        return_value=os.path.join(d, "t.jsonl")), \
                    redirect_stdout(io.StringIO()), \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(tf.main(), 0)
        self.assertEqual(seen["spec"]["provider"], "nebius")
        self.assertEqual(seen["repos"], ["arii/boomtick"])


class TestChatGateLocked(_StubbedCase):
    def test_task_exclusion_precedent_unchanged(self):
        # build_agent imports create_deep_agent lazily, so the
        # sys.modules stub records it; only build_tools needs a patch
        # (real one needs langchain).
        with mock.patch.object(
                deep_chat, "build_tools", return_value=["T1"]):
            deep_chat.build_agent(
                "arii/boomtick", "CHECKPOINTER", model=object(),
                spec={"provider": "nebius", "model": "nv"})
        self.assertEqual(len(REGISTERED), 1)
        key, profile = REGISTERED[0]
        self.assertEqual(key, "openai:nv")
        self.assertEqual(profile.kwargs.get("excluded_tools"), {"task"})
        self.assertEqual(len(CREATED), 1)
        _args, kwargs = CREATED[0]
        self.assertEqual(kwargs["tools"], ["T1"])
        self.assertIn("propose_action", kwargs["interrupt_on"])
        self.assertEqual(len(kwargs["permissions"]), 1)


def _tool_surface(agent):
    """Compiled tool surface: {name: tool} off the tools node.

    Pinned-stack introspection (deepagents 0.7.32 over langgraph):
    the harness assembles every model-visible tool, including the
    middleware-injected task tool, into the tools ToolNode.
    """
    return agent.nodes["tools"].bound.tools_by_name


def _ollama_probe(model_id):
    from langchain_ollama import ChatOllama

    return ChatOllama(model=model_id)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestRealSurfaces(unittest.TestCase):
    """Build each harness for real (no invoke, no network).

    Distinct probe model per builder: profile registrations are
    process-global, so unique keys keep the tests isolated.
    """

    def test_coder_implement_has_no_task(self):
        with tempfile.TemporaryDirectory() as d:
            agent = coder.build_agent(
                d, coder.build_system(d, ""),
                model=_ollama_probe("guard-coder"),
                spec={"provider": "ollama", "model": "guard-coder"})
        self.assertNotIn("task", _tool_surface(agent))

    def test_coder_plan_has_no_task(self):
        with tempfile.TemporaryDirectory() as d:
            agent = coder.build_plan_agent(
                d, "SYS", model=_ollama_probe("guard-plan"),
                spec={"provider": "ollama", "model": "guard-plan"})
        self.assertNotIn("task", _tool_surface(agent))

    def test_resolver_has_no_task(self):
        with tempfile.TemporaryDirectory() as d:
            agent = rc.build_agent(
                d, _ollama_probe("guard-resolver"),
                {"provider": "ollama", "model": "guard-resolver"})
        self.assertNotIn("task", _tool_surface(agent))

    def test_triage_task_lists_only_scout(self):
        agent = tf.build_agent(
            _ollama_probe("guard-triage"), ["arii/boomtick"],
            {"provider": "ollama", "model": "guard-triage"})
        surface = _tool_surface(agent)
        self.assertIn("task", surface)
        # List-item form: the static usage notes below still name
        # general-purpose in prose ("when only general-purpose is
        # available ..."), which is fine — it must not be offered.
        desc = surface["task"].description
        self.assertIn("- repo_scout:", desc)
        self.assertNotIn("- general-purpose:", desc)

    def test_chat_binds_no_task(self):
        # Chat gates via task exclusion, which filters at the model
        # request (the executor still registers task by design), so
        # this leg asserts the model-visible layer: one canned turn
        # through a capturing ChatOllama (no network).
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import (
            ChatGeneration, ChatResult)
        from langchain_ollama import ChatOllama

        seen = {}

        class _CapOllama(ChatOllama):
            def bind_tools(self, tools, **kwargs):
                seen["names"] = [
                    getattr(t, "name", None) for t in tools]
                return self

            def _generate(self, messages, stop=None,
                          run_manager=None, **kwargs):
                return ChatResult(generations=[ChatGeneration(
                    message=AIMessage(content="canned"))])

        agent = deep_chat.build_agent(
            "arii/tech-dancer", None,
            model=_CapOllama(model="guard-chat-bind"),
            spec={"provider": "ollama", "model": "guard-chat-bind"})
        agent.invoke({"messages": [{"role": "user",
                                    "content": "hi"}]})
        self.assertIn("propose_action", seen["names"])
        self.assertNotIn("task", seen["names"])


if __name__ == "__main__":
    unittest.main()
