"""coder slice tests. Part A (system python, stdlib-only import):
argv parsing, run_check allowlist, system prompt builder, verify
gate units (rounds env, trace predicate, re-invoke loop, marking).
Part B (deep-agent venv): FilesystemBackend root enforcement +
tool wiring, run_coder verify gate end-to-end with fakes.
Run: python3 tests/test_coder.py  (A only under system python)
     deep-agent/.venv/bin/python tests/test_coder.py  (A + B)"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import coder

try:
    from deepagents.backends import FilesystemBackend  # noqa: F401
    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


class TestArgv(unittest.TestCase):
    def test_spawner_shape(self):
        args = coder.parse_args(
            ["-p", "--system-prompt", "CTX", "--model", "m1", "do it"])
        self.assertTrue(args.p)
        self.assertEqual(args.system_prompt, "CTX")
        self.assertEqual(args.model, "m1")
        self.assertEqual(args.brief, "do it")
        self.assertIsNone(args.worktree)

    def test_direct_shape(self):
        args = coder.parse_args(
            ["--worktree", "/tmp/wt", "--thread", "t1", "fix typo"])
        self.assertEqual(args.worktree, "/tmp/wt")
        self.assertEqual(args.thread, "t1")

    def test_brief_required(self):
        with self.assertRaises(SystemExit):
            coder.parse_args(["-p"])


class TestAllowlist(unittest.TestCase):
    def test_allowed_prefixes(self):
        self.assertTrue(coder.check_allowed(
            ["python3", "-m", "pytest", "tests/", "-q"]))
        self.assertTrue(coder.check_allowed(
            ["pnpm", "exec", "vitest", "run", "x.test.ts"]))
        self.assertTrue(coder.check_allowed(
            ["git", "diff", "--stat"]))

    def test_direct_test_file(self):
        self.assertTrue(coder.check_allowed(
            ["python3", "tests/test_s5tools.py"]))
        self.assertTrue(coder.check_allowed(
            ["python3", "tests/test_x.py", "-v"]))

    def test_rejected(self):
        self.assertFalse(coder.check_allowed([]))
        self.assertFalse(coder.check_allowed(
            ["python3", "tests/helper.py"]))
        self.assertFalse(coder.check_allowed(
            ["python3", "tests/../evil.py"]))
        self.assertFalse(coder.check_allowed(["rm", "-rf", "."]))
        self.assertFalse(coder.check_allowed(["git", "push"]))
        self.assertFalse(coder.check_allowed(["curl", "http://x"]))
        self.assertFalse(coder.check_allowed(
            ["python3", "-c", "import os"]))
        # Prefix tricks must not smuggle through.
        self.assertFalse(coder.check_allowed(
            ["python3", "-m", "pytester"]))
        self.assertFalse(coder.check_allowed(["pytestx"]))
        # `npx tsc` fetches the `tsc` squat placeholder, not TypeScript.
        self.assertFalse(coder.check_allowed(["npx", "tsc", "--noEmit"]))
        self.assertFalse(coder.check_allowed(["npx", "tsc"]))


class TestSystemPrompt(unittest.TestCase):
    def test_rules_always_present(self):
        sys_prompt = coder.build_system("/wt", "")
        self.assertIn("/wt", sys_prompt)
        self.assertIn("Do NOT commit", sys_prompt)
        self.assertIn("run_check", sys_prompt)

    def test_context_prepended(self):
        sys_prompt = coder.build_system("/wt", "CONTRACT")
        self.assertTrue(sys_prompt.startswith("CONTRACT"))
        self.assertIn("Do NOT commit", sys_prompt)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestBackendRoot(unittest.TestCase):
    def test_write_read_round_trip(self):
        import tempfile

        from deepagents.backends import FilesystemBackend

        with tempfile.TemporaryDirectory() as d:
            backend = FilesystemBackend(root_dir=d, virtual_mode=True)
            backend.write("/notes.txt", "hello")
            self.assertEqual(
                backend.read("/notes.txt").file_data["content"], "hello")
            self.assertTrue(os.path.isfile(
                os.path.join(d, "notes.txt")))

    def test_escapes_rejected(self):
        import tempfile

        from deepagents.backends import FilesystemBackend

        with tempfile.TemporaryDirectory() as d:
            backend = FilesystemBackend(root_dir=d, virtual_mode=True)
            for bad in ("/../escape.txt", "/sub/../../escape.txt"):
                with self.assertRaises(Exception, msg=bad):
                    backend.write(bad, "x")
                with self.assertRaises(Exception, msg=bad):
                    backend.read(bad)
            # Absolute paths resolve virtually: contained, never host.
            backend.write("/tmp/coder-escape-probe.txt", "x")
            self.assertTrue(os.path.isfile(os.path.join(
                d, "tmp", "coder-escape-probe.txt")))
            self.assertFalse(os.path.exists(
                "/tmp/coder-escape-probe.txt"))


class TestVerifyRounds(unittest.TestCase):
    def _rounds(self, value):
        with mock.patch.dict(os.environ):
            if value is None:
                os.environ.pop("CODER_VERIFY_ROUNDS", None)
            else:
                os.environ["CODER_VERIFY_ROUNDS"] = value
            return coder.verify_rounds()

    def test_default_two(self):
        self.assertEqual(self._rounds(None), 2)

    def test_zero_disables(self):
        self.assertEqual(self._rounds("0"), 0)

    def test_custom_bound(self):
        self.assertEqual(self._rounds("5"), 5)

    def test_negative_clamps_to_zero(self):
        self.assertEqual(self._rounds("-3"), 0)

    def test_invalid_falls_back_to_default(self):
        self.assertEqual(self._rounds("many"), 2)


class TestTraceHasPassingCheck(unittest.TestCase):
    def _trace(self, lines):
        fh = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        with fh:
            for line in lines:
                fh.write(line if isinstance(line, str)
                         else json.dumps(line))
                fh.write("\n")
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def _call(self, name="run_check", preview="exit=0\nok"):
        return {"type": "tool_call", "name": name,
                "args_preview": "pytest", "duration_s": 0.1,
                "ok": True, "result_preview": preview, "ts": 1.0}

    def test_passing_check(self):
        path = self._trace([
            {"type": "run_start", "script": "coder.py", "ts": 1.0},
            self._call(),
        ])
        self.assertTrue(coder.trace_has_passing_check(path))

    def test_exact_exit_zero_no_output(self):
        path = self._trace([self._call(preview="exit=0")])
        self.assertTrue(coder.trace_has_passing_check(path))

    def test_failing_exit_does_not_verify(self):
        path = self._trace([self._call(preview="exit=1\nboom")])
        self.assertFalse(coder.trace_has_passing_check(path))

    def test_rejected_result_does_not_verify(self):
        path = self._trace(
            [self._call(preview="REJECTED: command must start with one of: x")])
        self.assertFalse(coder.trace_has_passing_check(path))

    def test_no_run_check_calls(self):
        path = self._trace([
            {"type": "run_start", "script": "coder.py", "ts": 1.0},
            {"type": "run_end", "duration_s": 1.0, "ok": True, "ts": 2.0},
        ])
        self.assertFalse(coder.trace_has_passing_check(path))

    def test_other_tool_exit_zero_ignored(self):
        path = self._trace([self._call(name="write_file")])
        self.assertFalse(coder.trace_has_passing_check(path))

    def test_error_shape_event_ignored(self):
        path = self._trace([{"type": "tool_call", "name": "run_check",
                             "args_preview": "x", "duration_s": 0.1,
                             "ok": False, "error": "boom", "ts": 1.0}])
        self.assertFalse(coder.trace_has_passing_check(path))

    def test_garbage_lines_skipped(self):
        path = self._trace(["not json", "[1, 2]", "{oops",
                            self._call()])
        self.assertTrue(coder.trace_has_passing_check(path))

    def test_only_garbage(self):
        path = self._trace(["not json", ""])
        self.assertFalse(coder.trace_has_passing_check(path))

    def test_missing_file_is_unverified(self):
        self.assertFalse(coder.trace_has_passing_check(
            "/tmp/does-not-exist-646.jsonl"))

    def test_none_path_is_unverified(self):
        self.assertFalse(coder.trace_has_passing_check(None))


class TestRunWithVerify(unittest.TestCase):
    def test_pass_first_time_single_invoke(self):
        calls = []

        def initial():
            calls.append("initial")
            return "r1"

        def reinvoke():
            calls.append("re")
            return "r2"

        result, rounds, verified = coder.run_with_verify(
            initial, reinvoke, lambda: True, 2)
        self.assertEqual((result, rounds, verified), ("r1", 0, True))
        self.assertEqual(calls, ["initial"])

    def test_trip_then_pass_reinvokes_once(self):
        calls = []
        verdicts = [False, True]

        def initial():
            calls.append("initial")
            return "r1"

        def reinvoke():
            calls.append("re")
            return "r2"

        result, rounds, verified = coder.run_with_verify(
            initial, reinvoke, lambda: verdicts.pop(0), 2)
        self.assertEqual((result, rounds, verified), ("r2", 1, True))
        self.assertEqual(calls, ["initial", "re"])

    def test_bounded_reinvoke(self):
        calls = []

        def reinvoke():
            calls.append("re")
            return "r%d" % len(calls)

        result, rounds, verified = coder.run_with_verify(
            lambda: "r0", reinvoke, lambda: False, 2)
        self.assertEqual((result, rounds, verified), ("r2", 2, False))
        self.assertEqual(calls, ["re", "re"])

    def test_zero_rounds_never_reinvokes(self):
        def reinvoke():
            raise AssertionError("must not re-invoke when disabled")

        result, rounds, verified = coder.run_with_verify(
            lambda: "r0", reinvoke, lambda: False, 0)
        self.assertEqual((result, rounds, verified), ("r0", 0, False))


class TestVerifyMarking(unittest.TestCase):
    def test_prefix(self):
        self.assertEqual(coder.mark_unverified("did x"),
                         "[unverified] did x")

    def test_empty_summary(self):
        self.assertEqual(coder.mark_unverified(""), "[unverified]")

    def test_nudge_text(self):
        self.assertIn("run_check", coder.VERIFY_NUDGE)
        self.assertIn("do not finish without a passing run",
                      coder.VERIFY_NUDGE)


class TestExtractReply(unittest.TestCase):
    class _Msg:
        def __init__(self, content):
            self.content = content

    def test_last_message_content(self):
        result = {"messages": [self._Msg("first"), self._Msg(" last ")]}
        self.assertEqual(coder.extract_reply(result), "last")

    def test_empty_result(self):
        self.assertEqual(coder.extract_reply({}), "")
        self.assertEqual(coder.extract_reply(None), "")

    def test_non_string_content(self):
        result = {"messages": [self._Msg({"text": "hi"})]}
        self.assertIn("hi", coder.extract_reply(result))


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestRunCoderVerifyGate(unittest.TestCase):
    """run_coder with a fake agent + real trace recorder (tmp trace)."""

    def _run(self, script, env_value="unset"):
        import deep_chat
        import observe

        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        box = []

        class FakeMsg:
            def __init__(self, content):
                self.content = content

        class FakeAgent:
            def __init__(self, recorder):
                self.invokes = []
                self.configs = []
                self._recorder = recorder

            def invoke(self, payload, config=None):
                n = len(self.invokes)
                self.invokes.append(payload)
                self.configs.append(config)
                if n < len(script) and script[n] is not None:
                    self._recorder.tool_call(
                        "run_check", "python3 -m pytest", 0.1, True,
                        result=script[n])
                return {"messages": [
                    FakeMsg("summary after invoke %d" % (n + 1))]}

        def fake_build_agent(worktree, system, model=None, recorder=None,
                             checkpointer=None):
            agent = FakeAgent(recorder)
            box.append(agent)
            return agent

        with (mock.patch.object(
                coder, "build_model",
                return_value=(object(), {"provider": "fake",
                                        "model": "fake"})),
              mock.patch.object(coder, "build_agent",
                                side_effect=fake_build_agent),
              mock.patch.object(deep_chat, "open_checkpointer",
                                return_value=(None, False, object())),
              mock.patch.object(observe, "default_trace_path",
                                return_value=tmp.name),
              mock.patch.dict(os.environ)):
            if env_value == "unset":
                os.environ.pop("CODER_VERIFY_ROUNDS", None)
            else:
                os.environ["CODER_VERIFY_ROUNDS"] = env_value
            out = coder.run_coder("/wt", "do the thing", "SYS",
                                  "thread-1", 30)
        return out, box[0]

    def test_gate_pass_single_invoke(self):
        (summary, _u, _c, _s, label, _t, verified, rounds), agent = \
            self._run(["exit=0\nok"])
        self.assertEqual(len(agent.invokes), 1)
        self.assertTrue(verified)
        self.assertEqual(rounds, 0)
        self.assertNotIn("[unverified]", summary)
        self.assertEqual(label, "fake:fake")

    def test_gate_trip_reinvokes_with_nudge(self):
        (summary, _u, _c, _s, _l, _t, verified, rounds), agent = \
            self._run([None, "exit=0\nok"])
        self.assertEqual(len(agent.invokes), 2)
        self.assertTrue(verified)
        self.assertEqual(rounds, 1)
        first, second = agent.invokes
        self.assertEqual(first["messages"][0].content, "do the thing")
        self.assertEqual(second["messages"][0].content,
                         coder.VERIFY_NUDGE)
        tids = [c["configurable"]["thread_id"] for c in agent.configs]
        self.assertEqual(tids, ["thread-1", "thread-1"])
        self.assertNotIn("[unverified]", summary)

    def test_give_up_marks_unverified(self):
        (summary, _u, _c, _s, _l, _t, verified, rounds), agent = \
            self._run([None, None, None])
        self.assertEqual(len(agent.invokes), 3)
        self.assertFalse(verified)
        self.assertEqual(rounds, 2)
        self.assertTrue(summary.startswith("[unverified] "))

    def test_failing_check_never_verifies(self):
        (_s, _u, _c, _st, _l, _t, verified, rounds), agent = \
            self._run(["exit=1\nboom"] * 3)
        self.assertEqual(len(agent.invokes), 3)
        self.assertFalse(verified)
        self.assertEqual(rounds, 2)

    def test_zero_rounds_preserves_legacy(self):
        (summary, _u, _c, _s, _l, _t, verified, rounds), agent = \
            self._run([None], env_value="0")
        self.assertEqual(len(agent.invokes), 1)
        self.assertFalse(verified)
        self.assertEqual(rounds, 0)
        self.assertNotIn("[unverified]", summary)


if __name__ == "__main__":
    unittest.main()
