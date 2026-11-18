"""Local-only issue pipeline acceptance contracts (no model or td required)."""

import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import auditor
import cli
import coder
import issue_generator
import model_router
import pipeline

from backend import review


class TestLocalRouting(unittest.TestCase):
    @mock.patch.dict(
        os.environ,
        {
            "S5_LOCAL_ONLY": "1",
            "ASK_MODEL": "qwen3:8b",
            "NEBIUS_ONLY": "1",
            "NEBIUS_API_KEY": "dummy",
            "OPENROUTER_API_KEY": "dummy",
        },
        clear=True,
    )
    def test_local_only_overrides_paid_provider_preferences(self):
        """Local-only wins even when cloud preferences and keys are present."""
        with mock.patch.object(model_router, "ollama_reachable", return_value=True):
            for prefer in (None, "nebius", "openrouter"):
                with self.subTest(prefer=prefer):
                    spec = model_router.resolve_llm(prefer=prefer)
                    self.assertEqual(spec["provider"], "ollama")
                    self.assertEqual(spec["model"], "qwen3:8b")

    @mock.patch.dict(os.environ, {"S5_LOCAL_ONLY": "1"}, clear=True)
    def test_unreachable_local_server_fails_closed(self):
        """No local server must be an error, not a hosted-provider retry."""
        with (
            mock.patch.object(model_router, "ollama_reachable", return_value=False),
            self.assertRaises(RuntimeError),
        ):
            model_router.resolve_llm()

    @mock.patch.dict(os.environ, {"S5_LOCAL_ONLY": "1"}, clear=True)
    def test_cloud_names_and_model_overrides_fail_closed(self):
        """Ollama cloud names are forbidden both at routing and construction."""
        with mock.patch.object(model_router, "ollama_reachable", return_value=True):
            for name in ("gpt-oss:120b-cloud", "qwen-cloud:latest"):
                with self.subTest(model=name):
                    with (
                        mock.patch.dict(os.environ, {"ASK_MODEL": name}),
                        self.assertRaises(RuntimeError),
                    ):
                        model_router.resolve_llm()
                    with self.assertRaises(RuntimeError):
                        model_router.build_chat_model(
                            spec={"provider": "ollama", "model": name}
                        )

    def test_review_does_not_retry_another_provider(self):
        """Review has one provider-selection path, not a fallback ladder."""
        factory = mock.Mock(side_effect=RuntimeError("unavailable"))
        with self.assertRaises(review.ReviewError):
            review.synthesize({}, "", model_factory=factory)
        factory.assert_called_once_with("ollama")


class TestSpecOutput(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.brief = self.root / "brief.md"
        self.brief.write_text("fix issue", encoding="utf-8")
        self.out = self.root / "nested" / "design.md"

    def run_cli(self, state):
        with (
            mock.patch.object(
                issue_generator.IssueGeneratorGraph, "run", return_value=state
            ),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return issue_generator.main(
                ["--spec-file", str(self.brief), "--out", str(self.out)]
            )

    def test_completed_spec_is_written_with_evidence(self):
        """--out creates missing parents and saves the completed design."""
        self.assertEqual(
            self.run_cli(
                {
                    "valid": True,
                    "draft": "# Design",
                    "evidence": "backend/file.py:1 evidence",
                }
            ),
            0,
        )
        text = self.out.read_text(encoding="utf-8")
        self.assertIn("# Design", text)
        self.assertIn("backend/file.py:1 evidence", text)

    def test_invalid_spec_does_not_create_output(self):
        """Invalid specs do not leave a deliverable behind."""
        with mock.patch("ledger.record"):
            self.assertNotEqual(self.run_cli({"valid": False, "errors": ["bad"]}), 0)
        self.assertFalse(self.out.exists())

    def test_output_write_error_is_nonzero(self):
        """An unwritable destination cannot report success."""
        with mock.patch.object(Path, "write_text", side_effect=OSError("denied")):
            self.assertNotEqual(self.run_cli({"valid": True, "draft": "design"}), 0)


class TestInvestigationEvidence(unittest.TestCase):
    def evidence(self, messages):
        agent = mock.Mock()
        agent.invoke.return_value = {"messages": messages}
        agents = types.ModuleType("langchain.agents")
        agents.create_agent = mock.Mock(return_value=agent)
        langchain = types.ModuleType("langchain")
        langchain.agents = agents
        investigator = types.ModuleType("ask_investigator")
        investigator._checkout = "/original"
        graph = issue_generator.IssueGeneratorGraph("/tmp", model_fn=lambda: object())
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "langchain": langchain,
                    "langchain.agents": agents,
                    "ask_investigator": investigator,
                },
            ),
            mock.patch.object(graph, "_investigator_tools", return_value=[]),
        ):
            result = graph.investigate_text("task", "backend/repair_worker.py draft")
        self.assertEqual(investigator._checkout, "/original")
        return result

    def test_only_final_assistant_evidence_enters_spec(self):
        messages = [
            types.SimpleNamespace(
                type="human", content="Draft: ## Allowed Files\n- bogus.py"
            ),
            types.SimpleNamespace(type="ai", content="I will read the file."),
            types.SimpleNamespace(
                type="tool", content="Raw source and unrelated docs/wrong.md"
            ),
            types.SimpleNamespace(
                type="ai", content="backend/repair_worker.py:166 defines the helper."
            ),
        ]
        self.assertEqual(
            self.evidence(messages), "backend/repair_worker.py:166 defines the helper."
        )

    def test_no_assistant_evidence_does_not_fall_back_to_draft(self):
        for messages in (
            [],
            [types.SimpleNamespace(type="tool", content="unreviewed.py")],
        ):
            with self.subTest(messages=messages):
                self.assertEqual(self.evidence(messages), "")


