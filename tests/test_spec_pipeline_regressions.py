import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import issue_generator
import pipeline


class TestExplicitScope(unittest.TestCase):
    @unittest.skipUnless(shutil.which("td"), "td CLI not installed")
    def test_real_td_scope_is_explicit_and_normalized(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "backend").mkdir()
            Path(root, "backend/repair_worker.py").write_text("pass\n")
            graph = issue_generator.IssueGeneratorGraph(root)
            for path, expected in (
                ("./backend/repair_worker.py", True),
                ("missing.py", False),
            ):
                text = (
                    "## Problem Statement\nDo not change /api/, CI/ or compiler/.\n"
                    "## Goal\nFix the helper.\n## Scope\n### Allowed Files\n"
                    f"- `{path}`\n## Definition of Done\nHelper is called.\n"
                    "## Acceptance Criteria\n- [ ] Helper is called.\n"
                    "## Verification Commands\n```bash\npython3 -m pytest tests/\n```\n"
                )
                valid, data, errors = graph.validate_spec(text)
                self.assertEqual(valid, expected, errors)
                self.assertEqual(data["allowed_files"], [path.removeprefix("./")])
                self.assertEqual(
                    data["verification_commands"], ["python3 -m pytest tests/"]
                )

    def test_empty_and_unsafe_scope(self):
        self.assertEqual(issue_generator.explicit_allowed_paths(""), [])
        graph = issue_generator.IssueGeneratorGraph(
            "/tmp",
            td_runner=lambda args: (0, json.dumps({"valid": True, "errors": []})),
        )
        for path in ("/api/", "../outside.py"):
            valid, _, errors = graph.validate_spec(f"### Allowed Files\n- `{path}`\n")
            self.assertFalse(valid)
            self.assertIn("repo-relative", errors[0])

    def test_non_scope_errors_remain_blocking(self):
        graph = issue_generator.IssueGeneratorGraph(
            "/tmp",
            td_runner=lambda args: (
                1,
                json.dumps(
                    {"valid": False, "errors": ["missing required section: Goal"]}
                ),
            ),
        )
        self.assertEqual(graph.validate_spec("")[2], ["missing required section: Goal"])


class TestSpecResume(unittest.TestCase):
    @unittest.skipUnless(
        importlib.util.find_spec("langgraph"), "langgraph not installed"
    )
    def test_real_spec_interrupt_resumes_with_answers(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "widget.py").write_text("pass\n")
            pipe = pipeline.Pipeline(
                root,
                "fix widget",
                spec_model_fn=lambda: object(),
                investigate_fn=lambda *args: "widget.py:1 evidence",
                td_run=lambda *args: (
                    0,
                    json.dumps({"status": "ready", "valid": True}),
                ),
            )
            question = "Keep the helper?"

            def answer(interrupts, state):
                self.assertEqual(
                    pipeline.final_interrupt(interrupts)["questions"], [question]
                )
                return {question: "yes"}

            pipe._spec_policy = answer
            with (
                mock.patch.object(
                    issue_generator.IssueGeneratorGraph,
                    "draft_text",
                    return_value=f"### Allowed Files\n- `widget.py`\n- {question}",
                ),
                mock.patch("backend.shared.ledger.record"),
            ):
                ok, state, text = pipe.stage_spec()
            self.assertTrue(ok, text)
            self.assertEqual(state["brief"], "fix widget")
            self.assertEqual(state["answers"][question], "yes")
            self.assertIn("yes", text)


class TestFailureDetail(unittest.TestCase):
    def test_spec_failures_keep_their_reason_in_result(self):
        for state, detail, expected in (
            ({}, "spec drive exhausted", "spec drive exhausted"),
            ({"errors": ["missing scope"]}, "invalid spec", "missing scope"),
            ({"errors": []}, ["invalid spec"], "invalid spec"),
            ({}, "", "spec failed"),
        ):
            with self.subTest(state=state, detail=detail):
                pipe = pipeline.Pipeline("/tmp", "task")
                with (
                    mock.patch.object(pipe, "stage_init", return_value=(True, {})),
                    mock.patch.object(
                        pipe, "stage_spec", return_value=(False, state, detail)
                    ),
                ):
                    result = pipe.run()
                self.assertEqual(result["detail"], expected)
                self.assertEqual(result["status"], pipeline.BLOCKED)


