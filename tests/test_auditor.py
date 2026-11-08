"""Tests for deep-agent/auditor.py (stdlib only, no model).

Graph integration cases need langgraph (deep-agent venv) and skip
otherwise. Run agent cases with: deep-agent/.venv/bin/python
tests/test_auditor.py
"""
import json
import os
import re
import sys
import tempfile
import unittest

import auditor

try:
    import langgraph  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def _gate_ok(args):
    return 0, json.dumps({"status": "success", "passed": True,
                          "impacted_files": ["a.py"],
                          "dependent_tests": [], "violations": []})


class TestRunTdAuditImpact(unittest.TestCase):
    def test_parses_payload(self):
        ok, payload = auditor.run_td_audit_impact(
            "/tmp", runner=_gate_ok)
        self.assertTrue(ok)
        self.assertEqual(payload["impacted_files"], ["a.py"])

    def test_non_json_fails(self):
        ok, error = auditor.run_td_audit_impact(
            "/tmp", runner=lambda args: (0, "not json"))
        self.assertFalse(ok)
        self.assertIn("non-JSON", error)

    def test_missing_td_fails_closed(self):
        def runner(args):
            raise FileNotFoundError("no td")
        ok, error = auditor.run_td_audit_impact("/tmp", runner=runner)
        self.assertFalse(ok)
        self.assertIn("not found", error)

    def test_error_exit_fails(self):
        ok, error = auditor.run_td_audit_impact(
            "/tmp", runner=lambda args: (1, "boom"))
        self.assertFalse(ok)
        self.assertTrue(error)

    def test_preview_url_flows_to_impact(self):
        seen = []

        def runner(args):
            seen.append(list(args))
            return _gate_ok(args)

        ok, _ = auditor.run_td_audit_impact(
            "/tmp", runner=runner, preview_url="https://x/preview")
        self.assertTrue(ok)
        self.assertIn("--preview-url", seen[0])
        self.assertIn("https://x/preview", seen[0])

    def test_no_preview_url_omits_flag(self):
        seen = []

        def runner(args):
            seen.append(list(args))
            return _gate_ok(args)

        ok, _ = auditor.run_td_audit_impact("/tmp", runner=runner)
        self.assertTrue(ok)
        self.assertNotIn("--preview-url", seen[0])


