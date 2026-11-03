"""deep_chat slice tests. No live LLM: Part A runs under system python
(stdlib-only import); Part B needs the deep-agent venv and uses a stub
chat model plus real middleware/checkpointer mechanics.
Run: python3 tests/test_deep_chat.py  (A only under system python)
     deep-agent/.venv/bin/python tests/test_deep_chat.py  (A + B)"""
import base64
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import deep_chat

try:
    import langchain_core  # noqa: F401
    from deepagents import create_deep_agent  # noqa: F401
    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def b64(text):
    return base64.b64encode(text.encode()).decode()


class TestProposalValidation(unittest.TestCase):
    def test_issue_ok(self):
        ok, text = deep_chat.validate_proposal(
            {"kind": "issue", "title": "Fix it", "body": "details"})
        self.assertTrue(ok)
        self.assertIn("PROPOSE issue", text)
        self.assertIn("TITLE: Fix it", text)
        self.assertIn("END PROPOSE", text)

    def test_issue_rejects(self):
        for bad in (
            {"kind": "issue", "title": "", "body": "b"},
            {"kind": "issue", "title": "x" * 121, "body": "b"},
            {"kind": "issue", "title": "t", "body": ""},
            {"kind": "issue", "title": "t", "body": "x" * 4001},
            {"kind": "issue", "title": "t"},  # missing body
            {"kind": "issue", "title": 42, "body": "b"},
        ):
            ok, text = deep_chat.validate_proposal(bad)
            self.assertFalse(ok, bad)
            self.assertIn("REJECTED", text)

    def test_bad_kind(self):
        for bad in ("pr", "", None, 42):
            ok, text = deep_chat.validate_proposal({"kind": bad})
            self.assertFalse(ok)
            self.assertIn("REJECTED", text)
        ok, _ = deep_chat.validate_proposal(["not", "a", "dict"])
        self.assertFalse(ok)

    def test_td_exact_shapes(self):
        for cmd in ("td context-warm", "td --json preflight",
                    "td --json agent list-sessions",
                    "td --json jules list-sessions",
                    "td --json agent run-feedback-check"):
            ok, text = deep_chat.validate_proposal(
                {"kind": "td", "command": cmd})
            self.assertTrue(ok, cmd)
            self.assertIn(f"COMMAND: {cmd}", text)

    def test_td_n_to_digits(self):
        ok, text = deep_chat.validate_proposal(
            {"kind": "td",
             "command": "td --json agent fix-ci --pr 153 --execute"})
        self.assertTrue(ok)
        self.assertIn("--pr 153 --execute", text)
        ok, _ = deep_chat.validate_proposal(
            {"kind": "td", "command": "td gh read-pr-reviews --pr 7"})
        self.assertTrue(ok)

    def test_td_rejects(self):
        for bad in (
            "td --json agent fix-ci --pr 0 --execute",  # N must be > 0
            "td --json agent fix-ci --pr abc --execute",
            "td --json agent fix-ci --pr 15a --execute",
            "td gh read-pr-reviews",  # missing N
            "td rm -rf /",  # invented subcommand
            "td --json preflight --force",  # invented flag
            "td context-warm extra",  # extra arg
            "sudo td context-warm",  # prefix smuggling
            "td context-warm; rm x",  # shell smuggling
            "",
            "x" * 301,
            None,
            42,
        ):
            ok, text = deep_chat.validate_proposal(
                {"kind": "td", "command": bad})
            self.assertFalse(ok, bad)
            self.assertIn("REJECTED", text)

    def test_td_whitespace_normalized(self):
        ok, text = deep_chat.validate_proposal(
            {"kind": "td", "command": "td   --json\tpreflight "})
        self.assertTrue(ok)
        self.assertIn("COMMAND: td --json preflight", text)

    def test_dispatch_ok(self):
        ok, text = deep_chat.validate_proposal(
            {"kind": "dispatch", "repo": "arii/boomtick",
             "branch": "main", "task": "Fix the typo."})
        self.assertTrue(ok)
        self.assertIn("PROPOSE dispatch", text)
        self.assertIn("REPO: arii/boomtick", text)
        self.assertIn("BRANCH: main", text)
        self.assertIn("TASK:\nFix the typo.", text)
        self.assertIn("END PROPOSE", text)

    def test_dispatch_rejects(self):
        good = {"kind": "dispatch", "repo": "arii/boomtick",
                "branch": "fix/x-1.0", "task": "Do it."}
        ok, _ = deep_chat.validate_proposal(good)
        self.assertTrue(ok)
        for field, bad in (
            ("repo", "noslash"),
            ("repo", "a/b/c"),
            ("repo", ""),
            ("repo", 42),
            ("branch", ""),
            ("branch", "has space"),
            ("branch", "dot..dot"),
            ("branch", "/leading"),
            ("branch", "-leading"),
            ("branch", "x" * 121),
            ("branch", None),
            ("task", ""),
            ("task", "   "),
            ("task", "x" * 4001),
            ("task", 42),
        ):
            payload = dict(good, **{field: bad})
            ok, text = deep_chat.validate_proposal(payload)
            self.assertFalse(ok, (field, bad))
            self.assertIn("REJECTED", text)


