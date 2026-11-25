import importlib.util
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import auditor
import coder
import pipeline


HAS_DEPS = importlib.util.find_spec("deepagents") is not None


@unittest.skipUnless(HAS_DEPS, "requires deep-agent requirements")
class TestRepairBudget(unittest.TestCase):
    def call(self, name="grep", **args):
        return SimpleNamespace(tool_call={"name": name, "args": args, "id": "probe"})

    def test_1570_repeated_grep_replays_empty_result_without_reexecution(self):
        from langchain_core.messages import ToolMessage

        for repair in (False, True):
            with self.subTest(repair=repair):
                guard = coder.coder_tool_middleware(repair=repair).wrap_tool_call

                def execute(request):
                    return ToolMessage(
                        content="No matches found",
                        tool_call_id=request.tool_call["id"],
                    )

                handler = mock.Mock(side_effect=execute)
                call = self.call(pattern='"last_recovery"')
                call.tool_call["id"] = "9f894daf"
                first = guard(call, handler)
                self.assertEqual(first.content, "No matches found")
                other = self.call(pattern='"heartbeat"')
                other.tool_call["id"] = "bd4adc59"
                guard(other, handler)
                for trace in ("2c0e109b", "7adebf4a"):
                    call.tool_call["id"] = trace
                    cached = guard(call, handler)
                    self.assertEqual(cached.tool_call_id, trace)
                    self.assertEqual(cached.status, "success")
                    self.assertIn("Already called", cached.content)
                    self.assertIn(
                        "make the planned edit or report DONE/BLOCKED", cached.content
                    )
                    self.assertTrue(cached.content.endswith(first.content))
                self.assertEqual(handler.call_count, 2)

    def test_1627_repeated_inspections_carry_the_earlier_result(self):
        from langchain_core.messages import ToolMessage

        for repair in (False, True):
            with self.subTest(repair=repair):
                guard = coder.coder_tool_middleware(repair=repair).wrap_tool_call
                handler = mock.Mock(
                    return_value=ToolMessage(
                        content="/backend/tests/test_queue.py", tool_call_id="probe"
                    )
                )
                call = self.call("ls", path="backend/tests")
                guard(call, handler)
                warning = guard(call, handler)
                self.assertIn("Already called", warning.content)
                self.assertIn("/backend/tests/test_queue.py", warning.content)
                self.assertEqual(warning.status, "success")
                self.assertEqual(handler.call_count, 1)

    def test_1627_grep_on_a_file_defaults_to_content(self):
        from langchain_core.messages import ToolMessage

        with tempfile.TemporaryDirectory() as root:
            Path(root, "docs").mkdir()
            Path(root, "docs/queue.md").write_text("STOP sentinel\n")
            guard = coder.worktree_file_middleware(root, None).wrap_tool_call
            sent = []

            def handler(request):
                sent.append(request.tool_call["args"])
                return ToolMessage(content="ok", tool_call_id="probe")

            def request(**args):
                return SimpleNamespace(
                    tool_call={"name": "grep", "args": args, "id": "probe"},
                    override=lambda tool_call: SimpleNamespace(tool_call=tool_call),
                )

            guard(request(path="docs/queue.md", pattern="STOP"), handler)
            guard(request(path="docs", pattern="STOP"), handler)
            guard(
                request(path="docs/queue.md", pattern="STOP", output_mode="count"),
                handler,
            )
            self.assertEqual(
                [args.get("output_mode") for args in sent], ["content", None, "count"]
            )

    def test_successful_edit_allows_rechecking_but_failed_edit_does_not(self):
        from langchain_core.messages import ToolMessage

        for status in ("success", "error"):
            with self.subTest(status=status):
                guard = coder.coder_tool_middleware(repair=True).wrap_tool_call
                handler = mock.Mock(
                    return_value=ToolMessage(
                        content="result", tool_call_id="probe", status=status
                    )
                )
                call = self.call("grep", path="a.py", pattern="fixed")
                guard(call, handler)
                guard(self.call("edit_file", file_path="a.py"), handler)
                result = guard(call, handler)
                self.assertEqual("Already called" in result.content, status == "error")
                self.assertEqual(handler.call_count, 3 if status == "success" else 2)

    def test_check_can_run_again_after_install(self):
        from langchain_core.messages import ToolMessage

        for repair in (False, True):
            with self.subTest(repair=repair):
                guard = coder.coder_tool_middleware(repair=repair).wrap_tool_call
                handler = mock.Mock(
                    side_effect=[
                        ToolMessage(content=f"exit={code}\n", tool_call_id="probe")
                        for code in (1, 0, 0)
                    ]
                )
                check = self.call("run_check", command="pnpm test")
                guard(check, handler)
                guard(self.call("run_check", command="pnpm install"), handler)
                result = guard(check, handler)
                self.assertEqual(result.content, "exit=0\n")
                self.assertEqual(handler.call_count, 3)

    def test_total_budget_cannot_be_reset_by_edits_or_unique_calls(self):
        from langchain_core.messages import ToolMessage

        guard = coder.coder_tool_middleware(repair=True).wrap_tool_call
        handler = mock.Mock(
            return_value=ToolMessage(content="edited", tool_call_id="probe")
        )
        for n in range(coder.REPAIR_TOOL_LIMIT):
            guard(self.call("edit_file", file_path=f"{n}.py"), handler)
        with self.assertRaisesRegex(
            coder.RepairBudgetExceeded, "budget exhausted"
        ) as caught:
            guard(self.call("read_file", file_path="next.py"), handler)
        self.assertTrue(caught.exception.recoverable_stall)
        self.assertEqual(handler.call_count, coder.REPAIR_TOOL_LIMIT)

    def test_initial_tool_budget_is_larger_but_cannot_be_reset_by_edits(self):
        from langchain_core.messages import ToolMessage

        self.assertGreater(coder.INITIAL_TOOL_LIMIT, coder.REPAIR_TOOL_LIMIT)
        guard = coder.coder_tool_middleware().wrap_tool_call
        handler = mock.Mock(
            return_value=ToolMessage(content="edited", tool_call_id="probe")
        )
        for n in range(coder.INITIAL_TOOL_LIMIT):
            guard(self.call("edit_file", file_path=f"{n}.py"), handler)
        with self.assertRaisesRegex(
            coder.CoderBudgetExceeded, "BLOCKED.*budget exhausted"
        ) as caught:
            guard(self.call("read_file", file_path="next.py"), handler)
        # A stalled turn's written-but-unverified edits still get the
        # verify/correction salvage path (#1571).
        self.assertTrue(caught.exception.recoverable_stall)
        self.assertEqual(handler.call_count, coder.INITIAL_TOOL_LIMIT)

    def test_1633_exploring_without_an_edit_nudges_then_blocks(self):
        from langchain_core.messages import ToolMessage

        handler = mock.Mock(
            return_value=ToolMessage(
                content="ok", tool_call_id="probe", status="success"
            )
        )
        guard = coder.coder_tool_middleware().wrap_tool_call
        for n in range(coder.EXPLORE_NUDGE - 1):
            guard(self.call("grep", path="a.py", pattern=f"p{n}"), handler)
        nudge = guard(self.call("read_file", file_path="b.py"), handler)
        self.assertEqual(nudge.status, "error")
        self.assertIn("make the planned edit now", nudge.content)
        self.assertEqual(handler.call_count, coder.EXPLORE_NUDGE - 1)
        for n in range(coder.EXPLORE_LIMIT - coder.EXPLORE_NUDGE - 1):
            guard(self.call("ls", path=f"d{n}"), handler)
        with self.assertRaisesRegex(
            coder.CoderBudgetExceeded, "BLOCKED.*inspection calls without editing"
        ):
            guard(self.call("glob", pattern="*.ts"), handler)

    def test_1633_calls_batched_with_the_nudge_get_it_instead_of_the_block(self):
        from langchain_core.messages import AIMessage, ToolMessage

        handler = mock.Mock(
            return_value=ToolMessage(
                content="ok", tool_call_id="probe", status="success"
            )
        )
        guard = coder.coder_tool_middleware().wrap_tool_call
        for n in range(coder.EXPLORE_NUDGE - 1):
            guard(self.call("grep", path="a.py", pattern=f"p{n}"), handler)
        batch = [
            {"name": "grep", "args": {"pattern": f"b{n}"}, "id": f"b{n}"}
            for n in range(5)
        ]
        state = {"messages": [AIMessage(content="", tool_calls=batch)]}
        for call in batch:
            result = guard(SimpleNamespace(tool_call=call, state=state), handler)
            self.assertIn("make the planned edit now", result.content)
            self.assertIn("BLOCKED #<issue>", result.content)
        self.assertEqual(handler.call_count, coder.EXPLORE_NUDGE - 1)
        for n in range(coder.EXPLORE_LIMIT - coder.EXPLORE_NUDGE - 1):
            guard(self.call("ls", path=f"d{n}"), handler)
        with self.assertRaisesRegex(coder.CoderBudgetExceeded, "without editing"):
            guard(self.call("glob", pattern="*.ts"), handler)

    def test_1633_successful_edit_resets_exploring_and_other_modes_are_unbound(self):
        from langchain_core.messages import ToolMessage

        handler = mock.Mock(
            return_value=ToolMessage(
                content="ok", tool_call_id="probe", status="success"
            )
        )
        guard = coder.coder_tool_middleware().wrap_tool_call
        for n in range(coder.EXPLORE_NUDGE - 1):
            guard(self.call("grep", path="a.py", pattern=f"p{n}"), handler)
        guard(self.call("edit_file", file_path="a.py"), handler)
        for n in range(coder.EXPLORE_NUDGE - 1):
            result = guard(self.call("grep", path="a.py", pattern=f"q{n}"), handler)
            self.assertEqual(result.content, "ok")
        planner = coder.coder_tool_middleware(planning=True).wrap_tool_call
        for n in range(coder.EXPLORE_LIMIT + 1):
            self.assertEqual(
                planner(
                    self.call("grep", path="a.py", pattern=f"r{n}"), handler
                ).content,
                "ok",
            )

    def test_1550_initial_repeated_read_stops_at_model_budget(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        with tempfile.TemporaryDirectory() as root:
            Path(root, "tests").mkdir()
            Path(root, "tests/test_queue_runner.py").write_text("pass\n")
            model = LocalModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {
                                    "file_path": "tests/test_queue_runner.py",
                                    "limit": 80,
                                },
                                "id": f"read-{n}",
                                "type": "tool_call",
                            }
                        ],
                    )
                    for n in range(coder.INITIAL_MODEL_LIMIT)
                ]
            )
            tracker = coder.WriteTracker()
            tracker.add("tests/test_queue_runner.py")
            graph = coder.build_agent(
                root, "implement", model=model, repair=False, written_files=tracker
            )
            limit = coder.INITIAL_MODEL_LIMIT
            with self.assertRaisesRegex(
                ModelCallLimitExceededError, rf"thread limit \({limit}/{limit}\)"
            ):
                graph.invoke(
                    {"messages": [HumanMessage(content="fix #1550")]},
                    config={"recursion_limit": 128},
                )

    def test_1731_initial_stalled_run_stops_after_five_model_calls(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        with tempfile.TemporaryDirectory() as root:
            Path(root, "tests").mkdir()
            Path(root, "tests/test_queue_runner.py").write_text("pass\n")
            model = LocalModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {
                                    "file_path": "tests/test_queue_runner.py",
                                    "limit": 80,
                                },
                                "id": f"read-{n}",
                                "type": "tool_call",
                            }
                        ],
                    )
                    for n in range(10)
                ]
            )
            graph = coder.build_agent(root, "implement", model=model, repair=False)
            with self.assertRaisesRegex(
                coder.CoderBudgetExceeded,
                r"BLOCKED:.*stalled after 5 model calls with neither a plan nor an edit",
            ):
                graph.invoke(
                    {"messages": [HumanMessage(content="fix #1731")]},
                    config={"recursion_limit": 128},
                )

    def test_1731_progress_with_plan_or_edit_proceeds_past_five_calls(self):
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        with tempfile.TemporaryDirectory() as root:
            Path(root, "tests").mkdir()
            Path(root, "tests/test_queue_runner.py").write_text("pass\n")
            # 6 calls: call 0 outputs plan, calls 1-4 read, call 5 finishes
            plan_model = LocalModel(
                responses=[
                    AIMessage(
                        content="Plan:\n1. Update test [files: tests/test_queue_runner.py]",
                        tool_calls=[
                            {
                                "name": "read_file",
                                "args": {
                                    "file_path": "tests/test_queue_runner.py",
                                    "limit": 80,
                                },
                                "id": "read-0",
                                "type": "tool_call",
                            }
                        ],
                    ),
                    *[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "read_file",
                                    "args": {
                                        "file_path": "tests/test_queue_runner.py",
                                        "limit": 80,
                                    },
                                    "id": f"read-{n}",
                                    "type": "tool_call",
                                }
                            ],
                        )
                        for n in range(1, 5)
                    ],
                    AIMessage(content="DONE #1731"),
                ]
            )
            graph = coder.build_agent(root, "implement", model=plan_model, repair=False)
            res = graph.invoke(
                {"messages": [HumanMessage(content="fix #1731 with plan")]},
                config={"recursion_limit": 128},
            )
            self.assertIn("DONE #1731", res["messages"][-1].content)

    def test_approved_plan_does_not_consume_implementation_model_budget(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )
        from langchain_core.messages import AIMessage, HumanMessage
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.types import Command

        import deep_chat
        from tests.test_coder_outcome import LocalModel

        with tempfile.TemporaryDirectory() as root:
            saver = InMemorySaver()
            config = {
                "configurable": {"thread_id": "plan-implement"},
                "recursion_limit": 128,
            }
            plan = coder.build_plan_agent(
                root,
                "plan",
                checkpointer=saver,
                model=LocalModel(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "propose_plan",
                                    "args": {
                                        "plan": "PLAN:\n1. Implement [files: result.txt] [checks: pytest]"
                                    },
                                    "id": "plan",
                                    "type": "tool_call",
                                }
                            ],
                        ),
                        AIMessage(content="Plan approved."),
                    ]
                ),
            )
            result = plan.invoke(
                {"messages": [HumanMessage(content="plan")]}, config=config
            )
            self.assertTrue(result["__interrupt__"])
            plan.invoke(
                Command(
                    resume=deep_chat.resume_payload(
                        [deep_chat.decision_for_verdict("approve")]
                    )
                ),
                config=config,
            )
            model = LocalModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "write_file",
                                "args": {
                                    "file_path": f"result-{n}.txt",
                                    "content": "done",
                                },
                                "id": f"write-{n}",
                                "type": "tool_call",
                            }
                        ],
                    )
                    for n in range(coder.INITIAL_MODEL_LIMIT - 1)
                ]
                + [AIMessage(content="DONE #1550")]
            )
            implementation = coder.build_agent(
                root, "implement", model=model, checkpointer=saver
            )
            result = implementation.invoke(
                {"messages": [HumanMessage(content="implement")]}, config=config
            )
            self.assertEqual(result["messages"][-1].content, "DONE #1550")
            with self.assertRaises(ModelCallLimitExceededError):
                implementation.invoke(
                    {"messages": [HumanMessage(content="verify again")]}, config=config
                )

    def test_real_repair_agent_stops_repeated_grep_at_model_budget(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.py").write_text("pass\n")
            model = LocalModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "grep",
                                "args": {"path": "a.py", "pattern": "removed_helper"},
                                "id": f"probe-{n}",
                                "type": "tool_call",
                            }
                        ],
                    )
                    for n in range(coder.REPAIR_MODEL_LIMIT)
                ]
            )
            graph = coder.build_agent(root, "repair", model=model, repair=True)
            limit = coder.REPAIR_MODEL_LIMIT
            with self.assertRaisesRegex(
                ModelCallLimitExceededError, rf"thread limit \({limit}/{limit}\)"
            ):
                graph.invoke(
                    {"messages": [HumanMessage(content="fix")]},
                    config={"recursion_limit": 100},
                )

    def test_repair_planner_and_executor_receive_findings_and_have_no_nested_retries(
        self,
    ):
        notes = json.dumps(
            {"verified": False, "findings": [{"description": "fresh tests required"}]}
        )
        planner = mock.Mock(return_value={"plan": "fix [files: a.py]"})
        with tempfile.TemporaryDirectory() as root:
            graph = pipeline.Pipeline(
                root, "fix", plan_fn=planner, answers={"choice": "keep"}
            ).make_coder(
                "### Allowed Files\n- `a.py`\n", worktree_root=root, repair_notes=notes
            )
            graph.plan_fn("task", root, {"errors": ["gate failed"]})
            context = planner.call_args.args[2]
            self.assertEqual(context["repair_notes"], notes)
            self.assertEqual(context["errors"], ["gate failed"])
            self.assertEqual(context["choice"], "keep")
            self.assertEqual((graph.max_fix_loops, graph.max_replans), (0, 0))
            with (
                mock.patch.object(
                    coder,
                    "run_coder",
                    return_value=("DONE", {}, [], 0, "ollama:test", "trace", True, 0),
                ) as executor,
                mock.patch("backend.shared.ledger.record"),
            ):
                graph.implement_fn("task", "plan", root, [])
            self.assertIn(notes, executor.call_args.args[1])
            self.assertTrue(executor.call_args.kwargs["repair"])
            self.assertEqual(executor.call_args.kwargs["repair_files"], ["a.py"])

    def test_read_only_repair_failure_keeps_gate_errors_and_fails_closed(self):
        empty = RuntimeError(coder.NO_WRITES)
        with tempfile.TemporaryDirectory() as root:
            graph = pipeline.Pipeline(root, "fix").make_coder(
                "### Allowed Files\n- `a.py`\n",
                worktree_root=root,
                repair_notes="gate failed",
            )
            gate_errors = ["scope violation: outside.py"]
            with (
                mock.patch.object(coder, "run_coder", side_effect=empty),
                mock.patch("backend.shared.ledger.record"),
            ):
                with self.assertRaises(RuntimeError) as repaired:
                    graph.implement_fn("task", "plan", root, gate_errors)
                with self.assertRaises(RuntimeError) as first_pass:
                    graph.implement_fn("task", "plan", root, [])
        self.assertIn(coder.NO_WRITES, str(repaired.exception))
        self.assertIn("scope violation: outside.py", str(repaired.exception))
        self.assertEqual(str(first_pass.exception), coder.NO_WRITES)

    def test_model_budget_bounds_loops_even_when_tool_ids_are_reused(self):
        from langchain.agents.middleware.model_call_limit import (
            ModelCallLimitExceededError,
        )
        from langchain_core.messages import AIMessage, HumanMessage
        from tests.test_coder_outcome import LocalModel

        with tempfile.TemporaryDirectory() as root:
            model = LocalModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "ls",
                                "args": {"path": "."},
                                "id": "reused",
                                "type": "tool_call",
                            }
                        ],
                    )
                ]
            )
            graph = coder.build_agent(root, "repair", model=model, repair=True)
            with self.assertRaises(ModelCallLimitExceededError):
                graph.invoke(
                    {"messages": [HumanMessage(content="fix")]},
                    config={"recursion_limit": 100},
                )

    def test_default_planner_includes_repair_notes(self):
        model = mock.Mock()
        model.invoke.return_value = "1. Fix [files: a.py]"
        with mock.patch.object(coder, "build_model", return_value=(model, {})):
            coder._default_plan_fn("brief", "/tmp", {"repair_notes": "audit finding"})
        self.assertIn("Repair notes:\naudit finding", model.invoke.call_args.args[0])

    def test_truncated_audit_blocks_instead_of_spending_on_code_repair(self):
        findings = json.dumps(
            {
                "verified": False,
                "truncated": True,
                "findings": ["investigation repeat limit"],
            }
        )
        state = {
            "valid": True,
            "verdict": {"status": "CHANGES_REQUESTED", "findings": findings},
        }
        with (
            mock.patch.object(auditor, "AuditorGraph"),
            mock.patch.object(pipeline, "drive_to_settled", return_value=state),
        ):
            status, _, detail = pipeline.Pipeline("/tmp", "fix").stage_audit("/tmp")
        self.assertEqual(status, pipeline.BLOCKED)
        self.assertEqual(detail, findings)

    def test_first_pass_retains_normal_retry_bounds(self):
        graph = pipeline.Pipeline("/tmp", "fix").make_coder("spec")
        self.assertEqual((graph.max_fix_loops, graph.max_replans), (3, 1))