class TestHardGatePreviewUrl(unittest.TestCase):
    def test_gate_passes_preview_url(self):
        seen = []

        def td_runner(args):
            seen.append(list(args))
            return 0, json.dumps({"status": "success", "passed": True,
                                  "impacted_files": [], "violations": []})

        with tempfile.TemporaryDirectory() as tmp:
            gen = auditor.AuditorGraph(
                tmp, td_runner=td_runner,
                preview_url="https://x/preview",
                thread_id="test-audit-preview")
            valid, payloads, errors = gen.hard_gate()
        self.assertTrue(valid, errors)
        impact = [a for a in seen if a[0] == "audit-impact"]
        self.assertTrue(impact)
        self.assertIn("--preview-url", impact[0])
        self.assertIn("https://x/preview", impact[0])

    def test_gate_omits_preview_url_by_default(self):
        seen = []

        def td_runner(args):
            seen.append(list(args))
            return 0, json.dumps({"status": "success", "passed": True,
                                  "impacted_files": [], "violations": []})

        with tempfile.TemporaryDirectory() as tmp:
            gen = auditor.AuditorGraph(
                tmp, td_runner=td_runner, thread_id="test-audit-noprev")
            valid, _, errors = gen.hard_gate()
        self.assertTrue(valid, errors)
        impact = [a for a in seen if a[0] == "audit-impact"]
        self.assertTrue(impact)
        self.assertNotIn("--preview-url", impact[0])


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langgraph)")
class TestAuditorGraph(unittest.TestCase):
    def setUp(self):
        import ledger
        self._ledger_tmp = tempfile.TemporaryDirectory()
        self._ledger_prev = ledger.LEDGER
        ledger.LEDGER = os.path.join(self._ledger_tmp.name, "ledger.jsonl")

    def tearDown(self):
        import ledger
        ledger.LEDGER = self._ledger_prev
        self._ledger_tmp.cleanup()

    def _gen(self, tmp, td_runner=None, thread="test-audit-1"):
        return auditor.AuditorGraph(
            tmp, audit_fn=lambda gate: "1. Fix the naming.",
            td_runner=td_runner or _gate_ok, thread_id=thread)

    def test_violations_fail_closed_no_llm(self):
        seen = []

        def td_runner(args):
            seen.append(args[0])
            return 1, json.dumps(
                {"status": "success", "passed": False,
                 "impacted_files": ["a.py"], "dependent_tests": [],
                 "violations": [{"file": "a.py", "line": 2,
                                 "rule": "r", "message": "m"}]})

        with tempfile.TemporaryDirectory() as tmp:
            gen = self._gen(tmp, td_runner=td_runner,
                            thread="test-audit-fail")
            final = gen.run()
            self.assertFalse(final.get("valid"))
            self.assertNotIn("__interrupt__", final)
            joined = "\n".join(final.get("errors", []))
            self.assertIn("audit-code:a.py:2: r: m", joined)
            # audit-code failure blocks before audit-impact runs.
            self.assertNotIn("audit-impact", seen)

    def test_verify_scope_runs_with_spec(self):
        seen = []

        def td_runner(args):
            seen.append(args[0])
            return _gate_ok(args)

        with tempfile.TemporaryDirectory() as tmp:
            spec = os.path.join(tmp, "spec.md")
            with open(spec, "w", encoding="utf-8") as handle:
                handle.write("# spec\n")
            gen = auditor.AuditorGraph(
                tmp, audit_fn=lambda gate: "ok",
                td_runner=td_runner, thread_id="test-audit-spec",
                spec_file=spec)
            state = gen.run()
            self.assertIn("__interrupt__", state)
            self.assertIn("verify-scope", seen)

    def test_verdict_approved_on_approve(self):
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.jsonl")
            old = ledger.LEDGER
            ledger.LEDGER = ledger_path
            try:
                gen = self._gen(tmp, thread="test-audit-verdict")
                gen.run()
                final = gen.run(resume={"decision": "approve"})
            finally:
                ledger.LEDGER = old
            self.assertEqual(final.get("verdict", {}).get("status"),
                             "APPROVED")

    def test_approve_persists(self):
        import ledger
        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = os.path.join(tmp, "ledger.jsonl")
            old = ledger.LEDGER
            ledger.LEDGER = ledger_path
            try:
                gen = self._gen(tmp, thread="test-audit-ok")
                state = gen.run()
                self.assertIn("__interrupt__", state)
                final = gen.run(resume={"decision": "approve"})
                self.assertTrue(final.get("persisted"))
            finally:
                ledger.LEDGER = old
            with open(ledger_path, encoding="utf-8") as handle:
                entry = json.loads(
                    handle.read().strip().splitlines()[-1])
            self.assertEqual(entry.get("script"), "auditor.py")

    def test_reject_reruns_gate_then_persists(self):
        seen = []

        def td_runner(args):
            seen.append(1)
            return _gate_ok(args)

        with tempfile.TemporaryDirectory() as tmp:
            gen = self._gen(tmp, td_runner=td_runner,
                            thread="test-audit-reject")
            state = gen.run()
            state = gen.run(resume={"decision": "reject",
                                    "notes": "say more"})
            self.assertIn("__interrupt__", state)
            final = gen.run(resume={"decision": "approve"})
            self.assertTrue(final.get("persisted"))
            self.assertGreaterEqual(len(seen), 2)


