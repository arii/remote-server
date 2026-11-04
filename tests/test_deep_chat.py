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


class TestApprovedProposalBlocks(unittest.TestCase):
    def test_valid_issue_block(self):
        texts = deep_chat.approved_proposal_blocks([
            {"name": "propose_action",
             "args": {"kind": "issue", "title": "T", "body": "B"}}])
        self.assertEqual(len(texts), 1)
        self.assertIn("PROPOSE issue", texts[0])
        self.assertIn("END PROPOSE", texts[0])

    def test_invalid_becomes_rejected(self):
        texts = deep_chat.approved_proposal_blocks([
            {"name": "propose_action", "args": {"kind": "issue"}}])
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].startswith("REJECTED"))

    def test_skips_malformed(self):
        self.assertEqual(
            deep_chat.approved_proposal_blocks([None, "x", {}]),
            ["REJECTED: kind must be 'issue', 'td', or 'dispatch'"])
        self.assertEqual(deep_chat.approved_proposal_blocks([]), [])
        self.assertEqual(deep_chat.approved_proposal_blocks(None), [])


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


class TestSuperviseMode(unittest.TestCase):
    def test_brief_is_propose_only(self):
        brief = deep_chat.SUPERVISOR_BRIEF
        self.assertIn("TODO", brief)
        self.assertIn("propose_action", brief)
        self.assertIn("read-only", brief)

    def test_argv_dispatch(self):
        from unittest import mock

        with mock.patch.object(deep_chat, "_cmd_turn",
                               return_value=0) as turn:
            code = deep_chat.main(
                ["deep_chat.py", "arii/remote-server", "--supervise"])
        self.assertEqual(code, 0)
        repo, thread, raw = turn.call_args[0]
        self.assertEqual(repo, "arii/remote-server")
        self.assertEqual(thread, "supervise-remote-server")
        self.assertEqual(base64.b64decode(raw).decode(),
                         deep_chat.SUPERVISOR_BRIEF)

    def test_bad_repo_rejected(self):
        from unittest import mock

        with mock.patch.object(deep_chat, "_cmd_turn") as turn:
            code = deep_chat.main(["deep_chat.py", "nope", "--supervise"])
        self.assertEqual(code, 2)
        turn.assert_not_called()