class TestModelTierSelector(unittest.TestCase):
    def test_first_attempt_defaults_to_fast_tier(self):
        tier, used = pipeline.choose_model_tier(
            attempt=1, failure_class=None, escalations_used=0, max_escalations=1
        )
        self.assertEqual(tier, "fast")
        self.assertEqual(used, 0)

        # Even with prior failure class, attempt 1 stays on fast tier
        tier, used = pipeline.choose_model_tier(
            attempt=1,
            failure_class="test_failure",
            escalations_used=0,
            max_escalations=1,
        )
        self.assertEqual(tier, "fast")
        self.assertEqual(used, 0)

    def test_mechanical_failure_stays_on_fast_tier(self):
        for attempt in (1, 2, 3, 4):
            tier, used = pipeline.choose_model_tier(
                attempt=attempt,
                failure_class="mechanical",
                escalations_used=0,
                max_escalations=1,
            )
            self.assertEqual(tier, "fast")
            self.assertEqual(used, 0)

    def test_test_failure_escalates_to_strong_tier_once(self):
        # Attempt 2 escalates to strong
        tier, used = pipeline.choose_model_tier(
            attempt=2,
            failure_class="test_failure",
            escalations_used=0,
            max_escalations=1,
        )
        self.assertEqual(tier, "strong")
        self.assertEqual(used, 1)

        # Subsequent retry is capped by max_escalations=1, stays on fast
        tier, used = pipeline.choose_model_tier(
            attempt=3,
            failure_class="test_failure",
            escalations_used=1,
            max_escalations=1,
        )
        self.assertEqual(tier, "fast")
        self.assertEqual(used, 1)

    def test_audit_semantic_failure_escalates_to_strong_tier_once(self):
        tier, used = pipeline.choose_model_tier(
            attempt=2,
            failure_class="audit_semantic",
            escalations_used=0,
            max_escalations=1,
        )
        self.assertEqual(tier, "strong")
        self.assertEqual(used, 1)

        tier, used = pipeline.choose_model_tier(
            attempt=3,
            failure_class="audit_semantic",
            escalations_used=1,
            max_escalations=1,
        )
        self.assertEqual(tier, "fast")
        self.assertEqual(used, 1)

    def test_call_cap_escalates_once_then_stays_fast(self):
        cap = pipeline.CALL_CAP
        self.assertEqual(pipeline.choose_model_tier(1, cap, 0), ("fast", 0))
        self.assertEqual(pipeline.choose_model_tier(2, cap, 0), ("strong", 1))
        self.assertEqual(pipeline.choose_model_tier(3, cap, 1), ("fast", 1))


