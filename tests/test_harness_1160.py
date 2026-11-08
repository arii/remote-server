"""Regression tests for issue #1160 harness gaps 1-3 + review ledgering.

Gap 1: run_check/verify commands run with the checkout on
PYTHONPATH (the allowlist forbids env prefixes, so the harness
must set it). Gap 2: coder --resume approve works cross-process
when the checkpointer is memory-only, continuing from the
ledgered plan. Gap 3: repeated identical failing checks are
detected (dither guard) and the verify loop stops early.
Review: AuditorGraph review usage is captured for the persist row.

Stdlib-only (venv-only imports are faked). Run:
python3 -m unittest tests.test_harness_1160
"""
import json
import os
import sys
import tempfile
import time
import types
import unittest
from types import SimpleNamespace
from unittest import mock

import auditor
import coder
import ledger
import s5tools


def _install_fake_langchain(test):
    """Fake langchain_core.messages + .tools for stdlib testing."""
    class HumanMessage(SimpleNamespace):
        def __init__(self, content=""):
            super().__init__(content=content)

    class AIMessage(SimpleNamespace):
        def __init__(self, content=""):
            super().__init__(content=content)

    lc = types.ModuleType("langchain_core")
    msgs = types.ModuleType("langchain_core.messages")
    msgs.HumanMessage = HumanMessage
    msgs.AIMessage = AIMessage
    tools_mod = types.ModuleType("langchain_core.tools")

    def tool(fn):
        fn.description = fn.__doc__ or ""
        return fn

    tools_mod.tool = tool
    lc.messages = msgs
    lc.tools = tools_mod
    saved = {}
    for name, mod in (("langchain_core", lc),
                      ("langchain_core.messages", msgs),
                      ("langchain_core.tools", tools_mod)):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod

    def _restore():
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    test.addCleanup(_restore)
    return HumanMessage, AIMessage


