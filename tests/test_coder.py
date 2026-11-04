"""coder slice tests. Part A (system python, stdlib-only import):
argv parsing, run_check allowlist, system prompt builder, verify
gate units (rounds env, trace predicate, re-invoke loop, marking),
bounded-timeout units (fast/error/hung daemon worker),
plan bar units (file tokens, needs_plan, plan validation, ledger
lookup, staleness) + main branching with faked phases.
Part B (deep-agent venv): FilesystemBackend root enforcement +
tool wiring, run_coder verify gate end-to-end with fakes, plan
agent wiring, plan-and-pause + resume paths with fakes.
Run: python3 tests/test_coder.py  (A only under system python)
     deep-agent/.venv/bin/python tests/test_coder.py  (A + B)"""
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
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


class TestRunBounded(unittest.TestCase):
    """run_bounded: daemon-thread wall timeout (#514). Stdlib-only."""

    def test_fast_returns_value(self):
        self.assertEqual(coder.run_bounded(lambda: 42, 5), 42)
        self.assertIsNone(coder.run_bounded(lambda: None, 5))

    def test_error_propagates(self):
        def boom():
            raise ValueError("kaput")

        with self.assertRaises(ValueError):
            coder.run_bounded(boom, 5)

    def test_hung_raises_timeout_promptly(self):
        stuck = threading.Event()
        before = set(threading.enumerate())
        start = time.time()
        with self.assertRaises(TimeoutError) as ctx:
            coder.run_bounded(lambda: stuck.wait(120), 1)
        elapsed = time.time() - start
        self.assertIn("timed out after 1s", str(ctx.exception))
        self.assertLess(elapsed, 15)
        # Process exit is proven: every surviving new worker is a
        # daemon, so no join can hang the interpreter or the Sorry
        # path when the worker is stuck.
        survivors = [t for t in threading.enumerate() if t not in before]
        self.assertTrue(survivors)
        for thread in survivors:
            self.assertTrue(thread.daemon, thread.name)


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
                             checkpointer=None, spec=None):
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

    def test_hung_invoke_times_out_and_closes_trace(self):
        import deep_chat
        import observe

        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)

        class HungAgent:
            def invoke(self, payload, config=None):
                time.sleep(120)
                return {"messages": []}

        with (mock.patch.object(
                coder, "build_model",
                return_value=(object(), {"provider": "fake",
                                        "model": "fake"})),
              mock.patch.object(coder, "build_agent",
                                return_value=HungAgent()),
              mock.patch.object(deep_chat, "open_checkpointer",
                                return_value=(None, False, object())),
              mock.patch.object(observe, "default_trace_path",
                                return_value=tmp.name),
              mock.patch.dict(os.environ, {"CODER_VERIFY_ROUNDS": "0"})):
            start = time.time()
            with self.assertRaises(RuntimeError) as ctx:
                coder.run_coder("/wt", "do the thing", "SYS",
                                "thread-hung", 1)
            elapsed = time.time() - start
        self.assertIn("timed out after 1s", str(ctx.exception))
        self.assertLess(elapsed, 15)
        with open(tmp.name, encoding="utf-8") as fh:
            trace = fh.read()
        self.assertIn('"type": "run_end"', trace)
        self.assertIn('"ok": false', trace)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestImplementTimeoutLedger(unittest.TestCase):
    """A timed-out run is ledgered with the error + Sorry exit 1."""

    def test_timeout_ledgered_with_sorry(self):
        import ledger

        ledger_fh = tempfile.NamedTemporaryFile(suffix=".jsonl",
                                                delete=False)
        ledger_fh.close()
        self.addCleanup(os.unlink, ledger_fh.name)
        buf = io.StringIO()
        with (mock.patch.object(ledger, "LEDGER", ledger_fh.name),
              mock.patch.object(
                  coder, "run_coder",
                  side_effect=RuntimeError(
                      "coder timed out after 1s")),
              redirect_stdout(buf)):
            code = coder._implement_and_finish(
                "/wt", "do the thing", "SYS", "t1", 1)
        self.assertEqual(code, 1)
        self.assertIn("Sorry", buf.getvalue())
        with open(ledger_fh.name, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertIn("timed out", rows[0]["extra"]["error"])


class TestFilesNamed(unittest.TestCase):
    def test_multi(self):
        self.assertEqual(coder.files_named("update a.py and b.py"),
                         ["a.py", "b.py"])

    def test_single(self):
        self.assertEqual(coder.files_named("fix typo in server.py"),
                         ["server.py"])

    def test_none(self):
        self.assertEqual(coder.files_named("refactor the auth flow"), [])

    def test_dedup_sorted(self):
        self.assertEqual(coder.files_named("b.py then a.py then b.py"),
                         ["a.py", "b.py"])

    def test_paths_with_slash(self):
        self.assertEqual(
            coder.files_named("touch backend/api/x.py"),
            ["backend/api/x.py"])

    def test_urls_ignored(self):
        self.assertEqual(
            coder.files_named("see https://example.com/x.py for docs"),
            [])

    def test_version_numbers_ignored(self):
        self.assertEqual(coder.files_named("ship v1.2 now"), [])

    def test_prose_abbreviations_ignored(self):
        self.assertEqual(
            coder.files_named("fix it, e.g. the crash, i.e. null"), [])

    def test_multiple_texts(self):
        self.assertEqual(coder.files_named("edit a.py", "scope: b.py"),
                         ["a.py", "b.py"])

    def test_non_string_ignored(self):
        self.assertEqual(coder.files_named(None, 123), [])


class TestNeedsPlan(unittest.TestCase):
    def _needs(self, brief, context="", env="unset"):
        with mock.patch.dict(os.environ):
            if env == "unset":
                os.environ.pop("CODER_PLAN", None)
            else:
                os.environ["CODER_PLAN"] = env
            return coder.needs_plan(brief, context)

    def test_multi_file_plans(self):
        self.assertTrue(self._needs("update a.py and b.py"))

    def test_single_file_skips(self):
        self.assertFalse(self._needs("fix typo in a.py"))

    def test_no_files_skips(self):
        self.assertFalse(self._needs("refactor the auth flow"))

    def test_context_counts(self):
        self.assertTrue(self._needs("implement the scoped task",
                                    "scope: a.py b.py"))

    def test_always_forces(self):
        self.assertTrue(self._needs("fix typo", env="always"))

    def test_never_skips(self):
        self.assertFalse(self._needs("update a.py and b.py", env="never"))

    def test_case_insensitive_env(self):
        self.assertTrue(self._needs("fix typo", env="Always"))

    def test_unknown_env_falls_back_to_auto(self):
        self.assertTrue(self._needs("update a.py and b.py", env="sometimes"))
        self.assertFalse(self._needs("fix typo", env="sometimes"))


class TestValidatePlan(unittest.TestCase):
    def test_ok(self):
        ok, text = coder.validate_plan(
            "PLAN:\n1. x [files: a] [checks: t]")
        self.assertTrue(ok)
        self.assertTrue(text.startswith("PLAN recorded:"))

    def test_empty(self):
        ok, text = coder.validate_plan("   ")
        self.assertFalse(ok)
        self.assertIn("REJECTED", text)

    def test_non_string(self):
        ok, _text = coder.validate_plan(None)
        self.assertFalse(ok)

    def test_too_long(self):
        ok, text = coder.validate_plan("x" * (coder.PLAN_LIMIT + 1))
        self.assertFalse(ok)
        self.assertIn("REJECTED", text)


class TestPlanPrompt(unittest.TestCase):
    def test_shape(self):
        prompt = coder.PLAN_PROMPT.format(brief="do the thing")
        self.assertIn("propose_plan", prompt)
        self.assertIn("PLAN:", prompt)
        self.assertIn("files:", prompt)
        self.assertIn("checks:", prompt)
        self.assertIn("Do not write", prompt)
        self.assertIn("do the thing", prompt)

    def test_nudge(self):
        self.assertIn("propose_plan", coder.PLAN_NUDGE)


class TestLatestPlan(unittest.TestCase):
    def _ledger(self, rows):
        fh = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        with fh:
            for row in rows:
                fh.write(row if isinstance(row, str) else json.dumps(row))
                fh.write("\n")
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def _row(self, thread="t1", phase="plan_pending", script="coder.py",
             ts=100.0, brief="b", worktree="/wt"):
        return {"script": script, "model": "fake:m", "thread_id": thread,
                "ts": ts,
                "extra": {"coder_phase": phase, "brief": brief,
                          "worktree": worktree, "system": "SYS",
                          "plan": "PLAN", "trace": "/tmp/x.jsonl"}}

    def test_newest_wins(self):
        import ledger

        path = self._ledger([self._row(ts=1.0, brief="old"),
                             self._row(ts=2.0, brief="new")])
        with mock.patch.object(ledger, "LEDGER", path):
            plan = coder.latest_plan("t1")
        self.assertEqual(plan["brief"], "new")
        self.assertEqual(plan["ts"], 2.0)
        self.assertEqual(plan["worktree"], "/wt")

    def test_filters_thread_script_phase(self):
        import ledger

        path = self._ledger([
            self._row(thread="other", brief="wrong-thread"),
            self._row(script="deep_chat.py", brief="wrong-script"),
            self._row(phase="plan_rejected", brief="wrong-phase"),
            "not json",
            "[1, 2]",
            self._row(brief="match"),
        ])
        with mock.patch.object(ledger, "LEDGER", path):
            plan = coder.latest_plan("t1")
        self.assertEqual(plan["brief"], "match")

    def test_missing(self):
        import ledger

        path = self._ledger([self._row(thread="other")])
        with mock.patch.object(ledger, "LEDGER", path):
            self.assertIsNone(coder.latest_plan("t1"))
        with mock.patch.object(ledger, "LEDGER",
                               "/tmp/does-not-exist-654.jsonl"):
            self.assertIsNone(coder.latest_plan("t1"))


class TestPlanIsStale(unittest.TestCase):
    def test_fresh(self):
        self.assertFalse(coder.plan_is_stale(100.0, 150.0, 60))

    def test_old(self):
        self.assertTrue(coder.plan_is_stale(100.0, 200.0, 60))

    def test_boundary_is_fresh(self):
        self.assertFalse(coder.plan_is_stale(100.0, 160.0, 60))

    def test_garbage_ts_is_stale(self):
        self.assertTrue(coder.plan_is_stale(None, 150.0, 60))
        self.assertTrue(coder.plan_is_stale("when", 150.0, 60))


class TestResumeArgv(unittest.TestCase):
    def test_resume_without_brief(self):
        args = coder.parse_args(["--resume", "approve", "--thread", "t1"])
        self.assertEqual(args.resume, "approve")
        self.assertIsNone(args.brief)
        self.assertIsNone(args.worktree)

    def test_resume_bad_verdict(self):
        with self.assertRaises(SystemExit):
            coder.parse_args(["--resume", "maybe"])

    def test_brief_still_required_without_resume(self):
        with self.assertRaises(SystemExit):
            coder.parse_args(["--thread", "t1"])

    def test_resume_brief_override(self):
        args = coder.parse_args(
            ["--resume", "approve", "--thread", "t1", "brief text"])
        self.assertEqual(args.brief, "brief text")


class TestMainBranching(unittest.TestCase):
    def test_resume_needs_thread(self):
        self.assertEqual(coder.main(["--resume", "approve"]), 2)

    def test_resume_dispatch(self):
        with mock.patch.object(coder, "_cmd_resume",
                               return_value=0) as resume:
            self.assertEqual(coder.main(
                ["--resume", "reject", "--thread", "t1",
                 "--worktree", "/tmp"]), 0)
        resume.assert_called_once_with(
            "/tmp", "t1", "reject", brief_override=None,
            system_prompt_override=None)

    def test_plan_branch(self):
        with tempfile.TemporaryDirectory() as wt:
            with (mock.patch.object(coder, "needs_plan",
                                    return_value=True),
                  mock.patch.object(coder, "plan_and_pause",
                                    return_value=3) as plan,
                  mock.patch.object(coder,
                                    "_implement_and_finish") as impl):
                self.assertEqual(
                    coder.main(["--worktree", wt, "do it"]), 3)
            plan.assert_called_once()
            self.assertEqual(plan.call_args[0][1], "do it")
            impl.assert_not_called()

    def test_implement_branch(self):
        with tempfile.TemporaryDirectory() as wt:
            with (mock.patch.object(coder, "needs_plan",
                                    return_value=False),
                  mock.patch.object(coder, "plan_and_pause") as plan,
                  mock.patch.object(coder, "_implement_and_finish",
                                    return_value=0) as impl):
                self.assertEqual(
                    coder.main(["--worktree", wt, "do it"]), 0)
            plan.assert_not_called()
            impl.assert_called_once()

    def test_missing_worktree(self):
        self.assertEqual(
            coder.main(["--worktree", "/tmp/does-not-exist-654", "do it"]),
            1)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestBuildPlanAgentWiring(unittest.TestCase):
    def test_plan_agent_denies_writes_and_pauses(self):
        import deepagents

        with mock.patch.object(deepagents, "create_deep_agent",
                               return_value="AGENT") as create:
            agent = coder.build_plan_agent("/wt", "SYS", model="M",
                                           recorder=None,
                                           checkpointer="C")
        self.assertEqual(agent, "AGENT")
        _args, kwargs = create.call_args
        self.assertIn("propose_plan", kwargs["interrupt_on"])
        perms = kwargs["permissions"]
        self.assertEqual(len(perms), 1)
        self.assertEqual(perms[0].mode, "deny")
        self.assertIn("write", list(perms[0].operations))
        names = [getattr(t, "name", "") for t in kwargs["tools"]]
        self.assertEqual(names, ["propose_plan"])
        self.assertEqual(kwargs["checkpointer"], "C")

    def test_implement_defaults_unchanged(self):
        import deepagents

        with mock.patch.object(deepagents, "create_deep_agent",
                               return_value="AG") as create:
            coder.build_agent("/wt", "SYS", model="M")
        _args, kwargs = create.call_args
        self.assertIsNone(kwargs["interrupt_on"])
        self.assertIsNone(kwargs["permissions"])
        names = [getattr(t, "name", "") for t in kwargs["tools"]]
        self.assertEqual(names, ["run_check"])


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestPlanAndPause(unittest.TestCase):
    """plan_and_pause with a fake plan agent + real HITL helpers."""

    PLAN = "PLAN:\n1. do it [files: a.py] [checks: pytest]"

    def _run(self, pending="plan", invokes_before_pending=0):
        import deep_chat
        import ledger
        import observe

        trace_fh = tempfile.NamedTemporaryFile(suffix=".jsonl",
                                               delete=False)
        trace_fh.close()
        self.addCleanup(os.unlink, trace_fh.name)
        ledger_fh = tempfile.NamedTemporaryFile(suffix=".jsonl",
                                                delete=False)
        ledger_fh.close()
        self.addCleanup(os.unlink, ledger_fh.name)

        plan_text = self.PLAN

        class FakeTask:
            def __init__(self, interrupts):
                self.interrupts = interrupts

        class FakeIV:
            def __init__(self, id, value):
                self.id = id
                self.value = value

        class FakeSnap:
            def __init__(self, tasks):
                self.tasks = tasks

        class FakeAgent:
            def __init__(self):
                self.invokes = []
                self.n_get_state = 0

            def invoke(self, payload, config=None):
                self.invokes.append(payload)
                return {"messages": []}

            def get_state(self, config=None):
                self.n_get_state += 1
                if self.n_get_state <= invokes_before_pending:
                    return FakeSnap([FakeTask([])])
                if pending == "plan":
                    return FakeSnap([FakeTask([FakeIV(
                        "iv1",
                        {"action_requests": [
                            {"name": "propose_plan",
                             "args": {"plan": plan_text}}]})])])
                return FakeSnap([FakeTask([])])

        agent = FakeAgent()
        buf = io.StringIO()
        with (mock.patch.object(coder, "build_plan_agent",
                                return_value=agent) as builder,
              mock.patch.object(
                  coder, "build_model",
                  return_value=(object(), {"provider": "fake",
                                          "model": "fake"})),
              mock.patch.object(deep_chat, "open_checkpointer",
                                return_value=(None, False, object())),
              mock.patch.object(observe, "default_trace_path",
                                return_value=trace_fh.name),
              mock.patch.object(ledger, "LEDGER", ledger_fh.name),
              redirect_stdout(buf)):
            code = coder.plan_and_pause(
                "/wt", "update a.py and b.py", "SYS", "t-plan", 30)
        builder.assert_called_once()
        return code, buf.getvalue(), agent, trace_fh.name, ledger_fh.name

    def _ledger_rows(self, path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_pause_emits_standard_blob(self):
        code, out, agent, trace_path, ledger_path = self._run()
        self.assertEqual(code, 3)
        blobs = [line for line in out.splitlines()
                 if line.startswith("INTERRUPT:")]
        self.assertEqual(len(blobs), 1)
        env = json.loads(blobs[0][len("INTERRUPT:"):])
        self.assertEqual(env["thread_id"], "t-plan")
        self.assertEqual(len(agent.invokes), 1)
        self.assertIn("update a.py and b.py",
                      agent.invokes[0]["messages"][0].content)

    def test_plan_traced_and_ledgered(self):
        _code, _out, _agent, trace_path, ledger_path = self._run()
        with open(trace_path, encoding="utf-8") as fh:
            trace = fh.read()
        self.assertIn('"type": "plan"', trace)
        self.assertIn("PLAN:", trace)
        rows = self._ledger_rows(ledger_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["extra"]["coder_phase"], "plan_pending")
        self.assertEqual(rows[0]["extra"]["brief"], "update a.py and b.py")
        self.assertEqual(rows[0]["thread_id"], "t-plan")

    def test_nudge_then_pause(self):
        code, _out, agent, _trace, _ledger = self._run(
            invokes_before_pending=1)
        self.assertEqual(code, 3)
        self.assertEqual(len(agent.invokes), 2)
        self.assertIn("propose_plan",
                      agent.invokes[1]["messages"][0].content)

    def test_no_tool_call_fails_closed(self):
        code, out, agent, _trace, ledger_path = self._run(pending="none")
        self.assertEqual(code, 1)
        self.assertIn("Sorry", out)
        self.assertEqual(len(agent.invokes), 2)
        rows = self._ledger_rows(ledger_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["extra"]["coder_phase"], "plan_failed")


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestResumePaths(unittest.TestCase):
    """_cmd_resume approve/reject/stale with fakes (no model)."""

    def _ledger_with_plan(self, ts, brief="update a.py and b.py",
                          worktree=None, system="SYS"):
        fh = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8")
        with fh:
            fh.write(json.dumps(
                {"script": "coder.py", "model": "fake:fake",
                 "thread_id": "t1", "ts": ts,
                 "extra": {"coder_phase": "plan_pending", "brief": brief,
                           "worktree": worktree, "system": system,
                           "plan": "PLAN:\n1. x", "trace": "/tmp/p.jsonl"}})
            + "\n")
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def _ledger_rows(self, path):
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _trace_file(self):
        fh = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        return fh.name

    def test_reject_aborts_cleanly(self):
        import ledger

        with tempfile.TemporaryDirectory() as wt:
            before = sorted(os.listdir(wt))
            ledger_path = self._ledger_with_plan(time.time(),
                                                 worktree=wt)
            buf = io.StringIO()
            with (mock.patch.object(ledger, "LEDGER", ledger_path),
                  mock.patch.object(
                      coder, "run_coder",
                      side_effect=AssertionError("must not implement")),
                  mock.patch.object(
                      coder, "build_plan_agent",
                      side_effect=AssertionError("must not touch agent")),
                  redirect_stdout(buf)):
                code = coder._cmd_resume(wt, "t1", "reject")
            self.assertEqual(code, 0)
            self.assertIn("no changes made", buf.getvalue())
            self.assertEqual(sorted(os.listdir(wt)), before)
            rows = self._ledger_rows(ledger_path)
            self.assertEqual(rows[-1]["extra"]["coder_phase"],
                             "plan_rejected")

    def test_approve_continues_same_thread(self):
        import deep_chat
        import ledger
        import observe

        with tempfile.TemporaryDirectory() as wt:
            ledger_path = self._ledger_with_plan(time.time(),
                                                 worktree=wt)
            trace_path = self._trace_file()

            class FakeTask:
                def __init__(self, interrupts):
                    self.interrupts = interrupts

            class FakeIV:
                def __init__(self, id, value):
                    self.id = id
                    self.value = value

            class FakeSnap:
                def __init__(self, tasks):
                    self.tasks = tasks

            class FakeAgent:
                def __init__(self):
                    self.invokes = []

                def invoke(self, payload, config=None):
                    self.invokes.append((payload, config))
                    return {"messages": []}

                def get_state(self, config=None):
                    return FakeSnap([FakeTask([FakeIV(
                        "iv1", {"action_requests": [{"name": "p",
                                                     "args": {}}]})])])

            agent = FakeAgent()
            implement_calls = []

            def fake_run_coder(worktree, brief, system, thread, timeout):
                implement_calls.append(
                    {"worktree": worktree, "brief": brief,
                     "system": system, "thread": thread})
                return ("did it", {}, [], time.time(), "fake:fake",
                        "/tmp/i.jsonl", True, 0)

            buf = io.StringIO()
            with (mock.patch.object(ledger, "LEDGER", ledger_path),
                  mock.patch.object(observe, "default_trace_path",
                                    return_value=trace_path),
                  mock.patch.object(coder, "build_model",
                                    return_value=(object(),
                                                  {"provider": "fake",
                                                   "model": "fake"})),
                  mock.patch.object(deep_chat, "open_checkpointer",
                                    return_value=(None, False, object())),
                  mock.patch.object(coder, "build_plan_agent",
                                    return_value=agent),
                  mock.patch.object(coder, "run_coder",
                                    side_effect=fake_run_coder),
                  redirect_stdout(buf)):
                code = coder._cmd_resume(wt, "t1", "approve")
            self.assertEqual(code, 0)
            self.assertIn("did it", buf.getvalue())
            # Same thread, ledgered brief, stored system.
            self.assertEqual(len(implement_calls), 1)
            self.assertEqual(implement_calls[0]["thread"], "t1")
            self.assertEqual(implement_calls[0]["brief"],
                             "update a.py and b.py")
            self.assertEqual(implement_calls[0]["system"], "SYS")
            # Approve decision carried on the resume command.
            self.assertEqual(len(agent.invokes), 1)
            command, config = agent.invokes[0]
            self.assertEqual(getattr(command, "resume", None),
                             {"decisions": [{"type": "approve"}]})
            self.assertEqual(config["configurable"]["thread_id"], "t1")
            # Ledger order: plan → approve → implement.
            rows = self._ledger_rows(ledger_path)
            phases = [r["extra"].get("coder_phase") for r in rows]
            self.assertEqual(phases,
                             ["plan_pending", "plan_approved",
                              "implement"])
            with open(trace_path, encoding="utf-8") as fh:
                trace = fh.read()
            self.assertIn('"plan_decision"', trace)
            self.assertIn('"approve"', trace)

    def test_stale_aborts_distinctly(self):
        import ledger

        with tempfile.TemporaryDirectory() as wt:
            ledger_path = self._ledger_with_plan(time.time() - 100000,
                                                 worktree=wt)
            buf = io.StringIO()
            with (mock.patch.object(ledger, "LEDGER", ledger_path),
                  mock.patch.object(
                      coder, "run_coder",
                      side_effect=AssertionError("must not implement")),
                  mock.patch.object(
                      coder, "build_plan_agent",
                      side_effect=AssertionError("must not touch agent")),
                  redirect_stdout(buf)):
                code = coder._cmd_resume(wt, "t1", "approve")
            self.assertEqual(code, 1)
            self.assertIn("expired", buf.getvalue())
            rows = self._ledger_rows(ledger_path)
            self.assertEqual(rows[-1]["extra"]["coder_phase"],
                             "plan_stale")

    def test_no_plan_is_sorry(self):
        import ledger

        with tempfile.TemporaryDirectory() as wt:
            ledger_path = self._ledger_with_plan(time.time(),
                                                 worktree=wt)
            buf = io.StringIO()
            with (mock.patch.object(ledger, "LEDGER", ledger_path),
                  redirect_stdout(buf)):
                code = coder._cmd_resume(wt, "no-such-thread", "approve")
            self.assertEqual(code, 1)
            self.assertIn("no pending plan", buf.getvalue())

    def test_approve_without_checkpoint_pause_is_sorry(self):
        import deep_chat
        import ledger
        import observe

        with tempfile.TemporaryDirectory() as wt:
            ledger_path = self._ledger_with_plan(time.time(),
                                                 worktree=wt)
            trace_path = self._trace_file()

            class FakeTask:
                def __init__(self, interrupts):
                    self.interrupts = interrupts

            class FakeSnap:
                def __init__(self, tasks):
                    self.tasks = tasks

            class FakeAgent:
                def get_state(self, config=None):
                    return FakeSnap([FakeTask([])])

                def invoke(self, payload, config=None):
                    raise AssertionError("must not invoke")

            buf = io.StringIO()
            with (mock.patch.object(ledger, "LEDGER", ledger_path),
                  mock.patch.object(observe, "default_trace_path",
                                    return_value=trace_path),
                  mock.patch.object(coder, "build_model",
                                    return_value=(object(),
                                                  {"provider": "fake",
                                                   "model": "fake"})),
                  mock.patch.object(deep_chat, "open_checkpointer",
                                    return_value=(None, False, object())),
                  mock.patch.object(coder, "build_plan_agent",
                                    return_value=FakeAgent()),
                  mock.patch.object(
                      coder, "run_coder",
                      side_effect=AssertionError("must not implement")),
                  redirect_stdout(buf)):
                code = coder._cmd_resume(wt, "t1", "approve")
            self.assertEqual(code, 1)
            self.assertIn("no pending approval", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