@unittest.skipUnless(
    importlib.util.find_spec("langgraph"), "agent dependencies not installed"
)
class TestSpecUsage(unittest.TestCase):
    def test_investigation_calls_survive_recursion_truncation(self):
        from backend.shared import ledger
        from langchain_core.language_models.fake_chat_models import (
            FakeMessagesListChatModel,
        )
        from langchain_core.messages import AIMessage
        from langchain_core.tools import tool

        class Model(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        @tool(description="Read local evidence")
        def lookup() -> str:
            return "widget.py:1 evidence"

        usage = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
        for limit, calls in ((25, 2), (2, 1)):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as root:
                model = Model(
                    responses=[
                        AIMessage(
                            content="",
                            tool_calls=[{"id": "read", "name": "lookup", "args": {}}],
                            usage_metadata=usage,
                        ),
                        AIMessage(content="widget.py:1 evidence", usage_metadata=usage),
                    ]
                )
                graph = issue_generator.IssueGeneratorGraph(
                    root,
                    model_fn=lambda model=model: model,
                    thread_id="proof-investigation",
                )
                graph.last_spec = {
                    "provider": "nebius",
                    "model": "zai-org/GLM-5.3-Flash",
                }
                path = Path(root, "ledger.jsonl")
                with (
                    mock.patch.object(ledger, "LEDGER", str(path)),
                    mock.patch.dict(
                        os.environ,
                        {
                            "DEEP_AGENT_STATE_DIR": root,
                            "S5_SPEC_RECURSION_LIMIT": str(limit),
                        },
                    ),
                    mock.patch.object(
                        graph, "_investigator_tools", return_value=[lookup]
                    ),
                ):
                    evidence = graph.investigate_text("fix", "draft")
                self.assertIn("widget.py", evidence)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual(len(rows), calls)
                self.assertEqual(sum(row["cost_usd"] for row in rows), calls * 0.0002)
                self.assertTrue(
                    all(row["extra"]["phase"] == "investigation" for row in rows)
                )

    def test_validation_failure_keeps_completed_draft_usage(self):
        from backend.shared import ledger
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage

        message = AIMessage(
            content="draft",
            usage_metadata={
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
            },
        )
        with tempfile.TemporaryDirectory() as root:
            Path(root, "widget.py").write_text("pass\n")
            graph = issue_generator.IssueGeneratorGraph(
                root,
                model_fn=lambda: GenericFakeChatModel(messages=iter([message])),
                investigate_fn=lambda *args: "widget.py:1 evidence",
                td_runner=lambda args: (
                    0,
                    json.dumps(
                        {"status": "ready", "valid": False, "errors": ["bad spec"]}
                    ),
                ),
                max_loops=1,
                thread_id="proof-spec-accounting",
            )
            graph.last_spec = {"provider": "nebius", "model": "zai-org/GLM-5.3-Flash"}
            path = Path(root, "ledger.jsonl")
            with (
                mock.patch.object(ledger, "LEDGER", str(path)),
                mock.patch.dict(os.environ, {"DEEP_AGENT_STATE_DIR": root}),
            ):
                state = graph.run("fix widget")
            self.assertFalse(state["valid"])
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["usage"], message.usage_metadata)
            self.assertEqual(rows[0]["cost_usd"], 0.0002)
            self.assertEqual(rows[0]["extra"]["phase"], "draft")
            trace = Path(rows[0]["extra"]["trace_path"]).read_text()
            self.assertIn('"input_tokens": 1000', trace)

    def test_missing_usage_and_model_errors_are_unknown_not_free(self):
        from backend.shared import ledger
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage

        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                graph = issue_generator.IssueGeneratorGraph(
                    root, thread_id="proof-missing-usage"
                )
                graph.last_spec = {
                    "provider": "nebius",
                    "model": "zai-org/GLM-5.3-Flash",
                }
                callback = graph._usage_callback("investigation")
                model = GenericFakeChatModel(
                    messages=iter([AIMessage(content="answer")])
                )
                path = Path(root, "ledger.jsonl")
                with (
                    mock.patch.object(ledger, "LEDGER", str(path)),
                    mock.patch.dict(os.environ, {"DEEP_AGENT_STATE_DIR": root}),
                ):
                    if failure:
                        with (
                            mock.patch.object(
                                GenericFakeChatModel,
                                "_generate",
                                side_effect=RuntimeError("failed"),
                            ),
                            self.assertRaises(RuntimeError),
                        ):
                            model.invoke("task", config={"callbacks": [callback]})
                    else:
                        model.invoke("task", config={"callbacks": [callback]})
                row = json.loads(path.read_text())
                self.assertIsNone(row["cost_usd"])
                self.assertTrue(row["metered"])
                self.assertTrue(row["extra"]["usage_missing"])
                self.assertEqual(row["extra"]["phase"], "investigation")