class TestTrivialFastPath(unittest.TestCase):
    def test_trivial_skips_harness(self):
        from unittest import mock

        seen = {}

        def fake_trivial(repo, thread_id, prompt, started, model, spec):
            seen["prompt"] = prompt
            return 0

        def boom(*args, **kwargs):
            raise AssertionError("harness entered for trivial prompt")

        with mock.patch.object(
                deep_chat, "build_model",
                return_value=(object(), {"provider": "x",
                                        "model": "y"})), \
             mock.patch.object(deep_chat, "build_agent",
                               side_effect=boom), \
             mock.patch.object(deep_chat, "_cmd_trivial_turn",
                               side_effect=fake_trivial):
            with redirect_stdout(io.StringIO()):
                code = deep_chat._cmd_turn(
                    "arii/remote-server", "t1", b64("hi"))
        self.assertEqual(code, 0)
        self.assertEqual(seen.get("prompt"), "hi")


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestTrivialTrace(unittest.TestCase):
    """Trivial fast-path turns record a minimal trace (#420)."""

    def _run_trivial(self, tmp, model, ledger_seen):
        from unittest import mock

        trace_path = os.path.join(tmp, "trivial.jsonl")

        def fake_record(script, model_name, thread_id, messages,
                        started, extra=None, usage=None, calls=None):
            ledger_seen["extra"] = extra
            ledger_seen["usage"] = usage
            return {}

        with mock.patch.object(deep_chat.ledger, "record",
                               side_effect=fake_record), \
             mock.patch.object(deep_chat.observe,
                               "default_trace_path",
                               return_value=trace_path):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = deep_chat._cmd_trivial_turn(
                    "arii/remote-server", "t-trivial", "hi", 0.0,
                    model, {"provider": "ollama", "model": "qwen3:8b"})
        return code, buf.getvalue(), trace_path

    def _events(self, trace_path):
        with open(trace_path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def test_success_emits_request_response_pair(self):
        import tempfile
        from types import SimpleNamespace
        from unittest import mock

        usage = {"input_tokens": 12, "output_tokens": 5,
                 "total_tokens": 17}
        model = SimpleNamespace(invoke=mock.Mock(return_value=(
            SimpleNamespace(content="hey!", usage_metadata=usage))))
        seen = {}
        with tempfile.TemporaryDirectory() as tmp:
            code, out, trace_path = self._run_trivial(tmp, model, seen)
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "hey!")
            self.assertEqual(seen["extra"]["trace"], trace_path)
            self.assertEqual(seen["usage"], usage)
            events = self._events(trace_path)
        self.assertEqual([e["type"] for e in events],
                         ["run_start", "model_request", "model_response",
                          "run_end"])
        self.assertEqual(events[0]["model"], "ollama:qwen3:8b")
        self.assertEqual(events[0]["thread"], "t-trivial")
        self.assertEqual(events[1]["n_messages"], 2)
        self.assertEqual(events[2]["usage"]["total_tokens"], 17)
        self.assertTrue(events[3]["ok"])
        self.assertEqual(
            [d["why"] for d in seen["extra"]["decisions"]],
            ["trivial greeting, fast path"])

    def test_unreported_usage_records_zeros(self):
        import tempfile
        from types import SimpleNamespace
        from unittest import mock

        model = SimpleNamespace(invoke=mock.Mock(return_value=(
            SimpleNamespace(content="hey!", usage_metadata=None))))
        seen = {}
        with tempfile.TemporaryDirectory() as tmp:
            code, _, trace_path = self._run_trivial(tmp, model, seen)
            self.assertEqual(code, 0)
            self.assertIsNone(seen["usage"])
            events = self._events(trace_path)
        responses = [e for e in events if e["type"] == "model_response"]
        self.assertEqual(len(responses), 1)
        self.assertEqual(responses[0]["usage"]["total_tokens"], 0)

    def test_error_emits_start_end_and_sorry(self):
        import tempfile
        from types import SimpleNamespace
        from unittest import mock

        model = SimpleNamespace(invoke=mock.Mock(
            side_effect=RuntimeError("model down")))
        seen = {}
        with tempfile.TemporaryDirectory() as tmp:
            code, out, trace_path = self._run_trivial(tmp, model, seen)
            self.assertEqual(code, 1)
            self.assertIn("Sorry, the assistant failed", out)
            self.assertEqual(seen["extra"]["trace"], trace_path)
            self.assertIn("error", seen["extra"])
            events = self._events(trace_path)
        types = [e["type"] for e in events]
        self.assertGreaterEqual(len(types), 2)
        self.assertEqual(types[0], "run_start")
        self.assertEqual(types[-1], "run_end")
        self.assertFalse(events[-1]["ok"])


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestToolSurface(unittest.TestCase):
    def test_eight_tools(self):
        tools = deep_chat.build_tools("/tmp")
        self.assertEqual(len(tools), 8)
        self.assertEqual(
            {t.name for t in tools},
            {"read_repo_file", "grep_repo", "list_dir", "search_docs",
             "propose_action", "run_repo_command", "fetch_url",
             "search_turns"},
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

    def test_run_repo_command_tool(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "tests"))
            probe = os.path.join(d, "tests", "test_x.py")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("print('X-MARKER')\n")
            by_name = {t.name: t for t in deep_chat.build_tools(d)}
            out = by_name["run_repo_command"].invoke(
                {"command": "python3 tests/test_x.py"})
            self.assertTrue(out.startswith("exit=0\n"), out[:200])
            self.assertIn("X-MARKER", out)
            out = by_name["run_repo_command"].invoke(
                {"command": "npx tsc --noEmit"})
            self.assertIn("REJECTED", out)

    def test_fetch_url_tool(self):
        by_name = {t.name: t for t in deep_chat.build_tools("/tmp")}
        tool = by_name["fetch_url"]
        self.assertIn("256KB", tool.description)
        # Guarded shapes fail without touching the network.
        out = tool.invoke({"url": "http://169.254.169.254/"})
        self.assertTrue(out.startswith("FETCH_ERROR:"), out[:120])
        out = tool.invoke({"url": "file:///etc/passwd"})
        self.assertTrue(out.startswith("FETCH_ERROR:"), out[:120])

    def test_search_turns_tool(self):
        import tempfile

        by_name = {t.name: t for t in deep_chat.build_tools("/tmp")}
        tool = by_name["search_turns"]
        self.assertIn("past agent turns", tool.description)
        self.assertIn("triage", tool.description)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "kind": "deep-agent-run", "ts": 1700000000.0,
                    "script": "ask_investigator.py",
                    "model": "ollama:qwen3:8b", "thread_id": "ask-a-b",
                    "duration_s": 12.5,
                    "final_preview": "fixed by pinning the model",
                    "extra": {}}) + "\n")
            prev = os.environ.get("EVAL_LEDGER")
            os.environ["EVAL_LEDGER"] = path
            try:
                out = tool.invoke({"query": "pinning the model"})
            finally:
                if prev is None:
                    del os.environ["EVAL_LEDGER"]
                else:
                    os.environ["EVAL_LEDGER"] = prev
        self.assertIn("pinning the model", out)
        self.assertIn("run-0", out)
        self.assertIn("usage:",
                      tool.invoke({"query": "  "}))


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestAgentMechanics(unittest.TestCase):
    def _stub_agent(self, replies, thread="t-mech", checkout="/tmp"):
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
            tools=deep_chat.build_tools(checkout),
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

    def test_verify_tool_runs_in_turn(self):
        import tempfile

        from langchain_core.messages import AIMessage

        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "tests"))
            probe = os.path.join(d, "tests", "test_x.py")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("print('TURN-MARKER')\n")
            call = AIMessage(content="", tool_calls=[{
                "name": "run_repo_command",
                "args": {"command": "python3 tests/test_x.py"},
                "id": "call-verify", "type": "tool_call"}])
            agent, config = self._stub_agent(
                [call, AIMessage(content="exit was 0, tail shows TURN-MARKER")],
                thread="t-verify", checkout=d)
            done = agent.invoke(
                {"messages": [{"role": "user",
                               "content": "run the test to confirm"}]},
                config)
            joined = "\n".join(
                str(m.content) for m in done["messages"]
                if m.__class__.__name__ == "ToolMessage")
            self.assertIn("exit=0", joined)
            self.assertIn("TURN-MARKER", joined)
            self.assertEqual(
                done["messages"][-1].content,
                "exit was 0, tail shows TURN-MARKER")

    def test_fetch_tool_runs_in_turn(self):
        from unittest import mock

        from langchain_core.messages import AIMessage

        import s5tools

        call = AIMessage(content="", tool_calls=[{
            "name": "fetch_url",
            "args": {"url": "https://docs.example.com/x"},
            "id": "call-fetch", "type": "tool_call"}])
        agent, config = self._stub_agent(
            [call, AIMessage(content="the docs say FROBNICATE")],
            thread="t-fetch")
        with mock.patch.object(
                s5tools, "fetch_url",
                return_value="upstream text FROBNICATE") as fetch:
            done = agent.invoke(
                {"messages": [{"role": "user",
                               "content": "read the upstream docs"}]},
                config)
        fetch.assert_called_once_with("https://docs.example.com/x")
        joined = "\n".join(
            str(m.content) for m in done["messages"]
            if m.__class__.__name__ == "ToolMessage")
        self.assertIn("FROBNICATE", joined)
        self.assertEqual(done["messages"][-1].content,
                         "the docs say FROBNICATE")

    def test_search_turns_runs_in_turn(self):
        import tempfile

        from langchain_core.messages import AIMessage

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({
                    "kind": "deep-agent-run", "ts": 1700000000.0,
                    "script": "ask_investigator.py",
                    "model": "ollama:qwen3:8b",
                    "thread_id": "ask-old", "duration_s": 9.9,
                    "final_preview": "triage verdict: QUOTA-FAULT",
                    "extra": {}}) + "\n")
            call = AIMessage(content="", tool_calls=[{
                "name": "search_turns",
                "args": {"query": "triage verdict"},
                "id": "call-recall", "type": "tool_call"}])
            agent, config = self._stub_agent(
                [call, AIMessage(
                    content="last triage concluded QUOTA-FAULT")],
                thread="t-recall")
            prev = os.environ.get("EVAL_LEDGER")
            os.environ["EVAL_LEDGER"] = path
            try:
                done = agent.invoke(
                    {"messages": [{"role": "user",
                                   "content": "what did triage conclude?"}]},
                    config)
            finally:
                if prev is None:
                    del os.environ["EVAL_LEDGER"]
                else:
                    os.environ["EVAL_LEDGER"] = prev
        joined = "\n".join(
            str(m.content) for m in done["messages"]
            if m.__class__.__name__ == "ToolMessage")
        self.assertIn("QUOTA-FAULT", joined)
        self.assertEqual(done["messages"][-1].content,
                         "last triage concluded QUOTA-FAULT")

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
            reply, tool_msgs, msgs, salvaged = deep_chat._stream(
                agent, {"messages": [{"role": "user", "content": "hi"}]},
                config)
        self.assertEqual(reply, "streamed hi")
        self.assertEqual(tool_msgs, [])
        self.assertEqual(len(msgs), 1)
        self.assertFalse(salvaged)

    def test_stream_salvages_recursion_cutoff(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.errors import GraphRecursionError

        class LoopingAgent:
            def stream(self, *a, **k):
                raise GraphRecursionError("limit of 40 reached")

        seen = {}

        class FakeModel:
            def invoke(self, messages):
                seen["messages"] = messages
                return AIMessage(content="salvaged answer")

        buf = io.StringIO()
        with redirect_stdout(buf):
            reply, _, _, salvaged = deep_chat._stream(
                LoopingAgent(),
                {"messages": [{"role": "user", "content": "hi"}]},
                {}, model=FakeModel())
        self.assertEqual(reply, "salvaged answer")
        self.assertTrue(salvaged)
        self.assertIn("salvaged answer", buf.getvalue())
        tail = seen["messages"][-1]
        self.assertIsInstance(tail, HumanMessage)
        self.assertIn("any more tool calls", tail.content)

    def test_stream_reraises_without_model(self):
        from langgraph.errors import GraphRecursionError

        class LoopingAgent:
            def stream(self, *a, **k):
                raise GraphRecursionError("limit of 40 reached")

        with self.assertRaises(GraphRecursionError):
            deep_chat._stream(
                LoopingAgent(), {"messages": []}, {})

    def test_build_agent_appends_intent_hint(self):
        from unittest import mock

        with mock.patch("deepagents.create_deep_agent") as factory:
            deep_chat.build_agent(
                "arii/remote-server", mock.Mock(), model="m",
                intent_hint="HINT-SENTINEL")
        prompt = factory.call_args.kwargs["system_prompt"]
        self.assertIn("HINT-SENTINEL", prompt)
        with mock.patch("deepagents.create_deep_agent") as factory:
            deep_chat.build_agent("arii/remote-server", mock.Mock(),
                                  model="m")
        self.assertNotIn("HINT-SENTINEL",
                         factory.call_args.kwargs["system_prompt"])

    def test_stream_reraises_when_salvage_fails(self):
        from langgraph.errors import GraphRecursionError

        class LoopingAgent:
            def stream(self, *a, **k):
                raise GraphRecursionError("limit of 40 reached")

        class DeadModel:
            def invoke(self, messages):
                raise RuntimeError("invoke down")

        with self.assertRaises(GraphRecursionError):
            deep_chat._stream(
                LoopingAgent(), {"messages": []}, {},
                model=DeadModel())

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
        seen_extras = []

        def fake_record(script, model_name, thread_id, messages,
                        started, extra=None, usage=None, calls=None):
            seen_extras.append(extra)
            return {}

        with mock.patch.object(deep_chat, "build_model",
                               return_value=(model, spec)), \
                mock.patch.object(deep_chat, "open_checkpointer",
                                  return_value=(saver, False, None)), \
                mock.patch.object(deep_chat.ledger, "record",
                                  side_effect=fake_record):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = deep_chat._cmd_turn(
                    "arii/remote-server", "t-e2e", b64("check it"))
            self.assertEqual(code, 3)
            # "check it" classifies explain: the answer line's why
            # names intent + full path, not the reply text (#450).
            self.assertEqual(
                seen_extras[-1]["decisions"][-1]["why"],
                "explain turn, full path")
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
            self.assertEqual(
                seen_extras[-1]["decisions"][-1]["why"],
                "resumed after approve, full path")

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

    def _spying_model(self, base, replies, seen, **kwargs):
        from langchain_core.outputs import ChatGeneration, ChatResult
        from pydantic import Field

        class Spy(base):
            replies: list = Field(default_factory=list)

            def _generate(self, messages, stop=None, run_manager=None,
                          **kwargs):
                from langchain_core.messages import AIMessage

                msg = self.replies.pop(0) if self.replies \
                    else AIMessage(content="")
                return ChatResult(
                    generations=[ChatGeneration(message=msg)])

            def bind_tools(self, tools, **kwargs):
                for t in tools:
                    name = t.get("name") if isinstance(t, dict) \
                        else getattr(t, "name", None)
                    if name:
                        seen.append(name)
                return self

        return Spy(replies=list(replies), **kwargs)

    def _fs_call_msg(self, name, args, call_id="call-fs"):
        from langchain_core.messages import AIMessage

        return AIMessage(content="", tool_calls=[
            {"name": name, "args": args, "id": call_id,
             "type": "tool_call"},
        ])

    def _tool_text(self, out):
        return "\n".join(str(m.content) for m in out["messages"]
                         if m.__class__.__name__ == "ToolMessage")

    def test_chat_gates_exclude_task(self):
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver
        from langchain_ollama import ChatOllama

        seen = []
        model = self._spying_model(
            ChatOllama, [AIMessage(content="hi-done")], seen,
            model="qwen3:8b")
        agent = deep_chat.build_agent(
            "arii/remote-server", InMemorySaver(), model=model,
            spec={"provider": "ollama", "model": "qwen3:8b"})
        out = agent.invoke(
            {"messages": [{"role": "user", "content": "hi"}]},
            {"configurable": {"thread_id": "t-gate-task"}})
        self.assertEqual(out["messages"][-1].content, "hi-done")
        self.assertNotIn("task", seen)
        for name in ("read_repo_file", "grep_repo", "list_dir",
                     "search_docs", "propose_action"):
            self.assertIn(name, seen)

    def test_chat_gates_translate_nebius_provider(self):
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver
        from langchain_openai import ChatOpenAI

        seen = []
        model = self._spying_model(
            ChatOpenAI, [AIMessage(content="hi-done")], seen,
            model="glm-gate-probe", api_key="test")
        agent = deep_chat.build_agent(
            "arii/remote-server", InMemorySaver(), model=model,
            spec={"provider": "nebius", "model": "glm-gate-probe"})
        agent.invoke(
            {"messages": [{"role": "user", "content": "hi"}]},
            {"configurable": {"thread_id": "t-gate-nebius"}})
        self.assertNotIn("task", seen)

    def test_chat_gates_deny_virtual_writes(self):
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver
        from langchain_ollama import ChatOllama

        seen = []
        model = self._spying_model(
            ChatOllama,
            [self._fs_call_msg("write_file",
                               {"file_path": "/probe.txt",
                                "content": "x"}),
             AIMessage(content="done")],
            seen, model="qwen3:8b")
        agent = deep_chat.build_agent(
            "arii/remote-server", InMemorySaver(), model=model,
            spec={"provider": "ollama", "model": "qwen3:8b"})
        out = agent.invoke(
            {"messages": [{"role": "user", "content": "write it"}]},
            {"configurable": {"thread_id": "t-gate-write"}})
        self.assertIn("permission denied", self._tool_text(out))

    def test_chat_gates_allow_virtual_reads(self):
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver
        from langchain_ollama import ChatOllama

        seen = []
        model = self._spying_model(
            ChatOllama,
            [self._fs_call_msg("read_file",
                               {"file_path": "/probe.txt"}),
             AIMessage(content="done")],
            seen, model="qwen3:8b")
        agent = deep_chat.build_agent(
            "arii/remote-server", InMemorySaver(), model=model,
            spec={"provider": "ollama", "model": "qwen3:8b"})
        out = agent.invoke(
            {"messages": [{"role": "user", "content": "read it"}]},
            {"configurable": {"thread_id": "t-gate-read"}})
        self.assertNotIn("permission denied", self._tool_text(out))

    def _looper_model(self, n_calls, seen_binds, seen_msgs):
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult
        from pydantic import Field

        class Looper(BaseChatModel):
            replies: list = Field(default_factory=list)

            @property
            def _llm_type(self):
                return "looper"

            def _generate(self, messages, stop=None, run_manager=None,
                          **kwargs):
                seen_msgs.append(list(messages))
                msg = self.replies.pop(0) if self.replies \
                    else AIMessage(content="")
                return ChatResult(
                    generations=[ChatGeneration(message=msg)])

            def bind_tools(self, tools, **kwargs):
                seen_binds.append(
                    {t.get("name") if isinstance(t, dict)
                     else getattr(t, "name", None) for t in tools})
                return self

        calls = [AIMessage(content="", tool_calls=[
            {"name": "grep_repo", "args": {"pattern": f"zzz-{i}"},
             "id": f"loop-{i}", "type": "tool_call"},
        ]) for i in range(n_calls)]
        return Looper(replies=calls + [AIMessage(content="done")])

    def test_tool_budget_narrows_to_propose(self):
        from langgraph.checkpoint.memory import InMemorySaver

        binds, msgs = [], []
        model = self._looper_model(deep_chat.CHAT_TOOL_BUDGET + 1,
                                   binds, msgs)
        agent = deep_chat.build_agent("arii/remote-server",
                                      InMemorySaver(), model=model)
        agent.invoke(
            {"messages": [{"role": "user", "content": "dig deep"}]},
            {"configurable": {"thread_id": "t-budget-narrow"}})
        self.assertGreater(len(binds), 1)
        self.assertIn("grep_repo", binds[0])
        self.assertIn({"propose_action"}, binds[1:])
        nudge_seen = any(
            m.__class__.__name__ == "SystemMessage"
            and "budget" in str(m.content).lower()
            for batch in msgs for m in batch)
        self.assertTrue(nudge_seen, "expected a budget-spent nudge")

    def test_tool_budget_rejects_over_calls(self):
        from langgraph.checkpoint.memory import InMemorySaver

        binds, msgs = [], []
        model = self._looper_model(deep_chat.CHAT_TOOL_BUDGET + 1,
                                   binds, msgs)
        agent = deep_chat.build_agent("arii/remote-server",
                                      InMemorySaver(), model=model)
        out = agent.invoke(
            {"messages": [{"role": "user", "content": "dig deep"}]},
            {"configurable": {"thread_id": "t-budget-reject"}})
        self.assertIn("budget spent", self._tool_text(out))


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


class TestExtractPrefer(unittest.TestCase):
    def test_flag_values(self):
        self.assertEqual(
            deep_chat.extract_prefer(["deep_chat.py", "--prefer=ollama"]),
            "ollama",
        )
        self.assertEqual(
            deep_chat.extract_prefer(["deep_chat.py", "--prefer=nebius"]),
            "nebius",
        )

    def test_absent_is_none(self):
        self.assertIsNone(deep_chat.extract_prefer(["deep_chat.py"]))
        # The b64 prompt slot can never match: '-' is outside the
        # base64 alphabet.
        self.assertIsNone(
            deep_chat.extract_prefer(
                ["deep_chat.py", "o/r", "t", b64("say --prefer=nebius")]
            )
        )

    def test_bad_value_raises(self):
        with self.assertRaises(ValueError):
            deep_chat.extract_prefer(["deep_chat.py", "--prefer=Muse"])

    def test_main_threads_prefer_to_turn(self):
        from unittest import mock

        seen = {}

        def fake_turn(repo, thread, raw, prefer=None):
            seen["prefer"] = prefer
            return 0

        with mock.patch.object(deep_chat, "_cmd_turn",
                               side_effect=fake_turn), \
                mock.patch.object(deep_chat.s5tools, "valid_repo",
                                  return_value=True), \
                mock.patch.object(deep_chat.s5tools, "checkout_for",
                                  return_value="/tmp"), \
                mock.patch.object(deep_chat, "valid_thread_id",
                                  return_value=True):
            rc = deep_chat.main(
                ["deep_chat.py", "o/r", "thread-1", b64("hi"),
                 "--prefer=nebius"]
            )
        self.assertEqual(rc, 0)
        self.assertEqual(seen["prefer"], "nebius")

    def test_main_defaults_to_auto(self):
        from unittest import mock

        seen = {}

        def fake_turn(repo, thread, raw, prefer=None):
            seen["prefer"] = prefer
            return 0

        with mock.patch.object(deep_chat, "_cmd_turn",
                               side_effect=fake_turn), \
                mock.patch.object(deep_chat.s5tools, "valid_repo",
                                  return_value=True), \
                mock.patch.object(deep_chat.s5tools, "checkout_for",
                                  return_value="/tmp"), \
                mock.patch.object(deep_chat, "valid_thread_id",
                                  return_value=True):
            rc = deep_chat.main(
                ["deep_chat.py", "o/r", "thread-1", b64("hi")]
            )
        self.assertEqual(rc, 0)
        self.assertIsNone(seen["prefer"])

    def test_main_rejects_bad_prefer(self):
        rc = deep_chat.main(["deep_chat.py", "--prefer=bogus"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