class TestAskOnce(unittest.TestCase):
    """AuditorGraph.ask_once: the eval_ask live entry (issue #1139).

    stdlib-only: ask_investigator is stubbed (it pulls langchain at
    import) and the agent arrives via agent_factory, so no model or
    langchain is needed. backend.exec.engine imports for the
    single-sourced td shapes (same as tests/test_eval_ask.py).
    """

    def _stub_ask_module(self):
        import types

        mod = types.ModuleType("ask_investigator")
        for name in auditor.ASK_TOOL_NAMES:
            def _fn(*args, **kwargs):
                return ""
            _fn.__name__ = name
            setattr(mod, name, _fn)
        mod._repo = ""
        mod._checkout = ""
        return mod

    def _run(self, messages, repo="arii/remote-server",
             mod=None, factory=None):
        seen = {}
        fake_messages = messages

        def default_factory(tools, system):
            seen["tools"] = tools
            seen["system"] = system

            class FakeAgent:
                def invoke(self, payload, config=None):
                    seen["payload"] = payload
                    seen["config"] = config
                    return {"messages": fake_messages}

            return FakeAgent()

        agent_factory = (factory if factory is not None
                         else default_factory)
        old = sys.modules.get("ask_investigator")
        sys.modules["ask_investigator"] = (
            mod if mod is not None else self._stub_ask_module())
        try:
            gen = auditor.AuditorGraph("/tmp",
                                       thread_id="test-ask-unit")
            out = gen.ask_once("What is the state of PR #153?", repo,
                               agent_factory=agent_factory)
        finally:
            if old is None:
                del sys.modules["ask_investigator"]
            else:
                sys.modules["ask_investigator"] = old
        return out, seen

    def test_wires_twelve_ask_tools_and_ask_voice(self):
        out, seen = self._run([
            {"role": "ai", "content": "PR 153 is open."},
        ])
        self.assertEqual([t.__name__ for t in seen["tools"]],
                         list(auditor.ASK_TOOL_NAMES))
        self.assertEqual(len(seen["tools"]), 12)
        system = seen["system"]
        self.assertTrue(system.startswith("Repo: arii/remote-server. "))
        for shape in ("PROPOSE issue", "PROPOSE td", "PROPOSE dispatch",
                      "Allowed td commands (exact shapes):"):
            self.assertIn(shape, system)
        from backend.exec.engine import ALLOWED_TD_COMMANDS
        for shape in ALLOWED_TD_COMMANDS:
            self.assertIn(shape, system)
        self.assertEqual(seen["payload"],
                         {"messages": [{"role": "user",
                                         "content": "What is the state "
                                                    "of PR #153?"}]})
        self.assertEqual(seen["config"], {"recursion_limit": 20})
        self.assertEqual(out["reply"], "PR 153 is open.")
        self.assertEqual(out["tool_names"], [])

    def test_reply_is_last_answer_and_tools_listed(self):
        out, _ = self._run([
            {"role": "user", "content": "q"},
            {"role": "ai", "content": "",
             "tool_calls": [{"name": "grep_repo", "args": "{}"}]},
            {"type": "tool", "tool_call_id": "1", "name": "grep_repo",
             "content": "buttons/server.py"},
            {"role": "ai", "content": "The handler is in "
                                      "buttons/server.py."},
        ])
        self.assertEqual(out["reply"],
                         "The handler is in buttons/server.py.")
        self.assertEqual(out["tool_names"], ["grep_repo"])

    def test_propose_block_passes_through_as_reply(self):
        block = ("PROPOSE td\nCOMMAND: td gh read-pr-reviews --pr 153\n"
                 "END PROPOSE")
        out, _ = self._run([{"role": "ai", "content": block}])
        self.assertEqual(out["reply"], block)

    def test_bad_repo_raises_before_agent_build(self):
        with self.assertRaises(ValueError):
            self._run([{"role": "ai", "content": "x"}],
                      repo="not a repo")

    def test_globals_restored_on_success(self):
        stub = self._stub_ask_module()
        stub._repo, stub._checkout = "sentinel-r", "sentinel-c"
        self._run([{"role": "ai", "content": "ok"}], mod=stub)
        self.assertEqual((stub._repo, stub._checkout),
                         ("sentinel-r", "sentinel-c"))

    def test_globals_restored_on_exception(self):
        stub = self._stub_ask_module()
        stub._repo, stub._checkout = "sentinel-r", "sentinel-c"

        def factory(tools, system):
            class FakeAgent:
                def invoke(self, payload, config=None):
                    raise RuntimeError("tool boom")

            return FakeAgent()

        with self.assertRaises(RuntimeError):
            self._run([{"role": "ai", "content": "x"}], mod=stub,
                      factory=factory)
        self.assertEqual((stub._repo, stub._checkout),
                         ("sentinel-r", "sentinel-c"))

    def test_malformed_result_is_flagged(self):
        for bad in (["not", "a", "dict"],
                    {"no_messages": True},
                    {"messages": "nope"}):
            with self.subTest(bad=bad):
                def factory(tools, system, _bad=bad):
                    class FakeAgent:
                        def invoke(self, payload, config=None):
                            return _bad

                    return FakeAgent()

                old = sys.modules.get("ask_investigator")
                sys.modules["ask_investigator"] = (
                    self._stub_ask_module())
                try:
                    gen = auditor.AuditorGraph(
                        "/tmp", thread_id="test-ask-malformed")
                    with self.assertRaises(ValueError):
                        gen.ask_once("q", "arii/remote-server",
                                     agent_factory=factory)
                finally:
                    if old is None:
                        del sys.modules["ask_investigator"]
                    else:
                        sys.modules["ask_investigator"] = old

    def test_tool_names_match_live_module(self):
        # Drift guard (#1157): the wired names must exist as exports
        # on the real ask_investigator module. AST, not import: the
        # live module pulls langchain at top.
        import ast

        with open(os.path.join("deep-agent", "ask_investigator.py"),
                  encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        defs = {node.name for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef,
                                     ast.AsyncFunctionDef))}
        missing = [n for n in auditor.ASK_TOOL_NAMES if n not in defs]
        self.assertEqual(missing, [])

    def test_helpers_tolerate_object_messages(self):
        class Msg:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        ai = Msg(content="hi",
                 tool_calls=[{"name": "pr_state", "args": "{}"}])
        self.assertEqual(
            [c["name"] for c in auditor._message_tool_calls(ai)],
            ["pr_state"])
        self.assertEqual(auditor._message_text(ai), "hi")
        self.assertFalse(auditor._is_tool_message(ai))
        tool = Msg(content="row", tool_call_id="1")
        self.assertTrue(auditor._is_tool_message(tool))
        self.assertEqual(
            auditor._message_text(Msg(content=[{"text": "a"},
                                               "b"])), "a b")