class TestPipelineContracts(unittest.TestCase):
    def test_pipeline_cli_accepts_upfront_answers_and_keep_worktree(self):
        with tempfile.TemporaryDirectory() as root:
            qna = Path(root) / "answers.json"
            qna.write_text(json.dumps({"Scope?": "Only the helper"}), encoding="utf-8")
            with (
                mock.patch.object(pipeline, "Pipeline") as factory,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                factory.return_value.run.return_value = {"status": "APPROVED"}
                result = cli.main(
                    [
                        "run-pipeline",
                        "--repo",
                        root,
                        "--intent",
                        "fix",
                        "--keep-worktree",
                        "--qna-file",
                        str(qna),
                    ]
                )
            self.assertEqual(result, 0)
            self.assertEqual(
                factory.call_args.kwargs["answers"], {"Scope?": "Only the helper"}
            )
            self.assertTrue(factory.call_args.kwargs["keep_worktree"])

    def test_pipeline_cli_rejects_invalid_answers(self):
        with tempfile.TemporaryDirectory() as root:
            qna = Path(root) / "answers.json"
            for value in ("[]", "not json"):
                qna.write_text(value, encoding="utf-8")
                with (
                    mock.patch.object(pipeline, "Pipeline") as factory,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    self.assertNotEqual(
                        cli.main(
                            [
                                "run-pipeline",
                                "--repo",
                                root,
                                "--intent",
                                "fix",
                                "--qna-file",
                                str(qna),
                            ]
                        ),
                        0,
                    )
                factory.assert_not_called()

    def test_worktree_contains_completed_spec(self):
        """The spec belongs in the implementation worktree so the PR ships it."""
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(
                coder.InitCoderGraph, "provision_worktree", return_value=(root, None)
            ):
                graph = pipeline.Pipeline(root, "fix", issue_id=1357).make_coder(
                    "# Completed design"
                )
                graph.provision_worktree()
            self.assertEqual(
                (Path(root) / "docs/specs/issue-1357.md").read_text(encoding="utf-8"),
                "# Completed design",
            )

    def test_default_implementer_uses_editing_executor(self):
        """Default coding uses run_coder rather than bare-model implementation notes."""
        with tempfile.TemporaryDirectory() as root:
            graph = pipeline.Pipeline(root, "fix").make_coder("spec")
            with mock.patch.object(
                coder,
                "run_coder",
                return_value=("done", {}, [], 0, "ollama:qwen3:8b", "trace", True, 0),
            ) as executor:
                self.assertEqual(graph.implement_fn("task", "plan", root, []), "done")
                executor.assert_called_once()
                self.assertEqual(executor.call_args.args[0], root)

    def test_unverified_implementation_is_not_success(self):
        """A run_coder result without verification fails the implementation."""
        with tempfile.TemporaryDirectory() as root:
            graph = pipeline.Pipeline(root, "fix").make_coder("spec")
            with (
                mock.patch.object(
                    coder,
                    "run_coder",
                    return_value=(
                        "done",
                        {},
                        [],
                        0,
                        "ollama:qwen3:8b",
                        "trace",
                        False,
                        0,
                    ),
                ),
                self.assertRaises(RuntimeError),
            ):
                graph.implement_fn("task", "plan", root, [])

    def test_blocking_findings_cannot_be_approved(self):
        """High and critical semantic findings override passing deterministic gates."""
        for severity in ("high", "critical"):
            with self.subTest(severity=severity):
                state = {
                    "valid": True,
                    "approved": True,
                    "errors": [],
                    "findings": json.dumps({"findings": [{"severity": severity}]}),
                }
                self.assertEqual(
                    pipeline.Pipeline("/tmp", "fix")._audit_policy([], state)[
                        "decision"
                    ],
                    "reject",
                )
                self.assertNotEqual(
                    auditor.AuditorGraph("/tmp").build_verdict(state)["status"],
                    "APPROVED",
                )

    def test_empty_and_low_findings_allow_approval(self):
        """Empty and nonblocking findings do not fail otherwise passing gates."""
        for findings in ([], [{"severity": "low"}]):
            state = {"valid": True, "errors": [], "findings": json.dumps(findings)}
            self.assertEqual(
                pipeline.Pipeline("/tmp", "fix")._audit_policy([], state)["decision"],
                "approve",
            )

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_coder_cli_model_is_used(self):
        """--model must reach the editing executor's model selection."""

        def finish(*args, **kwargs):
            self.assertEqual(os.environ["CODER_MODEL"], "qwen2.5-coder:7b")
            return 0

        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(coder, "needs_plan", return_value=False),
            mock.patch.object(
                coder, "_implement_and_finish", side_effect=finish
            ) as execute,
        ):
            self.assertEqual(
                coder.main(["--model", "qwen2.5-coder:7b", "--worktree", root, "fix"]),
                0,
            )
            execute.assert_called_once()

    @mock.patch.dict(os.environ, {}, clear=True)
    def test_pipeline_flag_sets_local_only(self):
        """--local-only enables the switch before any pipeline stages run."""

        def run():
            self.assertEqual(os.environ["S5_LOCAL_ONLY"], "1")
            return {"status": "APPROVED"}

        with (
            mock.patch.object(pipeline.Pipeline, "run", side_effect=run),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli.main(
                    [
                        "run-pipeline",
                        "--repo",
                        "/tmp",
                        "--intent",
                        "fix",
                        "--local-only",
                    ]
                ),
                0,
            )