class TestRecursionBudget(unittest.TestCase):
    def test_room_for_multi_tool_turns(self):
        # ~4 graph steps per tool call (measured); 40 covers ~9 calls.
        self.assertEqual(deep_chat.RECURSION_LIMIT, 40)


class TestVerdictMapping(unittest.TestCase):
    def test_approve(self):
        self.assertEqual(deep_chat.decision_for_verdict("approve"),
                         {"type": "approve"})

    def test_reject_default_message(self):
        d = deep_chat.decision_for_verdict("reject")
        self.assertEqual(d["type"], "reject")
        self.assertTrue(d["message"])

    def test_reject_custom_message(self):
        d = deep_chat.decision_for_verdict("reject", "not now")
        self.assertEqual(d, {"type": "reject", "message": "not now"})

    def test_reject_long_message_capped(self):
        d = deep_chat.decision_for_verdict("reject", "x" * 900)
        self.assertEqual(len(d["message"]), 500)

    def test_bad_verdict(self):
        for bad in ("Approve", "yes", "", None, True):
            with self.assertRaises(ValueError, msg=f"verdict={bad!r}"):
                deep_chat.decision_for_verdict(bad)

    def test_resume_payload(self):
        p = deep_chat.resume_payload([{"type": "approve"}])
        self.assertEqual(p, {"decisions": [{"type": "approve"}]})
        for bad in ([], "x", [{"type": "bogus"}], [{"nope": 1}], [None]):
            with self.assertRaises(ValueError, msg=f"payload={bad!r}"):
                deep_chat.resume_payload(bad)


class TestThreadIds(unittest.TestCase):
    def test_ok(self):
        for tid in ("abc123", "a" * 64, "a-b_c"):
            self.assertTrue(deep_chat.valid_thread_id(tid), tid)

    def test_bad(self):
        for tid in ("", "a" * 65, "bad id!", "a/b", None, 42):
            self.assertFalse(deep_chat.valid_thread_id(tid), repr(tid))


