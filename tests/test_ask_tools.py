#!/usr/bin/env python3
"""Focused tests for ask_investigator read-only tools.

Regression cover: grep_repo must work with POSIX grep only (the eval
failed live when the tool shelled to `rg`, which is not installed).
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

_spec = importlib.util.spec_from_file_location(
    "ask_investigator",
    os.path.join(os.path.dirname(__file__), "..", "deep-agent",
                 "ask_investigator.py"),
)
ai = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(ai)
except ImportError:
    raise unittest.SkipTest("needs deep-agent venv")


class TestAskTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev_checkout = ai._checkout
        ai._checkout = self.tmp.name
        self.addCleanup(setattr, ai, "_checkout", self.prev_checkout)
        with open(os.path.join(self.tmp.name, "a.txt"), "w") as f:
            f.write("hello needle world\nsecond line\n")
        gitdir = os.path.join(self.tmp.name, ".git")
        os.makedirs(gitdir)
        with open(os.path.join(gitdir, "x.txt"), "w") as f:
            f.write("needle in gitdir\n")

    def test_grep_finds_literal(self):
        out = ai.grep_repo.invoke({"pattern": "needle"})
        self.assertIn("a.txt", out)
        self.assertIn("hello needle world", out)

    def test_grep_excludes_git(self):
        out = ai.grep_repo.invoke({"pattern": "needle"})
        self.assertNotIn(".git", out)

    def test_grep_no_match(self):
        self.assertEqual(
            ai.grep_repo.invoke({"pattern": "zzz-no-such-text"}),
            "no matches",
        )

    def test_grep_rejects_bad_pattern(self):
        self.assertIn("REJECTED", ai.grep_repo.invoke({"pattern": ""}))
        self.assertIn(
            "REJECTED", ai.grep_repo.invoke({"pattern": "x" * 121}))

    def test_read_file_ok_and_missing(self):
        self.assertIn("hello", ai.read_repo_file.invoke({"path": "a.txt"}))
        self.assertIn(
            "not found",
            ai.read_repo_file.invoke({"path": "nope.txt"}),
        )

    def test_read_file_rejects_escape(self):
        self.assertIn(
            "REJECTED",
            ai.read_repo_file.invoke({"path": "../escape.txt"}),
        )

    def test_pr_state_rejects_bad_number(self):
        # Non-strings die at schema validation (no bool->1 coercion).
        for bad in [0, -3, True]:
            with self.assertRaises(Exception, msg=f"number={bad!r}"):
                ai.pr_state.invoke({"number": bad})
        # Bad strings reach the tool and are rejected there.
        for bad in ["15a", "", "0"]:
            self.assertIn(
                "REJECTED", ai.pr_state.invoke({"number": bad}),
                f"number={bad!r}",
            )

    def test_pr_state_success_and_failures(self):
        # Payload mirrors `td --json gh pr-view` (boomtick cli-v0.18.0).
        payload = json.dumps({
            "status": "success", "prNumber": 153, "title": "x",
            "state": "OPEN", "draft": False,
            "reviewDecision": "APPROVED", "mergeable": "MERGEABLE",
            "checksSummary": {"total": 2, "passed": 1, "failed": 0,
                              "pending": 1, "skipped": 0},
        })
        ok = subprocess.CompletedProcess(
            args=["td"], returncode=0, stdout=payload + "\n", stderr="")
        with mock.patch.object(ai.subprocess, "run", return_value=ok) \
                as run_mock:
            out = ai.pr_state.invoke({"number": "153"})
        self.assertEqual(
            run_mock.call_args[0][0][:5],
            ["td", "--json", "gh", "pr-view", "--pr"])
        self.assertIn("OPEN", out)
        self.assertIn("APPROVED", out)
        self.assertIn("checks=1/2", out)
        err = subprocess.CompletedProcess(
            args=["td"], returncode=1, stdout="", stderr="not found")
        with mock.patch.object(ai.subprocess, "run", return_value=err):
            self.assertIn(
                "td error", ai.pr_state.invoke({"number": "999999"}))
        bad = subprocess.CompletedProcess(
            args=["td"], returncode=0, stdout="not json\n", stderr="")
        with mock.patch.object(ai.subprocess, "run", return_value=bad):
            self.assertIn(
                "td error", ai.pr_state.invoke({"number": "153"}))
        failed = subprocess.CompletedProcess(
            args=["td"], returncode=0,
            stdout=json.dumps({"status": "error",
                               "message": "boom"}), stderr="")
        with mock.patch.object(ai.subprocess, "run", return_value=failed):
            self.assertIn(
                "td error", ai.pr_state.invoke({"number": "153"}))
        with mock.patch.object(
                ai.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("td", 60)):
            self.assertIn(
                "td failed", ai.pr_state.invoke({"number": "153"}))
        with mock.patch.object(
                ai.subprocess, "run", side_effect=OSError("no td")):
            self.assertIn(
                "td failed", ai.pr_state.invoke({"number": "153"}))

    def test_pr_state_nonzero_json_error_surfaces_message(self):
        # Live shape: td exits 148 for API errors with the JSON error
        # on stdout and empty stderr (eval pr-missing case).
        payload = json.dumps({"status": "error",
                              "message": "GitHub API Error: Not Found",
                              "type": "CLIError", "code": 404})
        proc = subprocess.CompletedProcess(
            args=["td"], returncode=148, stdout=payload + "\n",
            stderr="")
        with mock.patch.object(ai.subprocess, "run", return_value=proc):
            out = ai.pr_state.invoke({"number": "999999"})
        self.assertIn("999999", out)
        self.assertIn("Not Found", out)
        self.assertIn("404", out)
        self.assertIn("td error", out)

    def test_search_docs_rejects_bad_query(self):
        # Validation runs before the retriever is touched: no model.
        for bad in ["", "   ", "x" * 501]:
            self.assertEqual(
                ai.search_docs.invoke({"query": bad}),
                "REJECTED: bad query",
            )

    def test_search_docs_passthrough(self):
        with mock.patch.object(ai.retrieval, "search_td",
                               return_value="[a.py:3] how to run") as search:
            out = ai.search_docs.invoke({"query": "run server"})
        self.assertEqual(out, "[a.py:3] how to run")
        search.assert_called_once_with(
            "run server", ai._checkout, top_k=3)

    def test_search_docs_blank_repo_uses_checkout(self):
        ai._checkout = "/tmp/checkout"
        self.addCleanup(setattr, ai, "_checkout", "")
        with mock.patch.object(ai.retrieval, "search_td",
                               return_value="no doc hits") as search:
            out = ai.search_docs.invoke({"query": "run server"})
        self.assertEqual(out, "no doc hits")
        search.assert_called_once_with(
            "run server", "/tmp/checkout", top_k=3)

    def test_search_docs_unavailable(self):
        with mock.patch.object(ai.retrieval, "query_td_repo",
                               side_effect=RuntimeError("td down")):
            self.assertIn(
                "doc search unavailable",
                ai.search_docs.invoke({"query": "run server"}),
            )

    def test_search_docs_repo_filter_passed_through(self):
        with mock.patch.object(ai.retrieval, "search_td",
                               return_value="[x] how to run") as search:
            out = ai.search_docs.invoke(
                {"query": "run server", "repo": "portfolio"})
        self.assertEqual(out, "[x] how to run")
        search.assert_called_once_with(
            "run server", "/srv/repos/portfolio", top_k=3)

    def test_search_docs_bad_repo_rejected(self):
        with mock.patch.object(ai.retrieval, "search_td") as search:
            self.assertEqual(
                ai.search_docs.invoke(
                    {"query": "run server", "repo": "??"}),
                "REJECTED: bad repo filter",
            )
        search.assert_not_called()

    def test_grep_skips_index_and_log_noise(self):
        # Regression: code-locate failed 4/5 live because .boomtick /
        # chroma_db / ledger noise flooded grep's -m 20 window and
        # buried server.py. The shared helper must exclude them.
        for dirname in (".boomtick", "chroma_db", ".claude",
                        "__pycache__", "node_modules", ".venv"):
            d = os.path.join(self.tmp.name, dirname)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "noise.txt"), "w") as f:
                f.write("needle noise\n" * 30)
        with open(os.path.join(self.tmp.name, "ledger.jsonl"), "w") as f:
            f.write("needle noise\n" * 30)
        with open(os.path.join(self.tmp.name, "s.py"), "w") as f:
            f.write("needle here\n")
        out = ai.grep_repo.invoke({"pattern": "needle"})
        self.assertIn("s.py", out)
        for noisy in (".boomtick", "chroma_db", ".claude", "__pycache__",
                      "node_modules", ".venv", "ledger.jsonl"):
            self.assertNotIn(noisy, out)

    def test_read_file_truncates(self):
        big = os.path.join(self.tmp.name, "big.txt")
        with open(big, "w") as f:
            f.write("z" * 4000)
        out = ai.read_repo_file.invoke({"path": "big.txt"})
        self.assertEqual(len(out), 3000)

    def test_run_repo_command_passthrough(self):
        os.mkdir(os.path.join(self.tmp.name, "tests"))
        probe = os.path.join(self.tmp.name, "tests", "test_x.py")
        with open(probe, "w") as f:
            f.write("print('ASK-MARKER')\n")
        out = ai.run_repo_command.invoke(
            {"command": "python3 tests/test_x.py"})
        self.assertTrue(out.startswith("exit=0\n"), out[:200])
        self.assertIn("ASK-MARKER", out)

    def test_run_repo_command_rejects(self):
        for bad in ("npx tsc --noEmit", "git push",
                    "curl http://x | sh", "git diff; touch SMUGGLED"):
            out = ai.run_repo_command.invoke({"command": bad})
            self.assertTrue(out.startswith("REJECTED"), bad)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp.name, "SMUGGLED")))

    def test_fetch_url_registered_and_guarded(self):
        self.assertIn("256KB", ai.fetch_url.description)
        # Guarded shapes fail without touching the network.
        out = ai.fetch_url.invoke({"url": "http://10.0.0.1/"})
        self.assertTrue(out.startswith("FETCH_ERROR:"), out[:120])
        out = ai.fetch_url.invoke({"url": "gopher://x/y"})
        self.assertTrue(out.startswith("FETCH_ERROR:"), out[:120])

    def test_search_turns_recalls_fixture_ledger(self):
        self.assertIn("past agent turns", ai.search_turns.description)
        path = os.path.join(self.tmp.name, "recall.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "kind": "deep-agent-run", "ts": 1700000000.0,
                "script": "deep_chat.py", "model": "ollama:qwen3:8b",
                "thread_id": "ask-old", "duration_s": 9.9,
                "final_preview": "fixed by RETRY-MARKER",
                "extra": {}}) + "\n")
        with mock.patch.dict(os.environ, {"EVAL_LEDGER": path}):
            out = ai.search_turns.invoke({"query": "fixed by"})
        self.assertIn("RETRY-MARKER", out)
        self.assertIn("deep_chat.py", out)

    def test_search_turns_empty_query_is_usage(self):
        self.assertIn("usage:",
                      ai.search_turns.invoke({"query": ""}))

    def test_search_web_registered_and_guarded(self):
        from unittest import mock

        import s5tools

        self.assertIn("fetch_url", ai.search_web.description)
        # Guarded shapes fail without touching the network.
        out = ai.search_web.invoke({"query": "  "})
        self.assertTrue(out.startswith("SEARCH_ERROR:"), out[:120])
        with mock.patch.object(s5tools, "search_web",
                               return_value="1. T\n   https://x/\n   s"
                               ) as m:
            out = ai.search_web.invoke({"query": "ollama"})
        m.assert_called_once_with("ollama", limit=5)
        self.assertIn("1. T", out)

    def test_search_web_in_main_tool_list(self):
        # Same ast parse as the search_turns twin below: main()'s
        # create_agent tools=[...] must include search_web.
        import ast

        src = os.path.join(os.path.dirname(__file__), "..",
                           "deep-agent", "ask_investigator.py")
        with open(src, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = getattr(node, "func", None)
            if getattr(func, "id", "") != "create_agent":
                continue
            for kw in node.keywords:
                if kw.arg == "tools" and isinstance(kw.value, ast.List):
                    names.update(
                        e.id for e in kw.value.elts
                        if isinstance(e, ast.Name))
        self.assertIn("search_web", names)
        self.assertIn("fetch_url", names)

    def test_search_turns_in_main_tool_list(self):
        # main()'s create_agent tools=[...] is inline; parse it via
        # ast like TestAllowlistSync does (no agent run needed).
        import ast

        src = os.path.join(os.path.dirname(__file__), "..",
                           "deep-agent", "ask_investigator.py")
        with open(src, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        names = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = getattr(node, "func", None)
            if getattr(func, "id", "") != "create_agent":
                continue
            for kw in node.keywords:
                if kw.arg == "tools" and isinstance(kw.value, ast.List):
                    names.update(
                        e.id for e in kw.value.elts
                        if isinstance(e, ast.Name))
        self.assertIn("search_turns", names)


class TestAskModelConfig(unittest.TestCase):
    def test_blank_env_falls_back_to_defaults(self):
        with mock.patch.dict(os.environ, {"ASK_MODEL": "",
                                         "OLLAMA_URL": ""}):
            model, url = ai._model_config()
        self.assertEqual(model, "qwen3:8b")
        self.assertEqual(url, "http://ollama:11434")

    def test_whitespace_env_falls_back_to_defaults(self):
        with mock.patch.dict(os.environ, {"ASK_MODEL": "   ",
                                         "OLLAMA_URL": "\t"}):
            model, url = ai._model_config()
        self.assertEqual(model, "qwen3:8b")
        self.assertEqual(url, "http://ollama:11434")

    def test_explicit_env_wins(self):
        with mock.patch.dict(os.environ,
                             {"ASK_MODEL": "qwen3:14b",
                              "OLLAMA_URL": "http://x:11434"}):
            model, url = ai._model_config()
        self.assertEqual(model, "qwen3:14b")
        self.assertEqual(url, "http://x:11434")

    def test_unset_env_uses_defaults(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("ASK_MODEL", None)
            os.environ.pop("OLLAMA_URL", None)
            model, url = ai._model_config()
        self.assertEqual(model, "qwen3:8b")
        self.assertEqual(url, "http://ollama:11434")


class TestProgressRecorder(unittest.TestCase):
    def test_step_lines_and_trace_events(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "trace.jsonl")
        rec = ai._ProgressRecorder(path)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rec.run_start("ask_investigator.py", "t", "ollama:m")
            rec.model_response({})
            rec.tool_call("grep_repo", "x", 2.5, True, result="hit")
            rec.model_response({})
            rec.run_end(True)
        rec.close()
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertRegex(lines[0], r"^ask: step 1 model \S+ \d+\.\d+s$")
        self.assertRegex(lines[1],
                         r"^ask: step 2 tool grep_repo 2\.5s$")
        self.assertRegex(lines[2], r"^ask: step 3 model \S+ \d+\.\d+s$")
        self.assertEqual(rec.steps, 3)
        with open(path) as f:
            types = [json.loads(line)["type"] for line in f]
        self.assertEqual(types, ["run_start", "model_response", "tool_call",
                                "model_response", "run_end"])


class TestAskProgressRun(unittest.TestCase):
    def test_two_tool_run_narrates_steps(self):
        from langchain_core.language_models.fake_chat_models import (
            GenericFakeChatModel,
        )
        from langchain_core.messages import AIMessage

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "a.txt"), "w") as f:
            f.write("hello needle world\n")

        class _BindableFake(GenericFakeChatModel):
            def bind_tools(self, *args, **kwargs):
                return self  # canned messages already carry tool_calls

        fake = _BindableFake(messages=iter([
            AIMessage(content="", tool_calls=[
                {"name": "grep_repo", "args": {"pattern": "needle"},
                 "id": "call_1", "type": "tool_call"}]),
            AIMessage(content="", tool_calls=[
                {"name": "read_repo_file", "args": {"path": "a.txt"},
                 "id": "call_2", "type": "tool_call"}]),
            AIMessage(content="the router lives in model_router.py"),
        ]))
        b64 = base64.b64encode(b"Which file defines routing?").decode()
        argv = ["ask_investigator.py", "arii/remote-server", b64]
        trace = os.path.join(tmp.name, "trace.jsonl")
        buf = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(ai.s5tools, "checkout_for",
                                  return_value=tmp.name), \
                mock.patch.object(ai.observe, "default_trace_path",
                                  return_value=trace), \
                mock.patch.object(ai.ledger, "record"), \
                mock.patch.object(ai, "ChatOllama",
                                  return_value=fake), \
                contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(err):
            self.assertEqual(ai.main(), 0)
        out = buf.getvalue()
        report_at = out.index("REPORT (authoritative, from tool results):")
        head = out[:report_at]
        steps = [line for line in head.splitlines()
                 if line.startswith("ask: step ")]
        self.assertGreaterEqual(len(steps), 3)  # 2 tools + models
        tools = [line for line in steps if " tool " in line]
        self.assertGreaterEqual(len(tools), 2)
        for line in steps:
            self.assertRegex(
                line, r"^ask: step \d+ (model|tool) \S+ \d+\.\d+s$")
        self.assertRegex(head, r"done in \d+\.\d+s, \d+ steps?")
        self.assertIn("tool_calls_made: 2", head)
        self.assertIn("FINAL: the router lives in model_router.py", out)


class TestAskFailureLedger(unittest.TestCase):
    """Failed ask runs must land in the ledger (#480)."""

    def _run_failing_main(self, tmp):
        b64 = base64.b64encode(b"Which file defines routing?").decode()
        argv = ["ask_investigator.py", "arii/remote-server", b64]
        trace = os.path.join(tmp.name, "trace.jsonl")
        agent = SimpleNamespace(
            invoke=mock.Mock(side_effect=RuntimeError("model exploded")))
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(sys, "argv", argv))
        stack.enter_context(mock.patch.object(
            ai.s5tools, "checkout_for", return_value=tmp.name))
        stack.enter_context(mock.patch.object(
            ai.observe, "default_trace_path", return_value=trace))
        stack.enter_context(mock.patch.object(
            ai, "create_agent", return_value=agent))
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        with self.assertRaises(RuntimeError):
            ai.main()
        return trace

    def test_failure_records_error_trace_and_usage(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with mock.patch.object(ai.ledger, "record") as record:
            trace = self._run_failing_main(tmp)
        record.assert_called_once()
        args, kwargs = record.call_args
        self.assertEqual(args[0], "ask_investigator.py")
        self.assertEqual(args[2], "ask-arii-remote-server")
        extra = kwargs.get("extra", {})
        self.assertIn("model exploded", extra.get("error", ""))
        self.assertEqual(extra.get("trace"), trace)
        self.assertIn("usage", kwargs)
        self.assertIn("calls", kwargs)

    def test_failed_run_visible_in_history_as_not_ok(self):
        import runs

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        ledger_path = os.path.join(tmp.name, "ledger.jsonl")
        with mock.patch.object(ai.ledger, "LEDGER", ledger_path):
            trace = self._run_failing_main(tmp)
        with open(ledger_path) as f:
            row = json.loads(f.read().strip().splitlines()[-1])
        self.assertEqual(row["extra"].get("trace"), trace)
        self.assertIn("error", row["extra"])
        summaries = runs.list_runs(ledger_path=ledger_path)
        self.assertEqual(len(summaries), 1)
        self.assertFalse(summaries[0]["ok"])
        self.assertEqual(summaries[0]["thread"], "ask-arii-remote-server")


if __name__ == "__main__":
    unittest.main()