class TestAskMain(unittest.TestCase):
    """auditor.main driver: argv validation, print contract, ledger."""

    def _stub_main(self, ask_result=None, ask_error=None):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        import ledger

        def run(argv):
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "ledger.jsonl")
                old_ledger = ledger.LEDGER
                ledger.LEDGER = path
                orig = auditor.AuditorGraph.ask_once

                def fake(self, prompt, repo=""):
                    if ask_error is not None:
                        raise ask_error
                    return ask_result

                auditor.AuditorGraph.ask_once = fake
                buf = io.StringIO()
                try:
                    with redirect_stdout(buf), redirect_stderr(io.StringIO()):
                        code = auditor.main(argv)
                finally:
                    auditor.AuditorGraph.ask_once = orig
                    ledger.LEDGER = old_ledger
                rows = []
                if os.path.exists(path):
                    with open(path, encoding="utf-8") as handle:
                        rows = [json.loads(line) for line in handle
                                if line.strip()]
                return code, buf.getvalue(), rows

        return run

    def _result(self):
        return {
            "reply": "The handler is in buttons/server.py.",
            "tool_names": ["grep_repo"],
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "ai", "content": "",
                 "tool_calls": [{"name": "grep_repo",
                                "args": "{}"}]},
                {"type": "tool", "tool_call_id": "1",
                 "name": "grep_repo", "content": "buttons/server.py"},
                {"role": "ai", "content": "The handler is in "
                                          "buttons/server.py."},
            ],
            "model_label": "ollama:qwen3:8b",
        }

    def test_print_contract_and_ledger_row(self):
        import base64

        run = self._stub_main(ask_result=self._result())
        b64 = base64.b64encode(b"which file?").decode()
        code, out, rows = run(
            ["auditor.py", "arii/remote-server", "test-ask-x", b64])
        self.assertEqual(code, 0)
        for needle in ("REPORT (authoritative, from tool results):",
                       "buttons/server.py",
                       "tool_calls_made: 1",
                       "FINAL: The handler is in buttons/server.py."):
            self.assertIn(needle, out)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["script"], "auditor.py")
        self.assertEqual(row["thread_id"], "test-ask-x")
        self.assertEqual(
            [c["name"] for c in row["tool_calls"]], ["grep_repo"])
        self.assertFalse(row["metered"])

    def test_argv_validation(self):
        run = self._stub_main(ask_result=self._result())
        self.assertEqual(run(["auditor.py"])[0], 2)
        self.assertEqual(
            run(["auditor.py", "not a repo", "t", "eA=="])[0], 2)
        self.assertEqual(
            run(["auditor.py", "arii/remote-server", "t", "!!!"])[0],
            2)

    def test_thread_defaults_when_omitted(self):
        import base64

        run = self._stub_main(ask_result=self._result())
        b64 = base64.b64encode(b"which file?").decode()
        code, _, rows = run(["auditor.py", "arii/remote-server", b64])
        self.assertEqual(code, 0)
        self.assertEqual(rows[0]["thread_id"],
                         "test-ask-arii-remote-server")

    def test_model_unavailable_is_sorry_invalid(self):
        run = self._stub_main(
            ask_error=auditor.ModelUnavailableError(
                "Ollama unreachable at x"))
        code, out, rows = run(
            ["auditor.py", "arii/remote-server", "test-ask-y", "Zg=="])
        self.assertEqual(code, 1)
        self.assertIn("no model is available", out)
        self.assertEqual(rows, [])

    def test_tool_runtime_error_is_failing_case_not_invalid(self):
        # #1157: a tool-impl RuntimeError must surface as a failing
        # case (clean error + error row), never as gate-passing
        # invalid. Pinned through the real check_case verdict.
        import eval_ask

        run = self._stub_main(
            ask_error=RuntimeError("tool boom"))
        code, out, rows = run(
            ["auditor.py", "arii/remote-server", "test-ask-y", "Zg=="])
        self.assertEqual(code, 1)
        self.assertIn("error: tool boom", out)
        self.assertNotIn("Traceback", out)
        self.assertEqual(len(rows), 1)
        self.assertIn("boom", rows[0]["extra"]["error"])
        case = {"name": "t", "prompt": "p", "tools": [],
                "anchor": re.compile(r"x")}
        verdict, reasons = eval_ask.check_case(
            case, code, out, "", [], True, False)
        self.assertEqual(verdict, "fail", reasons)

    def test_unexpected_error_records_row_and_raises(self):
        import ledger as ledger_mod

        recorded = []
        orig_record = ledger_mod.record

        def spy(*args, **kwargs):
            recorded.append((args, kwargs))
            return orig_record(*args, **kwargs)

        ledger_mod.record = spy
        run = self._stub_main(ask_error=ValueError("boom"))
        try:
            with self.assertRaises(ValueError):
                run(["auditor.py", "arii/remote-server", "test-ask-z",
                     "Zg=="])
        finally:
            ledger_mod.record = orig_record
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0][0][0], "auditor.py")
        self.assertIn("boom", recorded[0][1]["extra"]["error"])


if __name__ == "__main__":
    unittest.main()
