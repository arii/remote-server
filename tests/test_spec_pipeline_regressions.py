import json
import importlib.util
import shutil
import tempfile
import os
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
                mock.patch("ledger.record"),
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
        from langchain_core.language_models.fake_chat_models import (
            FakeMessagesListChatModel,
        )
        from langchain_core.messages import AIMessage
        from langchain_core.tools import tool
        import ledger

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
                    root, model_fn=lambda: model, thread_id="proof-investigation"
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
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        import ledger

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
        from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
        from langchain_core.messages import AIMessage
        import ledger

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
        import ledger
        import runs
        import pricing
        import model_router

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
        import ledger
        import runs
        import model_router

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
        import ledger
        import runs

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