class TestUnknownSpend(unittest.TestCase):
    def test_unrated_usage_is_repriced_when_rates_become_available(self):
        from backend.shared import ledger
        from backend.shared import model_router
        from backend.shared import pricing
        from backend.shared import runs

        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root, "ledger.jsonl"))
            with (
                mock.patch.object(ledger, "LEDGER", path),
                mock.patch.object(runs, "LEDGER", path),
                mock.patch.dict(
                    os.environ,
                    {"NEBIUS_API_KEY": "dummy", "NEBIUS_SPEND_CAP_USD": "5"},
                    clear=True,
                ),
            ):
                ledger.record(
                    "issue_generator.py",
                    "Future/Model",
                    "proof-unrated",
                    [],
                    0,
                    provider="nebius",
                    usage={
                        "input_tokens": 1000,
                        "output_tokens": 1000,
                        "total_tokens": 2000,
                    },
                )
                self.assertEqual(
                    runs.total_metered_spend(), {"spent": 0.0, "unknown": 1}
                )
                with self.assertRaises(RuntimeError):
                    model_router.resolve_llm(prefer="nebius")
                with mock.patch.dict(pricing.RATE_CARD, {"Future/Model": (1.0, 2.0)}):
                    self.assertEqual(
                        runs.total_metered_spend(), {"spent": 0.003, "unknown": 0}
                    )
                    self.assertEqual(
                        model_router.resolve_llm(prefer="nebius")["provider"], "nebius"
                    )
                self.assertIsNone(json.loads(Path(path).read_text())["cost_usd"])

    def test_recorded_unknown_remote_cost_blocks_router(self):
        from backend.shared import ledger
        from backend.shared import model_router
        from backend.shared import runs

        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root, "ledger.jsonl"))
            with (
                mock.patch.object(ledger, "LEDGER", path),
                mock.patch.object(runs, "LEDGER", path),
                mock.patch.dict(
                    os.environ,
                    {
                        "NEBIUS_ONLY": "1",
                        "NEBIUS_API_KEY": "dummy",
                        "NEBIUS_SPEND_CAP_USD": "5",
                    },
                    clear=True,
                ),
            ):
                ledger.record(
                    "issue_generator.py",
                    "zai-org/GLM-5.3-Flash",
                    "proof-unknown-spend",
                    [],
                    0,
                    provider="nebius",
                    extra={"usage_missing": True},
                )
                self.assertEqual(
                    runs.total_metered_spend(), {"spent": 0.0, "unknown": 1}
                )
                with self.assertRaises(RuntimeError):
                    model_router.resolve_llm(prefer="nebius")

    def test_local_and_free_missing_usage_still_cost_zero(self):
        from backend.shared import ledger
        from backend.shared import runs

        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root, "ledger.jsonl"))
            with mock.patch.object(ledger, "LEDGER", path):
                for provider, model in (
                    ("ollama", "qwen3:8b"),
                    ("openrouter", "some/model:free"),
                ):
                    row = ledger.record(
                        "issue_generator.py",
                        model,
                        "proof-free-spend",
                        [],
                        0,
                        provider=provider,
                        extra={"usage_missing": True},
                    )
                    self.assertEqual(row["cost_usd"], 0)
            self.assertEqual(
                runs.total_metered_spend(ledger_path=path), {"spent": 0.0, "unknown": 0}
            )


