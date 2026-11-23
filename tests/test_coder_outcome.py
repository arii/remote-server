import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import coder
import deep_chat
import observe
from backend.shared import ledger, model_router, runs
from backend.shared.cli import repo_root

try:
    import deepagents  # noqa: F401
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )
    from langchain_core.messages import AIMessage

    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

if HAS_DEPS:

    class LocalModel(FakeMessagesListChatModel):
        def bind_tools(self, *args, **kwargs):
            return self


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestCoderFallback(unittest.TestCase):
    def cap_error(self, provider="twt", model="default", limit=20):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )

        error = ModelCallLimitExceededError(limit, limit, limit, None)
        error.coder_model_spec = {"provider": provider, "model": model}
        error.coder_written_files = ("result.txt",)
        return error

    def test_only_default_route_initial_cap_exhaustion_gets_one_configured_attempt(
        self,
    ):
        for model in ("default", "route:default"):
            with (
                self.subTest(model=model),
                mock.patch.object(
                    coder,
                    "_run_coder_attempt",
                    side_effect=[self.cap_error(model=model), "finished"],
                ) as attempt,
                mock.patch.dict(
                    os.environ,
                    {"S5_LOCAL_ONLY": "0", "CODER_FALLBACK_MODEL": "nebius/custom"},
                ),
            ):
                self.assertEqual(
                    coder.run_coder("/repo", "brief", "system", "thread", 5), "finished"
                )
                self.assertEqual(attempt.call_count, 2)
                self.assertEqual(attempt.call_args.args[3], "thread-fallback")
                self.assertEqual(
                    attempt.call_args.kwargs["fallback_model"], "nebius/custom"
                )
                self.assertEqual(
                    attempt.call_args.kwargs["prior_files"], ("result.txt",)
                )

    def test_noneligible_empty_disabled_and_nonbudget_failures_do_not_fallback(self):
        cases = [
            (self.cap_error(provider="ollama"), {}, {}),
            (self.cap_error(model="nebius/explicit"), {}, {}),
            (self.cap_error(limit=12), {"repair": True}, {}),
            (self.cap_error(), {}, {"S5_LOCAL_ONLY": "1"}),
            (self.cap_error(), {}, {"CODER_FALLBACK_MODEL": ""}),
            (RuntimeError("BLOCKED: repeated unchanged read"), {}, {}),
            (TimeoutError("timeout"), {}, {}),
        ]
        for error, kwargs, overrides in cases:
            with (
                self.subTest(
                    error=str(error), kwargs=kwargs, overrides=tuple(overrides)
                ),
                mock.patch.dict(
                    os.environ,
                    {
                        "S5_LOCAL_ONLY": "0",
                        "CODER_FALLBACK_MODEL": coder.DEFAULT_FALLBACK_MODEL,
                        **overrides,
                    },
                ),
                mock.patch.object(
                    coder, "_run_coder_attempt", side_effect=error
                ) as attempt,
            ):
                with self.assertRaises(type(error)) as raised:
                    coder.run_coder("/repo", "brief", "system", "thread", 5, **kwargs)
                self.assertIs(raised.exception, error)
                self.assertEqual(attempt.call_count, 1)
        for result in ("finished", ""):
            with mock.patch.object(
                coder, "_run_coder_attempt", return_value=result
            ) as attempt:
                self.assertEqual(
                    coder.run_coder("/repo", "brief", "system", "thread", 5), result
                )
                attempt.assert_called_once()

    def test_paid_failure_is_not_retried(self):
        error = self.cap_error(model=coder.DEFAULT_FALLBACK_MODEL)
        with (
            mock.patch.dict(
                os.environ,
                {
                    "S5_LOCAL_ONLY": "0",
                    "CODER_FALLBACK_MODEL": coder.DEFAULT_FALLBACK_MODEL,
                },
            ),
            mock.patch.object(
                coder, "_run_coder_attempt", side_effect=[self.cap_error(), error]
            ) as attempt,
        ):
            with self.assertRaises(type(error)) as raised:
                coder.run_coder("/repo", "brief", "system", "thread", 5)
            self.assertIs(raised.exception, error)
            self.assertEqual(attempt.call_count, 2)

    def test_resumed_approved_plan_reaches_the_fallback_prompt(self):
        for persistent in (False, True):
            with (
                self.subTest(persistent=persistent),
                tempfile.TemporaryDirectory() as root,
            ):
                plan = dict(
                    ts=0,
                    brief="implement issue",
                    system="system",
                    worktree=root,
                    model="twt:default",
                    plan="Only edit approved.py; retain its public API",
                )
                result = ("DONE #1550", {}, [], 0, "twt:paid", "trace", True, 0)
                with (
                    mock.patch.dict(
                        os.environ,
                        {
                            "S5_LOCAL_ONLY": "0",
                            "CODER_FALLBACK_MODEL": coder.DEFAULT_FALLBACK_MODEL,
                        },
                    ),
                    mock.patch.object(coder, "latest_plan", return_value=plan),
                    mock.patch.object(coder, "plan_is_stale", return_value=False),
                    mock.patch.object(
                        coder,
                        "build_model",
                        return_value=(None, {"provider": "twt", "model": "default"}),
                    ),
                    mock.patch.object(
                        coder,
                        "build_plan_agent",
                        return_value=SimpleNamespace(invoke=mock.Mock(return_value={})),
                    ),
                    mock.patch.object(
                        deep_chat,
                        "open_checkpointer",
                        return_value=(None, persistent, None),
                    ),
                    mock.patch.object(
                        deep_chat,
                        "_pending_interrupts",
                        return_value=[object()] if persistent else [],
                    ),
                    mock.patch.object(coder, "_plan_requests", return_value=[]),
                    mock.patch.object(
                        coder,
                        "_run_coder_attempt",
                        side_effect=[self.cap_error(), result],
                    ) as attempt,
                    mock.patch.object(
                        observe,
                        "default_trace_path",
                        return_value=str(Path(root, "trace.jsonl")),
                    ),
                    mock.patch.object(ledger, "record"),
                ):
                    self.assertEqual(coder._cmd_resume(root, "thread", "approve"), 0)
                    self.assertEqual(attempt.call_count, 2)
                    self.assertIn(plan["plan"], attempt.call_args.args[1])

    def test_fallback_builder_forces_twt_without_free_stage_maps(self):
        spec = {
            "provider": "twt",
            "model": "default",
            "base_url": "http://twt/v1",
            "provider_models": {"nvidia": "free"},
        }
        with (
            mock.patch.object(
                model_router, "resolve_llm", return_value=spec
            ) as resolve,
            mock.patch.object(
                model_router, "build_chat_model", return_value=(None, spec)
            ) as build,
        ):
            coder.build_fallback_model(coder.DEFAULT_FALLBACK_MODEL)
        resolve.assert_called_once_with(prefer="twt")
        self.assertNotIn("stage", build.call_args.kwargs)
        sent = build.call_args.kwargs["spec"]
        self.assertEqual(sent["provider"], "twt")
        self.assertEqual(sent["model"], "nebius/zai-org/GLM-5.3-Flash")
        self.assertNotIn("provider_models", sent)
        with (
            mock.patch.object(
                model_router, "resolve_llm", return_value={"provider": "ollama"}
            ),
            mock.patch.object(model_router, "build_chat_model") as build,
        ):
            with self.assertRaisesRegex(RuntimeError, "requires TwT"):
                coder.build_fallback_model(coder.DEFAULT_FALLBACK_MODEL)
            build.assert_not_called()

    def writing_model(self, prefix):
        from langchain_core.messages import AIMessage

        return LocalModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "write_file",
                            "args": {
                                "file_path": f"{prefix}-{n}.txt",
                                "content": "done",
                            },
                            "id": f"{prefix}-{n}",
                            "type": "tool_call",
                        }
                    ],
                )
                for n in range(coder.INITIAL_MODEL_LIMIT)
            ]
        )

    def test_real_graph_retains_edits_and_verifies_on_a_fresh_paid_thread(self):
        from langchain_core.messages import AIMessage
        from langgraph.checkpoint.memory import InMemorySaver

        paid = LocalModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "run_check",
                            "args": {"command": "python3 -m unittest tests.test_edits"},
                            "id": "check",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="DONE #1550"),
            ]
        )
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.dict(
                os.environ,
                {
                    "S5_LOCAL_ONLY": "0",
                    "CODER_FALLBACK_MODEL": coder.DEFAULT_FALLBACK_MODEL,
                },
            ),
            mock.patch.object(
                coder,
                "build_model",
                return_value=(
                    self.writing_model("free"),
                    {"provider": "twt", "model": "route:default"},
                ),
            ) as initial,
            mock.patch.object(
                coder,
                "build_fallback_model",
                return_value=(
                    paid,
                    {"provider": "twt", "model": coder.DEFAULT_FALLBACK_MODEL},
                ),
            ) as fallback,
            mock.patch.object(
                deep_chat,
                "open_checkpointer",
                return_value=(InMemorySaver(), False, None),
            ),
            mock.patch.object(
                observe,
                "default_trace_path",
                side_effect=lambda _, thread: str(Path(root, thread + ".jsonl")),
            ),
            mock.patch.object(coder, "verify_rounds", return_value=1),
            mock.patch.object(ledger, "record"),
        ):
            Path(root, "tests").mkdir()
            Path(root, "tests/__init__.py").touch()
            Path(root, "tests/test_edits.py").write_text(
                "import unittest\nfrom pathlib import Path\n"
                "class Edits(unittest.TestCase):\n"
                "    def test_retained(self):\n"
                "        self.assertEqual(len(list(Path('.').glob('free-*.txt'))), 20)\n"
            )
            result = coder.run_coder(root, "implement", "system", "thread", 10)
            self.assertEqual(result[0], "DONE #1550")
            self.assertTrue(result[6])
            self.assertEqual(len(list(Path(root).glob("free-*.txt"))), 20)
            initial.assert_called_once()
            fallback.assert_called_once_with(coder.DEFAULT_FALLBACK_MODEL)

    def test_real_paid_graph_has_its_own_20_call_limit(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )
        from langgraph.checkpoint.memory import InMemorySaver

        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.dict(
                os.environ,
                {
                    "S5_LOCAL_ONLY": "0",
                    "CODER_FALLBACK_MODEL": coder.DEFAULT_FALLBACK_MODEL,
                },
            ),
            mock.patch.object(
                coder,
                "build_model",
                return_value=(
                    self.writing_model("free"),
                    {"provider": "twt", "model": "default"},
                ),
            ),
            mock.patch.object(
                coder,
                "build_fallback_model",
                return_value=(
                    self.writing_model("paid"),
                    {"provider": "twt", "model": coder.DEFAULT_FALLBACK_MODEL},
                ),
            ) as fallback,
            mock.patch.object(
                deep_chat,
                "open_checkpointer",
                return_value=(InMemorySaver(), False, None),
            ),
            mock.patch.object(
                observe,
                "default_trace_path",
                side_effect=lambda _, thread: str(Path(root, thread + ".jsonl")),
            ),
            mock.patch.object(ledger, "record"),
        ):
            with self.assertRaises(ModelCallLimitExceededError) as raised:
                coder.run_coder(root, "implement", "system", "thread", 10)
            self.assertEqual(raised.exception.thread_count, 20)
            self.assertEqual(len(list(Path(root).glob("paid-*.txt"))), 20)
            fallback.assert_called_once()

    def test_remaining_issue_budget_prevents_paid_dispatch(self):
        from scripts import queue_runner

        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "ledger.jsonl")

            def build(*args, recorder, **kwargs):
                def invoke(*args, **kwargs):
                    recorder.model_response(
                        {
                            "reported_cost_usd": 0.5,
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                        }
                    )
                    raise self.cap_error()

                return SimpleNamespace(invoke=invoke)

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "S5_LOCAL_ONLY": "0",
                        "CODER_FALLBACK_MODEL": coder.DEFAULT_FALLBACK_MODEL,
                    },
                ),
                mock.patch.object(
                    coder,
                    "build_model",
                    return_value=(None, {"provider": "twt", "model": "default"}),
                ),
                mock.patch.object(coder, "build_agent", side_effect=build),
                mock.patch.object(
                    deep_chat, "open_checkpointer", return_value=(None, False, None)
                ),
                mock.patch.object(ledger, "LEDGER", str(path)),
                mock.patch.object(runs, "LEDGER", str(path)),
                mock.patch.object(ledger, "state_dir", return_value=root),
                mock.patch.object(
                    observe,
                    "default_trace_path",
                    side_effect=lambda _, thread: str(Path(root, thread + ".jsonl")),
                ),
                mock.patch.object(
                    model_router, "_remote_spec", model_router._remote_spec
                ),
                mock.patch.object(
                    model_router._BoundedHistory,
                    "_bounded_kwargs",
                    model_router._BoundedHistory._bounded_kwargs,
                ),
                mock.patch.object(model_router, "build_chat_model") as paid,
            ):
                queue_runner.install_budget_guard(0.5, Path(root, "job"), [])
                with self.assertRaises(queue_runner.BudgetStop):
                    coder.run_coder(root, "implement", "system", "thread", 5)
                paid.assert_not_called()
            self.assertEqual(json.loads(path.read_text())["cost_usd"], 0.5)

    def test_failed_first_attempt_is_ledgered_before_paid_dispatch_and_costs_add(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root, "ledger.jsonl")
            attempts = []

            def agent(*args, recorder, written_files, **kwargs):
                index = len(attempts)
                attempts.append(index)

                def invoke(*args, **kwargs):
                    recorder.model_response(
                        {
                            "reported_cost_usd": 0.01 if index == 0 else 0.02,
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                        }
                    )
                    written_files.add("result.txt")
                    if index == 0:
                        raise self.cap_error()
                    return {"messages": [AIMessage(content="DONE #1550")]}

                return SimpleNamespace(invoke=invoke)

            def paid(model):
                row = json.loads(path.read_text())
                self.assertEqual(row["cost_usd"], 0.01)
                self.assertIn("ModelCallLimitExceededError", row["extra"]["error"])
                return None, {"provider": "twt", "model": model}

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "S5_LOCAL_ONLY": "0",
                        "CODER_FALLBACK_MODEL": coder.DEFAULT_FALLBACK_MODEL,
                    },
                ),
                mock.patch.object(
                    coder,
                    "build_model",
                    return_value=(None, {"provider": "twt", "model": "default"}),
                ),
                mock.patch.object(coder, "build_fallback_model", side_effect=paid),
                mock.patch.object(coder, "build_agent", side_effect=agent),
                mock.patch.object(coder, "verify_rounds", return_value=0),
                mock.patch.object(
                    deep_chat, "open_checkpointer", return_value=(None, False, None)
                ),
                mock.patch.object(ledger, "LEDGER", str(path)),
                mock.patch.object(
                    observe,
                    "default_trace_path",
                    side_effect=lambda _, thread: str(Path(root, thread + ".jsonl")),
                ),
            ):
                result = coder.run_coder(root, "implement", "system", "thread", 5)
                ledger.record(
                    "pipeline.py",
                    result[4],
                    "thread",
                    [],
                    result[3],
                    usage=result[1],
                    calls=result[2],
                )
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertAlmostEqual(sum(row["cost_usd"] for row in rows), 0.03)


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestCoderOutcome(unittest.TestCase):
    def test_script_cli_can_write_a_file(self):
        repo = Path(repo_root())
        with tempfile.TemporaryDirectory() as root:
            Path(root, "sitecustomize.py").write_text(
                textwrap.dedent("""
                from pathlib import Path
                import deep_chat, observe
                from backend.shared import ledger, model_router
                from langchain_core.messages import AIMessage
                from tests.test_coder_outcome import LocalModel
                model = LocalModel(responses=[
                    AIMessage(content="", tool_calls=[{
                        "name": "write_file", "args": {"file_path": "result.txt", "content": "done"},
                        "id": "write", "type": "tool_call",
                    }]), AIMessage(content="DONE #1261"),
                ])
                model_router.resolve_llm = lambda: {"provider": "ollama", "model": "test"}
                model_router.build_chat_model = lambda **kwargs: (model, {"provider": "ollama", "model": "test"})
                deep_chat.open_checkpointer = lambda *args: (None, False, None)
                ledger.record = lambda *args, **kwargs: None
                observe.default_trace_path = lambda *args: str(Path.cwd() / "trace.jsonl")
            """)
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(repo / "deep-agent/coder.py"),
                    "--worktree",
                    root,
                    "Implement #1261",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        [root, str(repo), str(repo / "deep-agent")]
                    ),
                    "CODER_PLAN": "never",
                    "CODER_VERIFY_ROUNDS": "0",
                },
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("DONE #1261", result.stdout)
            self.assertEqual(Path(root, "result.txt").read_text(), "done")

    def run_scenario(
        self,
        reply,
        operation=None,
        cli=False,
        rounds=0,
        setup=None,
        repair_files=(),
        verified=False,
        repair=False,
    ):
        with tempfile.TemporaryDirectory() as root:
            trace = Path(root, "trace.jsonl")
            Path(root, "obsolete.txt").write_text("obsolete")
            if setup:
                setup(Path(root))

            calls = []
            actions = SimpleNamespace(
                delete=lambda path: calls.append(("delete", {"file_path": path})),
                write=lambda path, content: calls.append(
                    ("write_file", {"file_path": path, "content": content})
                ),
                edit=lambda path, old, new: calls.append(
                    (
                        "edit_file",
                        {"file_path": path, "old_string": old, "new_string": new},
                    )
                ),
            )
            if operation:
                operation(actions)
            responses = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": name, "args": args, "id": str(i), "type": "tool_call"}
                    ],
                )
                for i, (name, args) in enumerate(calls)
            ]
            model = LocalModel(responses=responses + [AIMessage(content=reply)])

            with (
                mock.patch.object(
                    coder,
                    "build_model",
                    return_value=(model, {"provider": "ollama", "model": "test"}),
                ),
                mock.patch.object(coder, "verify_rounds", return_value=rounds),
                mock.patch.object(
                    coder, "trace_has_passing_check", return_value=verified
                ),
                mock.patch.object(
                    deep_chat, "open_checkpointer", return_value=(None, False, None)
                ),
                mock.patch.object(
                    observe, "default_trace_path", return_value=str(trace)
                ),
                mock.patch("backend.shared.ledger.record"),
            ):
                if cli:
                    result = coder._implement_and_finish(
                        root, "Implement #1261", "system", "thread", 5
                    )
                else:
                    try:
                        result = coder.run_coder(
                            root,
                            "Implement #1261",
                            "system",
                            "thread",
                            5,
                            repair_files=repair_files,
                            repair=repair,
                        )
                    except RuntimeError as exc:
                        result = exc
            return result, [json.loads(line) for line in trace.read_text().splitlines()]

    def test_full_coding_turn_fits_graph_budget_including_repairs(self):
        for repair in (False, True):
            with self.subTest(repair=repair):
                result, events = self.run_scenario(
                    "DONE #1261",
                    operation=lambda actions: [
                        actions.write(f"result-{index}.txt", "done")
                        for index in range(coder.REPAIR_MODEL_LIMIT - 1)
                    ],
                    repair=repair,
                )
                self.assertNotIsInstance(result, BaseException)
                self.assertEqual(result[0], "DONE #1261")
                self.assertTrue(events[-1]["ok"])

    def test_repair_model_call_limit_still_bounds_a_long_coding_turn(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )

        with self.assertRaises(ModelCallLimitExceededError):
            self.run_scenario(
                "DONE #1261",
                operation=lambda actions: [
                    actions.write(f"result-{index}.txt", "done")
                    for index in range(coder.REPAIR_MODEL_LIMIT)
                ],
                repair=True,
            )

    def test_initial_turn_can_exceed_repair_model_budget(self):
        result, events = self.run_scenario(
            "DONE #1261",
            operation=lambda actions: [
                actions.write(f"result-{index}.txt", "done")
                for index in range(coder.REPAIR_MODEL_LIMIT + 2)
            ],
        )
        self.assertNotIsInstance(result, BaseException)
        self.assertEqual(result[0], "DONE #1261")
        self.assertTrue(events[-1]["ok"])

    def test_initial_model_budget_is_bounded(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )

        self.assertGreater(coder.INITIAL_MODEL_LIMIT, coder.REPAIR_MODEL_LIMIT)
        with self.assertRaises(ModelCallLimitExceededError):
            self.run_scenario(
                "DONE #1261",
                operation=lambda actions: [
                    actions.write(f"result-{index}.txt", "done")
                    for index in range(coder.INITIAL_MODEL_LIMIT)
                ],
            )

    def test_flatline_is_failed_with_missing_contract_reason(self):
        result, events = self.run_scenario("")
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("DONE", str(result))
        self.assertIn("no written files", str(result))
        self.assertFalse(events[-1]["ok"])
        self.assertIn("DONE", events[-2]["reason"])

    def test_done_without_writes_fails(self):
        result, events = self.run_scenario("DONE #1261")
        self.assertIsInstance(result, RuntimeError)
        self.assertFalse(events[-1]["ok"])

    def test_verified_repair_counts_existing_scoped_diff(self):
        result, events = self.run_scenario(
            "DONE #1261",
            setup=self.existing_diff,
            repair_files=("a.py",),
            verified=True,
            rounds=1,
        )
        self.assertEqual(result[0], "DONE #1261")
        self.assertTrue(events[-1]["ok"])

    def test_existing_diff_does_not_bypass_empty_or_unverified_turns(self):
        for files, verified in (((), True), (("other.py",), True), (("a.py",), False)):
            with self.subTest(files=files, verified=verified):
                result, _ = self.run_scenario(
                    "DONE #1261",
                    setup=self.existing_diff,
                    repair_files=files,
                    verified=verified,
                )
                self.assertIsInstance(result, RuntimeError)
                self.assertIn("no written files", str(result))

    def test_repair_diff_read_error_fails_closed(self):
        result, _ = self.run_scenario(
            "DONE #1261",
            repair_files=("a.py",),
            verified=True,
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("cannot inspect", str(result))

    def test_repair_with_reverted_diff_still_fails(self):
        def setup(root):
            self.existing_diff(root)
            (root / "a.py").write_text("before\n")

        result, _ = self.run_scenario(
            "DONE #1261",
            setup=setup,
            repair_files=("a.py",),
            verified=True,
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("no written files", str(result))

    def test_existing_diff_covers_staged_new_and_deleted_scoped_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.existing_diff(root)
            subprocess.run(["git", "-C", directory, "add", "a.py"], check=True)
            (root / "new.py").write_text("new\n")
            (root / "outside.py").write_text("unrelated\n")
            self.assertEqual(
                coder.existing_implementation_files(directory, ("a.py", "new.py")),
                {"a.py", "new.py"},
            )
            (root / "a.py").unlink()
            self.assertEqual(
                coder.existing_implementation_files(directory, ("a.py",)), {"a.py"}
            )
            with self.assertRaises(ValueError):
                coder.existing_implementation_files(directory, ("../outside.py",))

    @staticmethod
    def existing_diff(root):
        def git(*args):
            subprocess.run(
                ["git", "-C", str(root), *args], check=True, capture_output=True
            )

        git("init", "-q")
        Path(root, "a.py").write_text("before\n")
        git("add", "a.py")
        git(
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "baseline",
        )
        Path(root, "a.py").write_text("after\n")

    def test_deletion_only_implementation_succeeds(self):
        result, events = self.run_scenario(
            "DONE #1261", lambda b: b.delete("obsolete.txt")
        )
        self.assertNotIsInstance(result, RuntimeError)
        self.assertTrue(events[-1]["ok"])

    def test_failed_delete_does_not_count(self):
        result, events = self.run_scenario(
            "DONE #1261", lambda b: b.delete("missing.txt")
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertFalse(events[-1]["ok"])

    def test_write_without_terminal_contract_fails(self):
        result, events = self.run_scenario(
            "Implemented it", lambda b: b.write("a.txt", "hello")
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("DONE", str(result))
        self.assertFalse(events[-1]["ok"])

    def test_done_after_write_succeeds(self):
        result, events = self.run_scenario(
            "Changed a.txt\nDONE #1261", lambda b: b.write("a.txt", "hello")
        )
        self.assertEqual(result[0], "Changed a.txt\nDONE #1261")
        self.assertTrue(events[-1]["ok"])

    def test_failed_edit_does_not_count_as_a_write(self):
        result, events = self.run_scenario(
            "DONE #1261", lambda b: b.edit("missing.txt", "a", "b")
        )
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("no written files", str(result))
        self.assertFalse(events[-1]["ok"])

    def test_unverified_preserves_terminal_contract(self):
        result, _ = self.run_scenario(
            "DONE #1261", lambda b: b.write("a.txt", "hello"), rounds=1
        )
        self.assertEqual(result[0].splitlines()[-1], "DONE #1261")

    def test_successful_edit_counts(self):
        def operation(actions):
            actions.write("a.txt", "before")
            actions.edit("a.txt", "before", "after")

        result, events = self.run_scenario("DONE #1261", operation)
        self.assertEqual(result[0], "DONE #1261")
        self.assertTrue(events[-1]["ok"])
        self.assertTrue(any(event.get("name") == "edit_file" for event in events))

    def test_blocked_is_not_success(self):
        result, events = self.run_scenario("BLOCKED #1261")
        self.assertIsInstance(result, RuntimeError)
        self.assertIn("BLOCKED", str(result))
        self.assertFalse(events[-1]["ok"])

    def test_contract_must_be_terminal(self):
        result, _ = self.run_scenario("DONE #1261\nStill working")
        self.assertIsInstance(result, RuntimeError)

    def test_cli_flatline_returns_nonzero(self):
        result, events = self.run_scenario("", cli=True)
        self.assertNotEqual(result, 0)
        self.assertFalse(events[-1]["ok"])
