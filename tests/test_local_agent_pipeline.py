"""Local-only issue pipeline acceptance contracts (no model or td required)."""

import contextlib
import io
import json
import os
import re
import shutil
import subprocess
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
import skills_registry

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


class TestSpecTemplate(unittest.TestCase):
    def headers(self):
        prompt = skills_registry.SkillsRegistry().inject_skills_for_node(
            "draft_spec_node", ""
        )
        return re.findall(r"`(#{1,3} [^`]+)`", prompt)

    def test_injected_template_has_validator_required_headers(self):
        headers = self.headers()
        for required in issue_generator.REQUIRED_SECTIONS:
            with self.subTest(required=required):
                self.assertTrue(any(required.lower() in h.lower() for h in headers))

    @unittest.skipUnless(shutil.which("td"), "td CLI not installed")
    def test_template_sections_pass_real_td_validator(self):
        sections = []
        for header in self.headers():
            content = "Specified behavior."
            if "Allowed Files" in header:
                content = "- `widget.py`"
            elif "Acceptance Criteria" in header:
                content = "- [ ] Specified behavior is verified."
            elif "Verification Commands" in header:
                content = "```bash\npython -m unittest\n```"
            sections.append(header + "\n" + content)
        with tempfile.TemporaryDirectory() as root:
            Path(root, "widget.py").write_text("pass\n", encoding="utf-8")
            spec = Path(root, "spec.md")
            spec.write_text("\n\n".join(sections), encoding="utf-8")
            result = subprocess.run(
                [
                    "td",
                    "validate-issue",
                    "--file",
                    str(spec),
                    "--repo-root",
                    root,
                    "--json",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])


class TestInvestigationEvidence(unittest.TestCase):
    def evidence(self, messages, limit=None):
        agent = mock.Mock()
        agent.invoke.return_value = {"messages": messages}
        agents = types.ModuleType("langchain.agents")
        agents.create_agent = mock.Mock(return_value=agent)
        langchain = types.ModuleType("langchain")
        langchain.agents = agents
        middleware = types.ModuleType("langchain.agents.middleware")
        middleware.wrap_tool_call = lambda fn: fn
        investigator = types.ModuleType("ask_investigator")
        investigator._checkout = "/original"
        graph = issue_generator.IssueGeneratorGraph("/tmp", model_fn=lambda: object())
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "langchain": langchain,
                    "langchain.agents": agents,
                    "langchain.agents.middleware": middleware,
                    "ask_investigator": investigator,
                },
            ),
            mock.patch.object(graph, "_investigator_tools", return_value=[]),
            mock.patch.dict(os.environ),
        ):
            os.environ.pop("S5_SPEC_RECURSION_LIMIT", None)
            if limit is not None:
                os.environ["S5_SPEC_RECURSION_LIMIT"] = limit
            result = graph.investigate_text("task", "backend/repair_worker.py draft")
        self.assertEqual(investigator._checkout, "/original")
        agent.invoke.assert_called_once()
        self.assertEqual(
            agent.invoke.call_args.kwargs["config"]["recursion_limit"],
            int(limit) if limit is not None else 25,
        )
        self.guard = agents.create_agent.call_args.kwargs["middleware"][0]
        return result

    def test_recursion_limit_env_override(self):
        self.evidence([], limit="7")

    def test_invalid_recursion_limit(self):
        for limit in ("0", "-1", "bad"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.evidence([], limit=limit)

    def test_duplicate_call_stops_before_second_execution(self):
        self.evidence([])
        handler = mock.Mock(return_value="result")

        def request(name, args):
            return types.SimpleNamespace(tool_call={"name": name, "args": args})

        self.guard(
            request("read_repo_file", {"path": "widget.py", "offset": 0}), handler
        )
        with self.assertRaisesRegex(RuntimeError, "Repeated investigation tool call"):
            self.guard(
                request("read_repo_file", {"offset": 0, "path": "widget.py"}), handler
            )
        handler.assert_called_once()
        self.guard(
            request("read_repo_file", {"path": "widget.py", "offset": 100}), handler
        )
        self.guard(request("grep_repo", {"path": "widget.py", "offset": 0}), handler)
        self.assertEqual(handler.call_count, 3)

    def test_guard_is_scoped_to_one_investigation(self):
        call = types.SimpleNamespace(
            tool_call={"name": "grep_repo", "args": {"pattern": "helper"}}
        )
        handler = mock.Mock()
        self.evidence([])
        self.guard(call, handler)
        self.evidence([])
        self.guard(call, handler)
        self.assertEqual(handler.call_count, 2)

    def test_real_agent_propagates_duplicate_call_and_restores_checkout(self):
        try:
            import langchain.agents  # noqa: F401
            from langchain_core.language_models.fake_chat_models import (
                FakeMessagesListChatModel,
            )
            from langchain_core.messages import AIMessage
            from langchain_core.tools import tool
        except ImportError:
            self.skipTest("agent dependencies not installed")

        class ScriptedModel(FakeMessagesListChatModel):
            def bind_tools(self, tools, **kwargs):
                return self

        calls = []

        @tool
        def lookup(path: str) -> str:
            """Read a repository file."""
            calls.append(path)
            return "file contents"

        model = ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "lookup", "args": {"path": "widget.py"}, "id": str(i)}
                    ],
                )
                for i in range(2)
            ]
        )
        graph = issue_generator.IssueGeneratorGraph("/tmp", model_fn=lambda: model)
        investigator = types.SimpleNamespace(_checkout="/original")
        with (
            mock.patch.dict(sys.modules, {"ask_investigator": investigator}),
            mock.patch.object(graph, "_investigator_tools", return_value=[lookup]),
            self.assertRaisesRegex(RuntimeError, "Repeated investigation tool call"),
        ):
            graph.investigate_text("task", "draft")
        self.assertEqual(calls, ["widget.py"])
        self.assertEqual(investigator._checkout, "/original")

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

    def test_prose_severity_words_only_block_as_labels(self):
        """A "High-level" summary line is not a high-severity finding."""
        self.assertFalse(auditor.blocking_findings("- High-level: nothing risky"))
        self.assertTrue(auditor.blocking_findings("- high: deletes deploy keys"))
        self.assertTrue(auditor.blocking_findings("- High-severity: drops records"))

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