class TestSpecBounds(unittest.TestCase):
    """Deterministic bounds for spec generation (issue #1404)."""

    def test_env_positive_int(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S5_SPEC_MAX_TURNS", None)
            self.assertEqual(
                issue_generator._env_positive_int("S5_SPEC_MAX_TURNS", 4), 4
            )
        with mock.patch.dict(os.environ, {"S5_SPEC_MAX_TURNS": "7"}):
            self.assertEqual(
                issue_generator._env_positive_int("S5_SPEC_MAX_TURNS", 4), 7
            )
        for bad in ("0", "-2", "nope"):
            with mock.patch.dict(os.environ, {"S5_SPEC_MAX_TURNS": bad}):
                with self.assertRaises(ValueError):
                    issue_generator._env_positive_int("S5_SPEC_MAX_TURNS", 4)

    def test_recursion_limit_derives_from_turn_budget(self):
        graph = issue_generator.IssueGeneratorGraph("/tmp")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S5_SPEC_RECURSION_LIMIT", None)
            self.assertEqual(graph._spec_recursion_limit(), 9)  # 2*4 + 1
        with mock.patch.dict(os.environ, {"S5_SPEC_RECURSION_LIMIT": "30"}):
            self.assertEqual(graph._spec_recursion_limit(), 30)
        with mock.patch.dict(os.environ, {"S5_SPEC_RECURSION_LIMIT": "0"}):
            with self.assertRaises(ValueError):
                graph._spec_recursion_limit()

    def test_explicit_bounds_validated(self):
        for kwargs in (
            {"max_spec_turns": 0},
            {"max_spec_tool_calls": -1},
            {"max_spec_turns": "4"},
            {"max_spec_tool_calls": True},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    issue_generator.IssueGeneratorGraph("/tmp", **kwargs)
        graph = issue_generator.IssueGeneratorGraph(
            "/tmp", max_spec_turns=2, max_spec_tool_calls=1
        )
        self.assertEqual((graph.max_spec_turns, graph.max_spec_tool_calls), (2, 1))

    def test_tool_output_truncation_head_tail(self):
        text = "h" * 3000 + "m" * 3000 + "t" * 3000
        out = issue_generator._truncate_tool_output(
            text, issue_generator.SPEC_TOOL_OUTPUT_LIMIT
        )
        self.assertTrue(out.startswith("h" * 200))
        self.assertTrue(out.endswith("t" * 200))
        self.assertIn("chars omitted", out)
        self.assertLess(len(out), len(text))

    def test_preseed_context_pack_deterministic(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "widget.py").write_text("def widget():\n    return 1\n")
            Path(root, "README.md").write_text("# repo\n")
            graph = issue_generator.IssueGeneratorGraph(root)
            with mock.patch(
                "retrieval.search_td", return_value="[widget.py:1] rag hit"
            ):
                pack = graph.preseed_context_pack("fix widget.py crash")
                # cached: second call skips every source
                self.assertIs(pack, graph.preseed_context_pack("fix widget.py crash"))
            self.assertIn("Repo top level:", pack)
            self.assertIn("widget.py", pack)
            self.assertIn("def widget():", pack)
            self.assertIn("rag hit", pack)
            self.assertLessEqual(len(pack), issue_generator.PRESEED_CONTEXT_LIMIT + 60)

    def test_preseed_never_raises_on_empty_repo(self):
        with tempfile.TemporaryDirectory() as root:
            graph = issue_generator.IssueGeneratorGraph(root)
            pack = graph.preseed_context_pack("vague brief with no file")
            self.assertIn("Repo top level:", pack)

    def test_preseed_does_not_excerpt_archived_retry(self):
        with tempfile.TemporaryDirectory() as root:
            archived = Path(root, ".worktrees/.queue-retry/run/old.py")
            archived.parent.mkdir(parents=True)
            archived.write_text("old runner implementation\n")
            Path(root, "alias.py").symlink_to(archived)
            graph = issue_generator.IssueGeneratorGraph(root)
            with mock.patch("retrieval.search_td", return_value="no doc hits"):
                pack = graph.preseed_context_pack("fix alias.py")
            self.assertNotIn("old runner implementation", pack)
            self.assertNotIn(".worktrees", pack)

    def test_draft_prompt_carries_preseed_pack(self):
        prompts = []

        class Stub:
            def invoke(self, prompt, **kwargs):
                prompts.append(prompt)
                return "draft"

        graph = issue_generator.IssueGeneratorGraph(
            "/tmp", model_fn=lambda: Stub(), preseed_fn=lambda brief: "PACK-CONTENTS"
        )
        text = graph.draft_text("fix widget", "", [], {})
        self.assertEqual(text, "draft")
        self.assertEqual(len(prompts), 1)
        self.assertIn("PACK-CONTENTS", prompts[0])


@unittest.skipUnless(
    importlib.util.find_spec("langgraph"), "agent dependencies not installed"
)
class TestSpecToolBudget(unittest.TestCase):
    """Live agent-loop proof of the tool budget and forced final turn."""

    def test_exhausted_budget_requests_cited_summary_even_without_bound_tools(self):
        from langchain_core.messages import HumanMessage

        middleware = issue_generator.force_final_turn({"exhausted": True})
        for tools in (["lookup"], []):
            with self.subTest(tools=tools):
                messages = [HumanMessage(content="Gather evidence")]
                request = mock.Mock(tools=tools, messages=messages)
                handler = mock.Mock()
                middleware.wrap_model_call(request, handler)
                override = request.override.call_args.kwargs
                self.assertEqual(override["tools"], [])
                self.assertEqual(override["tool_choice"], "none")
                self.assertEqual(override["messages"][:-1], messages)
                prompt = override["messages"][-1].content
                self.assertIn("final evidence summary", prompt)
                self.assertIn("repo file path:line", prompt)
                self.assertIn("do not request tools or emit <tool_call> markup", prompt)
                self.assertEqual(len(messages), 1)
                handler.assert_called_once_with(request.override.return_value)

    def test_unspent_budget_leaves_request_unchanged(self):
        middleware = issue_generator.force_final_turn({"exhausted": False})
        request, handler = mock.Mock(), mock.Mock()
        middleware.wrap_model_call(request, handler)
        request.override.assert_not_called()
        handler.assert_called_once_with(request)

    def test_1537_markup_only_reply_is_rejected_before_grounding(self):
        replay = (
            "<tool_call>grep_repo<arg_key>pattern</arg_key>"
            "<arg_value>def post_entry|def read_latest|def ledger_read</arg_value>"
            "</tool_call><tool_call>grep_repo<arg_key>pattern</arg_key>"
            "<arg_value>class RunnerTests|def test_.*ledger|S5-Queue|S5-Local"
            "</arg_value></tool_call>"
        )
        for evidence in (
            replay,
            " <tool_call>read_repo_file<arg_value>widget.py:1</arg_value></tool_call> ",
        ):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as root:
                Path(root, "widget.py").write_text("pass\n")
                graph = issue_generator.IssueGeneratorGraph(
                    root,
                    investigate_fn=lambda *_, evidence=evidence: evidence,
                    model_fn=lambda: None,
                    max_loops=5,
                )
                with (
                    mock.patch.object(graph, "td_init", return_value=(True, {})),
                    mock.patch.object(graph, "draft_text", return_value="Draft"),
                    mock.patch.object(
                        issue_generator,
                        "evidence_cites_repo_file",
                        side_effect=AssertionError("markup reached grounding"),
                    ) as grounding,
                ):
                    result = graph.run("Fix #1537")
                grounding.assert_not_called()
                self.assertFalse(result.get("valid"))
                self.assertTrue(result["off_brief"])
                self.assertIn("only tool-call markup", result["errors"][0])
                self.assertEqual(result["attempts"], 1)

    def test_empty_and_cited_evidence_still_use_existing_grounding(self):
        for evidence, grounded in (("", False), ("widget.py:1 evidence", True)):
            with self.subTest(evidence=evidence), tempfile.TemporaryDirectory() as root:
                Path(root, "widget.py").write_text("pass\n")
                graph = issue_generator.IssueGeneratorGraph(
                    root,
                    investigate_fn=lambda *_, evidence=evidence: evidence,
                    model_fn=lambda: None,
                    max_loops=1,
                )
                with (
                    mock.patch.object(graph, "td_init", return_value=(True, {})),
                    mock.patch.object(graph, "draft_text", return_value="Draft"),
                    mock.patch.object(
                        graph,
                        "validate_spec",
                        return_value=(False, {}, ["validation gate"]),
                    ) as validate,
                    mock.patch.object(
                        issue_generator,
                        "evidence_cites_repo_file",
                        wraps=issue_generator.evidence_cites_repo_file,
                    ) as grounding,
                ):
                    result = graph.run("Fix widget")
                grounding.assert_called_once_with(evidence, root)
                self.assertEqual(validate.called, grounded)
                self.assertFalse(result.get("valid"))
                self.assertEqual(bool(result.get("off_brief")), not grounded)

    def _run(self, responses, max_tool_calls):
        from backend.shared import ledger
        from langchain_core.language_models.fake_chat_models import (
            FakeMessagesListChatModel,
        )
        from langchain_core.tools import tool

        executed = []

        @tool(description="Read local evidence")
        def lookup() -> str:
            executed.append(1)
            return "widget.py:1 evidence"

        class Model(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        with tempfile.TemporaryDirectory() as root:
            graph = issue_generator.IssueGeneratorGraph(
                root,
                model_fn=lambda: Model(responses=list(responses)),
                max_spec_tool_calls=max_tool_calls,
                thread_id="proof-budget",
            )
            path = Path(root, "ledger.jsonl")
            with (
                mock.patch.object(ledger, "LEDGER", str(path)),
                mock.patch.dict(os.environ, {"DEEP_AGENT_STATE_DIR": root}),
                mock.patch.object(graph, "_investigator_tools", return_value=[lookup]),
                mock.patch.object(
                    graph, "preseed_context_pack", return_value="pack: widget.py"
                ),
            ):
                evidence = graph.investigate_text("fix widget", "draft")
        return evidence, executed

    def test_budget_strips_tools_for_final_turn(self):
        from langchain_core.messages import AIMessage

        responses = [
            AIMessage(
                content="",
                tool_calls=[{"id": "t1", "name": "lookup", "args": {}}],
            ),
            # the agent loop would let the model ask for another call
            # here; the spent budget must strip tool bindings instead
            AIMessage(content="widget.py:1 evidence, cited from the tool result"),
        ]
        evidence, executed = self._run(responses, max_tool_calls=1)
        self.assertEqual(len(executed), 1)
        self.assertIn("widget.py", evidence)

    def test_parallel_calls_over_budget_short_circuit(self):
        from langchain_core.messages import AIMessage

        responses = [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "t1", "name": "lookup", "args": {}},
                    {"id": "t2", "name": "lookup", "args": {"extra": 1}},
                ],
            ),
            AIMessage(content="widget.py:1 evidence from the single allowed call"),
        ]
        evidence, executed = self._run(responses, max_tool_calls=1)
        self.assertEqual(len(executed), 1)
        self.assertIn("widget.py", evidence)