class TestCheckEnv(unittest.TestCase):
    def test_checkout_leads_pythonpath(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(s5tools.check_env("/repo"),
                             {"PYTHONPATH": "/repo"})

    def test_existing_pythonpath_kept_behind(self):
        with mock.patch.dict(os.environ, {"PYTHONPATH": "/a:/b"}):
            self.assertEqual(s5tools.check_env("/repo"),
                             {"PYTHONPATH": "/repo:/a:/b"})

    def test_run_repo_command_sets_pythonpath(self):
        with tempfile.TemporaryDirectory() as checkout:
            os.makedirs(os.path.join(checkout, "tests"))
            probe = os.path.join(checkout, "tests", "test_pp_probe.py")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("import os\n"
                        "print(os.environ.get('PYTHONPATH', ''))\n")
            out = s5tools.run_repo_command(
                checkout, "python3 tests/test_pp_probe.py")
            self.assertTrue(out.startswith("exit=0\n"))
            self.assertIn(checkout, out.splitlines()[1])

    def test_env_prefix_commands_stay_rejected(self):
        # The allowlist forbids env prefixes — the reason the
        # harness itself must set PYTHONPATH (gap 1).
        out = s5tools.run_repo_command(
            "/tmp", "PYTHONPATH=/tmp python3 -m pytest")
        self.assertTrue(out.startswith("REJECTED:"))


class TestRunCheckTool(unittest.TestCase):
    def test_tool_sets_pythonpath_and_dither_notice(self):
        _install_fake_langchain(self)
        with tempfile.TemporaryDirectory() as worktree:
            os.makedirs(os.path.join(worktree, "tests"))
            with open(os.path.join(worktree, "tests",
                                   "test_env_probe.py"), "w",
                      encoding="utf-8") as f:
                f.write("import os\n"
                        "print(os.environ.get('PYTHONPATH', ''))\n")
            with open(os.path.join(worktree, "tests",
                                   "test_fail_probe.py"), "w",
                      encoding="utf-8") as f:
                f.write("import sys\nsys.exit(3)\n")
            run_check = coder.build_check_tool(worktree)
            ok_out = run_check("python3 tests/test_env_probe.py")
            self.assertTrue(ok_out.startswith("exit=0\n"))
            self.assertIn(worktree, ok_out)
            failing = "python3 tests/test_fail_probe.py"
            first = run_check(failing)
            self.assertTrue(first.startswith("exit=3\n"))
            self.assertNotIn("DITHERING", first)
            fifth = first
            for _ in range(4):
                fifth = run_check(failing)
            self.assertIn("DITHERING", fifth)
            # A different command resets the streak.
            other = run_check("git status")
            self.assertNotIn("DITHERING", other)


def _check_event(command, code):
    return {"type": "tool_call", "name": "run_check",
            "args_preview": command,
            "result_preview": "exit=%d\nboom" % code,
            "ok": True}


class TestDitherGuard(unittest.TestCase):
    def test_find_dithered_command(self):
        cmd = "python3 -m pytest"
        self.assertEqual(
            coder.find_dithered_command([(cmd, 1)] * 5), cmd)
        self.assertEqual(
            coder.find_dithered_command([(cmd, 1)] * 7), cmd)
        self.assertIsNone(coder.find_dithered_command([(cmd, 1)] * 4))
        self.assertIsNone(
            coder.find_dithered_command([(cmd, 1)] * 4 + [(cmd, 0)]))
        self.assertIsNone(
            coder.find_dithered_command([(cmd, 1)] * 4 + [("git log", 1)]))
        self.assertIsNone(coder.find_dithered_command([]))
        self.assertIsNone(coder.find_dithered_command(None))
        self.assertIsNone(coder.find_dithered_command("junk"))

    def test_trace_has_dither(self):
        cmd = "python3 -m pytest"
        with tempfile.TemporaryDirectory() as d:
            spinning = os.path.join(d, "spin.jsonl")
            with open(spinning, "w", encoding="utf-8") as f:
                for _ in range(5):
                    f.write(json.dumps(_check_event(cmd, 1)) + "\n")
            self.assertTrue(coder.trace_has_dither(spinning))
            progressing = os.path.join(d, "prog.jsonl")
            with open(progressing, "w", encoding="utf-8") as f:
                for _ in range(3):
                    f.write(json.dumps(_check_event(cmd, 1)) + "\n")
                f.write(json.dumps({"type": "tool_call",
                                    "name": "edit_file",
                                    "args_preview": "a.py",
                                    "ok": True}) + "\n")
                for _ in range(4):
                    f.write(json.dumps(_check_event(cmd, 1)) + "\n")
            # An edit between checks breaks the streak: no dither.
            self.assertFalse(coder.trace_has_dither(progressing))
            self.assertFalse(
                coder.trace_has_dither(os.path.join(d, "missing.jsonl")))

    def test_run_with_verify_stops_on_dither(self):
        calls = []

        def initial():
            return "r1"

        def reinvoke():
            calls.append(1)
            return "r2"

        result, rounds, verified = coder.run_with_verify(
            initial, reinvoke, lambda: False, 2,
            dithered=lambda: True)
        self.assertEqual((result, rounds, verified), ("r1", 0, False))
        self.assertEqual(calls, [])

    def test_run_with_verify_reprompts_without_dither(self):
        calls = []

        def reinvoke():
            calls.append(1)
            return "r2"

        result, rounds, verified = coder.run_with_verify(
            lambda: "r1", reinvoke, lambda: False, 2,
            dithered=lambda: False)
        self.assertEqual((result, rounds, verified), ("r2", 2, False))
        self.assertEqual(len(calls), 2)

    def test_run_with_verify_unchanged_without_callback(self):
        result, rounds, verified = coder.run_with_verify(
            lambda: "r1", lambda: "r2", lambda: True, 2)
        self.assertEqual((result, rounds, verified), ("r1", 0, True))


class TestResumeFallback(unittest.TestCase):
    def _plan_ledger(self, directory, thread, worktree):
        path = os.path.join(directory, "ledger.jsonl")
        row = {"kind": "deep-agent-run", "ts": time.time(),
               "script": "coder.py", "model": "nebius:m",
               "thread_id": thread, "duration_s": 1.0,
               "tool_calls": [], "usage": {},
               "cost_usd": None, "metered": True,
               "final_preview": "",
               "extra": {"coder_phase": "plan_pending",
                         "brief": "do it", "worktree": worktree,
                         "system": "", "plan": "P1", "trace": ""}}
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return path

    def _run_resume(self, persistent):
        _install_fake_langchain(self)
        import deep_chat

        with tempfile.TemporaryDirectory() as d:
            worktree = os.path.join(d, "wt")
            os.makedirs(worktree)
            ledger_path = self._plan_ledger(d, "thr-1", worktree)
            seen = {}
            with mock.patch.dict(os.environ,
                                 {"DEEP_AGENT_STATE_DIR": d}):
                with mock.patch.object(ledger, "LEDGER", ledger_path):
                    with mock.patch.object(
                            coder, "build_model",
                            return_value=(object(),
                                          {"provider": "nebius",
                                           "model": "m"})):
                        with mock.patch.object(
                                coder, "build_plan_agent",
                                return_value=object()):
                            with mock.patch.object(
                                    deep_chat, "open_checkpointer",
                                    return_value=(None, persistent,
                                                  None)):
                                with mock.patch.object(
                                        deep_chat, "_pending_interrupts",
                                        return_value=[]):
                                    def _finish(wt, brief, system,
                                                thread, timeout,
                                                extra=None):
                                        seen["extra"] = extra
                                        return 0

                                    with mock.patch.object(
                                            coder, "_implement_and_finish",
                                            side_effect=_finish):
                                        rc = coder._cmd_resume(
                                            worktree, "thr-1", "approve")
            return rc, seen

    def test_cross_process_resume_continues_from_ledger(self):
        rc, seen = self._run_resume(persistent=False)
        self.assertEqual(rc, 0)
        self.assertTrue(seen["extra"].get("ledger_resume"))
        self.assertEqual(seen["extra"].get("coder_phase"), "implement")

    def test_persistent_no_pending_still_fails_closed(self):
        rc, seen = self._run_resume(persistent=True)
        self.assertEqual(rc, 1)
        self.assertEqual(seen, {})


def _usage_msg(n_in, n_out):
    return {"usage_metadata": {"input_tokens": n_in,
                               "output_tokens": n_out,
                               "total_tokens": n_in + n_out}}


class TestReviewUsage(unittest.TestCase):
    def test_record_review_usage_sums_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            gen = auditor.AuditorGraph(
                tmp, audit_fn=lambda gate: "findings",
                thread_id="test-review-usage")
            self.assertIsNone(gen.last_review_usage)
            total = gen.record_review_usage(
                [_usage_msg(10, 5), _usage_msg(3, 7), {"n": 1}])
            self.assertEqual(total, {"input_tokens": 13,
                                     "output_tokens": 12,
                                     "total_tokens": 25})
            self.assertEqual(gen.last_review_usage, total)

    def test_review_spend_prices_on_persist_shape(self):
        # The persist row shape (bare carded model + nebius
        # provider + review usage) meters real spend.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ledger.jsonl")
            open(path, "w").close()
            with mock.patch.object(ledger, "LEDGER", path):
                entry = ledger.record(
                    "auditor.py", "zai-org/GLM-5.3-Flash",
                    "audit-real-1", [], time.time(),
                    extra={"valid": True},
                    usage={"input_tokens": 2000, "output_tokens": 1000,
                           "total_tokens": 3000},
                    provider="nebius")
                self.assertTrue(entry["metered"])
                self.assertAlmostEqual(entry["cost_usd"], 0.0008)


if __name__ == "__main__":
    unittest.main()