class TestDeterministicPreAuditGate(unittest.TestCase):
    def test_pre_audit_catches_ast_syntax_error_as_mechanical(self):
        with tempfile.TemporaryDirectory() as root:
            # Create a broken py file with invalid syntax
            broken_file = Path(root, "broken.py")
            broken_file.write_text("def broken_func(\n")
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = SimpleNamespace(
                    stdout="broken.py\n", stderr="", returncode=0
                )
                pipe = pipeline.Pipeline(root, "fix syntax")
                passed, f_class, reason = pipe.pre_audit_check(root)
            self.assertFalse(passed)
            self.assertEqual(f_class, "mechanical")
            self.assertIn("SyntaxError", reason)

    def test_pre_audit_fails_closed_when_changed_file_listing_fails(self):
        with tempfile.TemporaryDirectory() as root:
            pipe = pipeline.Pipeline(root, "fix syntax")
            with mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = SimpleNamespace(
                    stdout="",
                    stderr="fatal: bad revision 'origin/main'",
                    returncode=128,
                )
                passed, f_class, reason = pipe.pre_audit_check(root)
            self.assertFalse(passed)
            self.assertEqual(f_class, "mechanical")
            self.assertIn("could not list changed files", reason)
            with mock.patch("subprocess.run", side_effect=OSError("no git")):
                passed, f_class, reason = pipe.pre_audit_check(root)
            self.assertFalse(passed)
            self.assertEqual(f_class, "mechanical")
            self.assertIn("could not list changed files", reason)

    def test_pre_audit_catches_verification_command_failure_as_test_failure(self):
        with tempfile.TemporaryDirectory() as root:
            pipe = pipeline.Pipeline(
                root,
                "fix tests",
                verification_commands=[["python3", "-m", "pytest", "tests/"]],
            )
            with (
                mock.patch("subprocess.run") as mock_run,
                mock.patch("pathlib.Path.is_file", return_value=False),
            ):
                mock_run.side_effect = [
                    # git diff proc
                    SimpleNamespace(stdout="", stderr="", returncode=0),
                    # verification command proc
                    SimpleNamespace(
                        stdout="FAILED test_something.py::test_calc\n",
                        stderr="",
                        returncode=1,
                    ),
                ]
                passed, f_class, reason = pipe.pre_audit_check(root)
            self.assertFalse(passed)
            self.assertEqual(f_class, "test_failure")
            self.assertIn("FAILED test_something.py::test_calc", reason)

    def test_patch_failing_pre_audit_routes_back_to_coder_skipping_auditor(self):
        with tempfile.TemporaryDirectory() as root:
            coder_calls = []
            audit_mock = mock.Mock()

            def fake_code(brief, worktree_root=None, repair_notes=""):
                coder_calls.append((worktree_root, repair_notes))
                return True, {}, root

            pre_audit_outcomes = [
                (False, "mechanical", "SyntaxError: unexpected EOF"),
                (True, None, ""),
            ]

            def fake_pre_audit(worktree_root):
                return pre_audit_outcomes.pop(0)

            pipe = pipeline.Pipeline(
                root,
                "fix helper",
                pre_audit_fn=fake_pre_audit,
                audit_fn=audit_mock,
                max_repair_cycles=2,
            )
            pipe.stage_init = lambda: (True, "ready")
            pipe.stage_spec = lambda: (True, {}, "spec")
            pipe.stage_code = fake_code
            pipe.stage_audit = lambda wt: (pipeline.APPROVED, {}, "approved")
            pipe.stage_cleanup = lambda wt: (True, "")

            result = pipe.run()
            self.assertEqual(result["status"], pipeline.APPROVED)
            self.assertEqual(len(coder_calls), 2)
            # The first repair attempt receives the pre-audit error notes
            self.assertIn("SyntaxError: unexpected EOF", coder_calls[1][1])
            # Auditor LLM was never called during the first pre-audit failure
            audit_mock.assert_not_called()

    def test_pipeline_terminates_cleanly_when_repair_cycles_exhausted(self):
        with tempfile.TemporaryDirectory() as root:

            def fake_pre_audit(worktree_root):
                return False, "mechanical", "SyntaxError: unfixable"

            pipe = pipeline.Pipeline(
                root,
                "stuck issue",
                pre_audit_fn=fake_pre_audit,
                max_repair_cycles=2,
            )
            pipe.stage_init = lambda: (True, "ready")
            pipe.stage_spec = lambda: (True, {}, "spec")
            pipe.stage_code = lambda brief, worktree_root=None, repair_notes="": (
                True,
                {},
                root,
            )
            result = pipe.run()
            self.assertEqual(result["status"], pipeline.CHANGES_REQUESTED)
            self.assertIn("repair cycles exhausted (2/2)", result["detail"])


class TestStartModelTier(unittest.TestCase):
    def test_default_start_is_fast_and_follows_choose_model_tier(self):
        with mock.patch.dict("os.environ", {"S5_START_MODEL_TIER": ""}):
            run = pipeline.Pipeline("/tmp", "fix")
        self.assertEqual(run.current_model_tier, "fast")
        self.assertEqual(run._next_tier(2, "test_failure"), ("strong", 1))
        self.assertEqual(run._next_tier(2, "mechanical"), ("fast", 0))

    def test_strong_start_never_drops_back_to_fast(self):
        with mock.patch.dict("os.environ", {"S5_START_MODEL_TIER": "strong"}):
            run = pipeline.Pipeline("/tmp", "fix")
        self.assertEqual(run.current_model_tier, "strong")
        for failure_class in ("mechanical", "test_failure", "audit_semantic"):
            self.assertEqual(run._next_tier(3, failure_class)[0], "strong")

    def test_unknown_start_tier_fails_loudly(self):
        with mock.patch.dict("os.environ", {"S5_START_MODEL_TIER": "huge"}):
            with self.assertRaisesRegex(ValueError, "S5_START_MODEL_TIER"):
                pipeline.Pipeline("/tmp", "fix")
