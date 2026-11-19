import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auditor
import ledger
import model_router


@unittest.skipUnless(
    importlib.util.find_spec("langgraph"), "agent dependencies not installed"
)
class TestSemanticLimits(unittest.TestCase):
    def exercise(self, responses, limit="25", tool=None, error=None, full=False):
        from langchain_core.language_models.fake_chat_models import (
            FakeMessagesListChatModel,
        )

        class Model(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

            def _generate(self, *args, **kwargs):
                if error is not None:
                    raise error
                return super()._generate(*args, **kwargs)

        with tempfile.TemporaryDirectory() as root:
            graph = auditor.AuditorGraph(root, thread_id="proof-semantic")
            spec = {"provider": "nebius", "model": "zai-org/GLM-5.3-Flash"}
            model = Model(responses=responses)
            path = Path(root, "ledger.jsonl")
            import ask_investigator

            previous = ask_investigator._checkout
            with (
                mock.patch.object(model_router, "resolve_llm", return_value=spec),
                mock.patch.object(
                    model_router, "build_chat_model", return_value=(model, spec)
                ),
                mock.patch.object(ledger, "LEDGER", str(path)),
                mock.patch.dict(
                    os.environ,
                    {"DEEP_AGENT_STATE_DIR": root, "S5_AUDIT_RECURSION_LIMIT": limit},
                ),
                mock.patch.object(
                    ask_investigator,
                    "read_repo_file",
                    tool or ask_investigator.read_repo_file,
                ),
            ):
                if error is not None:
                    with self.assertRaises(type(error)):
                        graph.audit_once({"passed": True})
                    findings = None
                elif full:
                    with mock.patch.object(
                        graph, "hard_gate", return_value=(True, {}, [])
                    ):
                        graph.run()
                        state = graph.run(resume={"decision": "approve"})
                    findings = state["findings"]
                    self.assertEqual(state["verdict"]["status"], "APPROVED")
                else:
                    findings = graph.audit_once({"passed": True})
            self.assertEqual(ask_investigator._checkout, previous)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertTrue(all(row["extra"]["stage"] == "audit" for row in rows))
            return graph, findings, rows

    def message(self, content="", call=None):
        from langchain_core.messages import AIMessage

        return AIMessage(
            content=content,
            tool_calls=[call] if call else [],
            usage_metadata={
                "input_tokens": 1000,
                "output_tokens": 100,
                "total_tokens": 1100,
            },
        )

    def test_normal_final_verdict_and_per_call_usage(self):
        graph, findings, rows = self.exercise([self.message('{"findings": []}')])
        self.assertFalse(graph.audit_truncated)
        self.assertEqual(json.loads(findings), {"findings": []})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cost_usd"], 0.0002)
        self.assertNotIn("error", rows[0]["extra"])

    def test_repeat_budget_never_reexecutes_tool_and_rejects_with_evidence(self):
        from langchain_core.tools import tool

        called = []

        @tool(description="Read evidence")
        def read_repo_file(path: str) -> str:
            called.append(path)
            return "widget.py:1 verified evidence"

        responses = [
            self.message(
                call={
                    "id": str(i),
                    "name": "read_repo_file",
                    "args": {"path": "widget.py"},
                }
            )
            for i in range(4)
        ]
        graph, findings, rows = self.exercise(responses, tool=read_repo_file)
        self.assertTrue(graph.audit_truncated)
        self.assertEqual(called, ["widget.py"])
        self.assertEqual(len(rows), 4)
        self.assertIn("verified evidence", findings)
        self.assertTrue(auditor.blocking_findings(findings))
        verdict = graph.build_verdict({"approved": True, "findings": findings})
        self.assertEqual(verdict["status"], "CHANGES_REQUESTED")

    def test_recursion_truncation_rejects_even_with_prior_clear_ai_text(self):
        call = {"id": "read", "name": "read_repo_file", "args": {"path": "widget.py"}}
        graph, findings, rows = self.exercise(
            [self.message('{"findings": []}', call=call)], limit="2"
        )
        self.assertTrue(graph.audit_truncated)
        self.assertTrue(auditor.blocking_findings(findings))
        self.assertIn("GraphRecursionError", findings)
        self.assertEqual(len(rows), 1)

    def test_persist_summary_does_not_double_count_calls(self):
        _graph, _findings, rows = self.exercise(
            [self.message('{"findings": []}')], full=True
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["cost_usd"] for row in rows), 0.0002)
        self.assertFalse(rows[-1]["metered"])
        self.assertFalse(rows[-1]["extra"]["llm_call"])

    def test_model_error_and_interrupt_propagate_with_durable_usage_row(self):
        for error in (RuntimeError("broken"), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__):
                _graph, _findings, rows = self.exercise([self.message()], error=error)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["extra"]["error"], type(error).__name__)
                self.assertIsNone(rows[0]["cost_usd"])

    def test_empty_final_reply_needs_human(self):
        _graph, findings, _rows = self.exercise([self.message()])
        self.assertTrue(auditor.blocking_findings(findings))
        self.assertIn("no final AI verdict", findings)

    def test_invalid_limit(self):
        for value in ("0", "-1", "invalid"):
            with (
                self.subTest(value=value),
                mock.patch.dict(os.environ, {"S5_AUDIT_RECURSION_LIMIT": value}),
                self.assertRaises(ValueError),
            ):
                auditor.AuditorGraph("/tmp").audit_once({})

    def test_callback_error_interrupt_and_duplicate_close_persist_once(self):
        from llm_usage import usage_callback

        for error in (RuntimeError("failure"), KeyboardInterrupt()):
            with (
                self.subTest(error=type(error).__name__),
                tempfile.TemporaryDirectory() as root,
                mock.patch.object(ledger, "LEDGER", str(Path(root, "ledger.jsonl"))),
                mock.patch.dict(os.environ, {"DEEP_AGENT_STATE_DIR": root}),
            ):
                callback = usage_callback(
                    {"provider": "nebius", "model": "zai-org/GLM-5.3-Flash"},
                    "proof-audit-error",
                    script="auditor.py",
                    stage="audit",
                    phase="semantic",
                )
                callback.on_chat_model_start({}, [[]], run_id="one")
                callback.on_llm_error(error, run_id="one")
                callback.close(error)
                row = json.loads(Path(root, "ledger.jsonl").read_text())
                self.assertEqual(row["extra"]["error"], type(error).__name__)
                self.assertIsNone(row["cost_usd"])
                self.assertTrue(row["metered"])
                callback.on_chat_model_start({}, [[]], run_id="unfinished")
                callback.close(error)
                self.assertEqual(
                    len(Path(root, "ledger.jsonl").read_text().splitlines()), 2
                )