class TestInterruptContract(unittest.TestCase):
    def test_interrupt_on_is_propose_only_approve_reject(self):
        self.assertEqual(set(deep_chat.INTERRUPT_ON), {"propose_action"})
        cfg = deep_chat.INTERRUPT_ON["propose_action"]
        self.assertEqual(cfg["allowed_decisions"], ["approve", "reject"])

    def test_emit_interrupt_line_and_exit(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = deep_chat._emit_interrupt(
                "t1", [{"id": "i1", "value": {"action_requests": []}}])
        self.assertEqual(code, 3)
        line = buf.getvalue().strip()
        self.assertTrue(line.startswith("INTERRUPT: "))
        env = json.loads(line[len("INTERRUPT: "):])
        self.assertEqual(env["thread_id"], "t1")
        self.assertEqual(env["interrupts"][0]["id"], "i1")


class TestCliValidation(unittest.TestCase):
    SCRIPT = os.path.join(
        os.path.dirname(__file__), "..", "deep-agent", "deep_chat.py")

    def run_script(self, *args):
        return subprocess.run(
            [sys.executable, self.SCRIPT, *args],
            capture_output=True, text=True, timeout=120,
        )

    def test_bad_repo(self):
        p = self.run_script("nope", "t1", b64("hi"))
        self.assertEqual(p.returncode, 2)

    def test_bad_thread(self):
        p = self.run_script("arii/remote-server", "bad id!", b64("hi"))
        self.assertEqual(p.returncode, 2)
        self.assertIn("thread-id", p.stderr)

    def test_bad_prompt(self):
        p = self.run_script("arii/remote-server", "t1", "!!!")
        self.assertEqual(p.returncode, 2)

    def test_empty_prompt(self):
        p = self.run_script("arii/remote-server", "t1", b64("   "))
        self.assertEqual(p.returncode, 2)

    def test_resume_needs_verdict(self):
        p = self.run_script("arii/remote-server", "t1", "--resume")
        self.assertEqual(p.returncode, 2)

    def test_resume_bad_verdict(self):
        p = self.run_script("arii/remote-server", "t1", "--resume", "maybe")
        self.assertEqual(p.returncode, 2)

    def test_validate_proposal_ok(self):
        p = self.run_script(
            "--validate-proposal",
            b64(json.dumps({"kind": "td",
                            "command": "td --json preflight"})))
        self.assertEqual(p.returncode, 0, p.stderr)
        body = json.loads(p.stdout)
        self.assertTrue(body["ok"])
        self.assertIn("PROPOSE td", body["text"])

    def test_validate_proposal_rejected(self):
        p = self.run_script(
            "--validate-proposal",
            b64(json.dumps({"kind": "td", "command": "td rm -rf /"})))
        self.assertEqual(p.returncode, 0, p.stderr)
        body = json.loads(p.stdout)
        self.assertFalse(body["ok"])
        self.assertIn("REJECTED", body["text"])

    def test_validate_proposal_bad_input(self):
        p = self.run_script("--validate-proposal", "!!!")
        self.assertEqual(p.returncode, 2)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestToolSurface(unittest.TestCase):
    def test_five_tools(self):
        tools = deep_chat.build_tools("/tmp")
        self.assertEqual(len(tools), 5)
        self.assertEqual(
            {t.name for t in tools},
            {"read_repo_file", "grep_repo", "list_dir", "search_docs",
             "propose_action"},
        )

    def test_read_grep_list_delegate(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.txt"), "w") as f:
                f.write("hello needle\n")
            by_name = {t.name: t for t in deep_chat.build_tools(d)}
            self.assertIn("hello", by_name["read_repo_file"].invoke(
                {"path": "a.txt"}))
            self.assertIn("REJECTED", by_name["read_repo_file"].invoke(
                {"path": "../escape"}))
            self.assertIn("needle", by_name["grep_repo"].invoke(
                {"pattern": "needle"}))
            self.assertEqual(by_name["grep_repo"].invoke(
                {"pattern": "zzz-absent"}), "no matches")
            self.assertIn("a.txt", by_name["list_dir"].invoke({"path": ""}))

    def test_search_docs_formats_and_degrades(self):
        from types import SimpleNamespace
        from unittest import mock

        import retrieval

        by_name = {t.name: t for t in deep_chat.build_tools("/tmp")}
        hit = SimpleNamespace(metadata={"repo": "r", "source": "s"},
                              page_content="how to x")
        fake = SimpleNamespace(invoke=lambda q: [hit])
        with mock.patch.object(retrieval, "get_retriever",
                               return_value=fake):
            self.assertIn("[r/s]", by_name["search_docs"].invoke(
                {"query": "how to"}))
        broken = SimpleNamespace(
            invoke=mock.Mock(side_effect=RuntimeError("down")))
        with mock.patch.object(retrieval, "get_retriever",
                               return_value=broken):
            self.assertIn("unavailable", by_name["search_docs"].invoke(
                {"query": "how to"}))

    def test_propose_action_tool(self):
        by_name = {t.name: t for t in deep_chat.build_tools("/tmp")}
        out = by_name["propose_action"].invoke(
            {"kind": "td", "command": "td --json preflight"})
        self.assertIn("PROPOSE td", out)
        out = by_name["propose_action"].invoke(
            {"kind": "td", "command": "td do-evil"})
        self.assertIn("REJECTED", out)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestAgentMechanics(unittest.TestCase):
    def _stub_agent(self, replies, thread="t-mech"):
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult
        from pydantic import Field

        class QueueChatModel(BaseChatModel):
            replies: list = Field(default_factory=list)

            @property
            def _llm_type(self):
                return "queue-fake"

            def _generate(self, messages, stop=None, run_manager=None,
                          **kwargs):
                from langchain_core.messages import AIMessage

                msg = self.replies.pop(0) if self.replies \
                    else AIMessage(content="")
                return ChatResult(
                    generations=[ChatGeneration(message=msg)])

            def bind_tools(self, tools, **kwargs):
                return self

        from langgraph.checkpoint.memory import InMemorySaver

        agent = create_deep_agent(
            model=QueueChatModel(replies=list(replies)),
            tools=deep_chat.build_tools("/tmp"),
            system_prompt="Repo: x. test",
            interrupt_on=dict(deep_chat.INTERRUPT_ON),
            checkpointer=InMemorySaver(),
        )
        return agent, {"configurable": {"thread_id": thread}}

    def _tool_call_msg(self, args, call_id="call-1"):
        from langchain_core.messages import AIMessage

        return AIMessage(content="", tool_calls=[
            {"name": "propose_action", "args": args, "id": call_id,
             "type": "tool_call"},
        ])

    def test_pause_then_approve_executes(self):
        from langchain_core.messages import AIMessage
        from langgraph.types import Command

        agent, config = self._stub_agent([
            self._tool_call_msg({"kind": "td",
                                 "command": "td --json preflight"}),
            AIMessage(content="done after approval"),
        ])
        paused = agent.invoke(
            {"messages": [{"role": "user", "content": "check it"}]}, config)
        iv = paused.get("__interrupt__")
        self.assertTrue(iv, "expected a HITL pause")
        reqs = iv[0].value["action_requests"]
        self.assertEqual(reqs[0]["name"], "propose_action")
        cfgs = iv[0].value["review_configs"]
        self.assertEqual(cfgs[0]["allowed_decisions"],
                         ["approve", "reject"])
        done = agent.invoke(
            Command(resume=deep_chat.resume_payload(
                [deep_chat.decision_for_verdict("approve")] * len(reqs))),
            config)
        self.assertEqual(done["messages"][-1].content,
                         "done after approval")
        joined = "\n".join(str(m.content) for m in done["messages"]
                           if m.__class__.__name__ == "ToolMessage")
        self.assertIn("PROPOSE td", joined)

    def test_reject_skips_tool(self):
        from langchain_core.messages import AIMessage
        from langgraph.types import Command

        agent, config = self._stub_agent([
            self._tool_call_msg({"kind": "td",
                                 "command": "td --json preflight"}),
            AIMessage(content="ok, skipped it"),
        ])
        paused = agent.invoke(
            {"messages": [{"role": "user", "content": "check it"}]}, config)
        self.assertTrue(paused.get("__interrupt__"))
        done = agent.invoke(
            Command(resume=deep_chat.resume_payload(
                [deep_chat.decision_for_verdict("reject", "not now")])),
            config)
        self.assertEqual(done["messages"][-1].content, "ok, skipped it")
        joined = "\n".join(str(m.content) for m in done["messages"]
                           if m.__class__.__name__ == "ToolMessage")
        self.assertNotIn("PROPOSE", joined)

    def test_multiturn_memory_and_isolation(self):
        from langchain_core.messages import AIMessage

        agent, config = self._stub_agent(
            [AIMessage(content="first answer"),
             AIMessage(content="second answer"),
             AIMessage(content="other thread answer")])
        r1 = agent.invoke(
            {"messages": [{"role": "user", "content": "remember aardvark"}]},
            config)
        r2 = agent.invoke(
            {"messages": [{"role": "user", "content": "what was it?"}]},
            config)
        self.assertGreater(len(r2["messages"]), len(r1["messages"]))
        self.assertTrue(any("aardvark" in str(m.content)
                            for m in r2["messages"]))
        other = {"configurable": {"thread_id": "t-other"}}
        r3 = agent.invoke(
            {"messages": [{"role": "user", "content": "hi"}]}, other)
        self.assertFalse(any("aardvark" in str(m.content)
                             for m in r3["messages"]))

    def test_pending_interrupts_helper(self):
        from langchain_core.messages import AIMessage
        from langgraph.types import Command

        agent, config = self._stub_agent([
            self._tool_call_msg({"kind": "issue", "title": "t",
                                 "body": "b"}),
            AIMessage(content="final"),
        ])
        agent.invoke(
            {"messages": [{"role": "user", "content": "file it"}]}, config)
        pending = deep_chat._pending_interrupts(agent, config)
        self.assertEqual(len(pending), 1)
        self.assertIn("action_requests", pending[0]["value"])
        agent.invoke(Command(resume={"decisions": [{"type": "approve"}]}),
                     config)
        self.assertEqual(deep_chat._pending_interrupts(agent, config), [])

    def test_stream_helper_shape(self):
        from langchain_core.messages import AIMessage

        agent, config = self._stub_agent(
            [AIMessage(content="streamed hi")], thread="t-stream")
        buf = io.StringIO()
        with redirect_stdout(buf):
            reply, tool_msgs, msgs = deep_chat._stream(
                agent, {"messages": [{"role": "user", "content": "hi"}]},
                config)
        self.assertEqual(reply, "streamed hi")
        self.assertEqual(tool_msgs, [])
        self.assertEqual(len(msgs), 1)

    def _queue_model(self, replies):
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult
        from pydantic import Field

        class M(BaseChatModel):
            replies: list = Field(default_factory=list)

            @property
            def _llm_type(self):
                return "m"

            def _generate(self, messages, stop=None, run_manager=None,
                          **kwargs):
                from langchain_core.messages import AIMessage

                msg = self.replies.pop(0) if self.replies \
                    else AIMessage(content="")
                return ChatResult(
                    generations=[ChatGeneration(message=msg)])

            def bind_tools(self, tools, **kwargs):
                return self

        return M(replies=list(replies))

    def test_cmd_turn_and_resume_end_to_end(self):
        from unittest import mock

        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver

        saver = InMemorySaver()
        model = self._queue_model([
            self._tool_call_msg({"kind": "td",
                                 "command": "td --json preflight"}),
        ])
        spec = {"provider": "stub", "model": "m"}
        with mock.patch.object(deep_chat, "build_model",
                               return_value=(model, spec)), \
                mock.patch.object(deep_chat, "open_checkpointer",
                                  return_value=(saver, False, None)), \
                mock.patch.object(deep_chat.ledger, "record",
                                  return_value={}):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = deep_chat._cmd_turn(
                    "arii/remote-server", "t-e2e", b64("check it"))
            self.assertEqual(code, 3)
            line = [ln for ln in buf.getvalue().splitlines()
                    if ln.startswith("INTERRUPT: ")]
            self.assertEqual(len(line), 1)
            env = json.loads(line[0][len("INTERRUPT: "):])
            self.assertEqual(env["thread_id"], "t-e2e")
            self.assertEqual(env["interrupts"][0]["value"]
                             ["action_requests"][0]["name"], "propose_action")
            # Resume in a "fresh process" (same saver, new model).
            model.replies.append(AIMessage(content="e2e resumed"))
            buf2 = io.StringIO()
            with redirect_stdout(buf2):
                code2 = deep_chat._cmd_resume(
                    "arii/remote-server", "t-e2e", "approve", None)
            self.assertEqual(code2, 0)
            self.assertNotIn("INTERRUPT", buf2.getvalue())

    def test_cmd_resume_without_pause_fails(self):
        from unittest import mock

        from langgraph.checkpoint.memory import InMemorySaver

        model = self._queue_model([])
        spec = {"provider": "stub", "model": "m"}
        with mock.patch.object(deep_chat, "build_model",
                               return_value=(model, spec)), \
                mock.patch.object(deep_chat, "open_checkpointer",
                                  return_value=(InMemorySaver(), False, None)), \
                mock.patch.object(deep_chat.ledger, "record",
                                  return_value={}):
            code = deep_chat._cmd_resume(
                "arii/remote-server", "t-nopause", "approve", None)
        self.assertEqual(code, 1)

    def test_build_agent_wires_model_and_repo(self):
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver

        replies = [AIMessage(content="wired")]
        # Build through the real factory with an injected stub model.
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.outputs import ChatGeneration, ChatResult
        from pydantic import Field

        class M(BaseChatModel):
            replies: list = Field(default_factory=list)

            @property
            def _llm_type(self):
                return "m"

            def _generate(self, messages, stop=None, run_manager=None,
                          **kwargs):
                return ChatResult(generations=[
                    ChatGeneration(message=self.replies.pop(0))])

            def bind_tools(self, tools, **kwargs):
                return self

        real = deep_chat.build_agent(
            "arii/remote-server", InMemorySaver(), model=M(replies=replies))
        out = real.invoke(
            {"messages": [{"role": "user", "content": "hi"}]},
            {"configurable": {"thread_id": "t-wire"}})
        self.assertEqual(out["messages"][-1].content, "wired")


def _msg(cls_name, mid):
    return type(cls_name, (), {"id": mid, "content": "x"})()


def _history(n_turns, start=0):
    # System + (human, ai, tool) rounds with stable ids.
    msgs = [_msg("SystemMessage", "sys")] if start == 0 else []
    i = start
    for _ in range(n_turns):
        msgs.append(_msg("HumanMessage", f"h{i}"))
        msgs.append(_msg("AIMessage", f"a{i}"))
        msgs.append(_msg("ToolMessage", f"t{i}"))
        i += 1
    return msgs


class TestHistoryTrim(unittest.TestCase):
    def test_within_budget_keeps_all(self):
        self.assertEqual(deep_chat.plan_history_trim(_history(2)), [])

    def test_trims_oldest_snapped_to_human(self):
        msgs = _history(20)
        drop = deep_chat.plan_history_trim(msgs, keep=10)
        # 60 non-system; newest 10 start mid-round at t16, so the
        # snap keeps h17..t19 and drops h0..t16 (51 ids).
        self.assertEqual(len(drop), 51)
        self.assertNotIn("sys", drop)
        self.assertIn("h0", drop)
        self.assertIn("h16", drop)
        self.assertIn("t16", drop)
        self.assertNotIn("h17", drop)
        self.assertNotIn("t19", drop)

    def test_no_human_boundary_skips(self):
        msgs = [_msg("AIMessage", f"a{i}") for i in range(50)]
        self.assertEqual(deep_chat.plan_history_trim(msgs, keep=10), [])

    def test_idless_messages_skipped(self):
        msgs = _history(20)
        for m in msgs:
            m.id = None
        self.assertEqual(deep_chat.plan_history_trim(msgs, keep=10), [])

    def test_empty(self):
        self.assertEqual(deep_chat.plan_history_trim([]), [])
        self.assertEqual(deep_chat.plan_history_trim(None), [])


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestTrimThreadHistory(unittest.TestCase):
    def test_removes_via_update_state(self):
        from langchain_core.messages import RemoveMessage

        seen = {}

        class FakeAgent:
            def get_state(self, config):
                seen["config"] = config
                snap = unittest.mock.Mock()
                snap.values = {"messages": _history(20)}
                return snap

            def update_state(self, config, values):
                seen["update"] = values

        n = deep_chat.trim_thread_history(
            FakeAgent(), {"thread": "t"}, keep=10)
        self.assertTrue(n > 0)
        updates = seen["update"]["messages"]
        self.assertTrue(all(isinstance(m, RemoveMessage) for m in updates))
        self.assertEqual(len(updates), n)
        self.assertIn("h0", [m.id for m in updates])

    def test_noop_within_budget(self):
        class FakeAgent:
            def get_state(self, config):
                snap = unittest.mock.Mock()
                snap.values = {"messages": _history(2)}
                return snap

            def update_state(self, config, values):
                raise AssertionError("must not trim")

        self.assertEqual(
            deep_chat.trim_thread_history(FakeAgent(), {}), 0)

    def test_broken_agent_never_raises(self):
        class FakeAgent:
            def get_state(self, config):
                raise RuntimeError("saver gone")

        self.assertEqual(
            deep_chat.trim_thread_history(FakeAgent(), {}), 0)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestCheckpointer(unittest.TestCase):
    def test_open_checkpointer_shape(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "chat.db")
            saver, persistent, holder = deep_chat.open_checkpointer(path)
            self.assertIsNotNone(saver)
            if persistent:
                self.assertTrue(os.path.exists(path))
                self.assertIsNotNone(holder)
            else:
                self.assertEqual(saver.__class__.__name__, "InMemorySaver")
                self.assertIsNone(holder)


if __name__ == "__main__":
    unittest.main()
