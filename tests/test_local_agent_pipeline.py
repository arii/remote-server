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
            for prefer in (None, "nebius", "openrouter", "twt"):
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


class TestScopeURLs(unittest.TestCase):
    def test_allowed_urls_removed_but_reference_links_preserved(self):
        draft = (
            "## Scope\n### Allowed Files\n- `widget.py`\n"
            "- https://github.com/org/repo/commit/123\n"
            "- `//github.com/org/repo`\n"
            "- [commit](http://example.org/commit/123)\n"
            "## References\nhttps://github.com/org/repo/commit/123\n"
        )
        cleaned = issue_generator.clean_allowed_file_urls(draft)
        scope, references = cleaned.split("## References")
        self.assertEqual(scope, "## Scope\n### Allowed Files\n- `widget.py`\n")
        self.assertIn("https://github.com", references)
        model = mock.Mock()
        model.invoke.return_value = types.SimpleNamespace(content=draft)
        graph = issue_generator.IssueGeneratorGraph("/tmp", model_fn=lambda: model)
        self.assertEqual(graph.draft_text("task", "", [], {}), cleaned)
        self.assertEqual(issue_generator.completed_spec({"draft": draft}), cleaned)

    def test_empty_and_plain_scope_unchanged(self):
        for text in (
            "",
            "### Allowed Files\n- `dir//widget.py`\n",
            "## References\n//github.com/org/repo\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(issue_generator.clean_allowed_file_urls(text), text)

    def test_validation_omits_citations_without_rewriting_commands(self):
        captured = []

        def td(args):
            captured.append(Path(args[args.index("--file") + 1]).read_text())
            return 0, json.dumps({"valid": True, "errors": []})

        graph = issue_generator.IssueGeneratorGraph("/tmp", td_runner=td)
        text = "## References\nhttps://github.com/org/repo\n## Verification Commands\n```bash\ncurl https://example.org/status\n```\n"
        self.assertTrue(graph.validate_spec(text)[0])
        self.assertNotIn("github.com", captured[0])
        self.assertIn("curl https://example.org/status", captured[0])
        self.assertIn("https://github.com", text)

    @unittest.skipUnless(shutil.which("td"), "td CLI not installed")
    def test_real_validator_ignores_citations_but_rejects_missing_files(self):
        with tempfile.TemporaryDirectory() as root:
            Path(root, "widget.py").write_text("pass\n", encoding="utf-8")
            graph = issue_generator.IssueGeneratorGraph(root)
            for path, expected in (("widget.py", True), ("missing.py", False)):
                text = (
                    "## Problem Statement\nFix behavior.\n## Goal\nCorrect behavior.\n"
                    f"## Scope\n### Allowed Files\n- `{path}`\n"
                    "- https://github.com/org/repo/commit/123\n- `//example.org/commit/456`\n"
                    "## Definition of Done\nBehavior corrected.\n"
                    "## Acceptance Criteria\n- [ ] Behavior verified.\n"
                    "## Verification Commands\n```bash\npython -m unittest\n```\n"
                    "## References\nhttps://github.com/org/repo/commit/123\n//example.org/commit/456\n"
                )
                valid, data, errors = graph.validate_spec(text)
                with self.subTest(path=path):
                    self.assertEqual(valid, expected, errors)
                    self.assertEqual(data["allowed_files"], [path])
                    if not expected:
                        self.assertIn(
                            "allowed path not in checkout: missing.py", errors[0]
                        )


class TestInvestigationEvidence(unittest.TestCase):
    def evidence(self, messages, limit=None, error=None):
        agent = mock.Mock()
        agent.invoke.return_value = {"messages": messages}
        agents = types.ModuleType("langchain.agents")
        agents.create_agent = mock.Mock(return_value=agent)
        langchain = types.ModuleType("langchain")
        langchain.agents = agents
        middleware = types.ModuleType("langchain.agents.middleware")
        middleware.wrap_tool_call = lambda fn: fn
        middleware.wrap_model_call = lambda fn: fn
        errors_module = types.ModuleType("langgraph.errors")
        errors_module.GraphRecursionError = type(
            "GraphRecursionError", (RuntimeError,), {}
        )
        messages_module = types.ModuleType("langchain_core.messages")
        messages_module.ToolMessage = lambda **kwargs: types.SimpleNamespace(**kwargs)
        investigator = types.ModuleType("ask_investigator")
        investigator._checkout = "/original"
        graph = issue_generator.IssueGeneratorGraph("/tmp", model_fn=lambda: object())
        self.graph = graph
        self.events = []
        graph.event_fn = self.events.append
        if error is not None:

            def invoke(*args, **kwargs):
                # middleware: [tool guard, forced-final-turn, capture]
                capture = agents.create_agent.call_args.kwargs["middleware"][2]
                capture(None, lambda request: types.SimpleNamespace(result=messages))
                raise error

            agent.invoke.side_effect = invoke
        with (
            mock.patch.dict(
                sys.modules,
                {
                    "langchain": langchain,
                    "langchain.agents": agents,
                    "langchain.agents.middleware": middleware,
                    "langchain_core.messages": messages_module,
                    "langgraph.errors": errors_module,
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
            # issue #1404: default derives from the spec turn budget
            int(limit) if limit is not None else 2 * graph.max_spec_turns + 1,
        )
        self.guard = agents.create_agent.call_args.kwargs["middleware"][0]
        return result

    def test_truncation_prefers_final_ai_text(self):
        messages = [
            types.SimpleNamespace(
                type="ai", content="widget.py:1 final evidence", tool_calls=[]
            )
        ]
        self.assertEqual(
            self.evidence(
                messages, error=issue_generator.InvestigationTruncated("limit")
            ),
            "widget.py:1 final evidence",
        )
        self.assertTrue(self.graph.investigation_truncated)
        self.assertEqual(len(self.events), 1)

    def test_truncation_without_results_returns_empty_evidence(self):
        self.assertEqual(
            self.evidence([], error=issue_generator.InvestigationTruncated("limit")), ""
        )
        self.assertTrue(self.graph.investigation_truncated)

    def test_unrelated_errors_are_not_swallowed(self):
        with self.assertRaisesRegex(ValueError, "bad tool"):
            self.evidence([], error=ValueError("bad tool"))

    def test_recursion_limit_env_override(self):
        self.evidence([], limit="7")

    def test_invalid_recursion_limit(self):
        for limit in ("0", "-1", "bad"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.evidence([], limit=limit)

    def test_duplicate_calls_warn_twice_then_stop_without_reexecution(self):
        self.evidence([])
        handler = mock.Mock(return_value="result")

        def request(name, args):
            return types.SimpleNamespace(
                tool_call={"name": name, "args": args, "id": "call-id"}
            )

        self.guard(
            request("read_repo_file", {"path": "widget.py", "offset": 0}), handler
        )
        for _ in range(2):
            warning = self.guard(
                request("read_repo_file", {"offset": 0, "path": "widget.py"}), handler
            )
            self.assertIn("Already called with these args", warning.content)
            self.assertEqual(warning.tool_call_id, "call-id")
        with self.assertRaisesRegex(RuntimeError, "3 repeats"):
            self.guard(
                request("read_repo_file", {"path": "widget.py", "offset": 0}), handler
            )
        handler.assert_called_once()
        self.guard(
            request("read_repo_file", {"path": "widget.py", "offset": 100}), handler
        )
        self.guard(request("grep_repo", {"path": "widget.py", "offset": 0}), handler)
        self.assertEqual(handler.call_count, 3)

    def test_repeat_budget_counts_across_different_tools(self):
        self.evidence([])
        handler = mock.Mock()
        first = types.SimpleNamespace(
            tool_call={
                "name": "read_repo_file",
                "args": {"path": "widget.py"},
                "id": "first",
            }
        )
        second = types.SimpleNamespace(
            tool_call={
                "name": "grep_repo",
                "args": {"pattern": "helper"},
                "id": "second",
            }
        )
        self.guard(first, handler)
        self.guard(second, handler)
        self.guard(first, handler)
        self.guard(second, handler)
        with self.assertRaisesRegex(RuntimeError, "3 repeats"):
            self.guard(first, handler)
        self.assertEqual(handler.call_count, 2)

    def test_guard_is_scoped_to_one_investigation(self):
        call = types.SimpleNamespace(
            tool_call={
                "name": "grep_repo",
                "args": {"pattern": "helper"},
                "id": "call-id",
            }
        )
        handler = mock.Mock()
        self.evidence([])
        self.guard(call, handler)
        self.guard(call, handler)
        self.guard(call, handler)
        self.evidence([])
        self.guard(call, handler)
        self.guard(call, handler)
        self.assertEqual(handler.call_count, 2)

    def test_real_agent_keeps_distinct_results_after_truncation(self):
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

        for call_count, limit in ((2, "25"), (4, "25"), (4, "2")):
            model = ScriptedModel(
                responses=[
                    AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "lookup",
                                "args": {"path": "widget.py"},
                                "id": str(i),
                            }
                        ],
                    )
                    for i in range(call_count)
                ]
                + [AIMessage(content="widget.py:1 evidence")]
            )
            graph = issue_generator.IssueGeneratorGraph(
                "/tmp", model_fn=lambda model=model: model
            )
            events = []
            graph.event_fn = events.append
            investigator = types.SimpleNamespace(_checkout="/original")
            calls.clear()
            with (
                self.subTest(call_count=call_count),
                mock.patch.dict(sys.modules, {"ask_investigator": investigator}),
                mock.patch.object(graph, "_investigator_tools", return_value=[lookup]),
                mock.patch.dict(os.environ, {"S5_SPEC_RECURSION_LIMIT": limit}),
            ):
                evidence = graph.investigate_text("task", "draft")
                if call_count == 4:
                    self.assertIn(
                        'lookup {"path": "widget.py"}: file contents', evidence
                    )
                    self.assertEqual(evidence.count("file contents"), 1)
                    self.assertTrue(graph.investigation_truncated)
                    self.assertEqual(len(events), 1)
                    self.assertIn(
                        "GraphRecursionError" if limit == "2" else "3 repeats",
                        events[0],
                    )
                else:
                    self.assertEqual(evidence, "widget.py:1 evidence")
                    self.assertFalse(graph.investigation_truncated)
                    self.assertFalse(events)
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
    def test_truncated_investigation_redrafts_and_logs_pipeline_event(self):
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest("agent dependencies not installed")
        original = issue_generator.IssueGeneratorGraph
        seen = []

        def factory(**kwargs):
            gen = original(**kwargs)
            gen.draft_text = mock.Mock(side_effect=["initial draft", "redrafted spec"])

            def investigate(brief, draft):
                gen.investigation_truncated = True
                gen.event_fn("repeat budget")
                return "widget.py:1 observed evidence"

            gen.investigate_text = investigate
            seen.append(gen)
            return gen

        with tempfile.TemporaryDirectory() as root:
            Path(root, "widget.py").write_text("pass\n", encoding="utf-8")
            pipe = pipeline.Pipeline(
                root,
                "task",
                spec_model_fn=lambda: object(),
                td_run=lambda *args: (
                    0,
                    json.dumps({"status": "ready", "valid": True, "errors": []}),
                ),
            )
            with (
                mock.patch.object(
                    issue_generator, "IssueGeneratorGraph", side_effect=factory
                ),
                mock.patch("ledger.record"),
            ):
                ok, state, text = pipe.stage_spec()
        self.assertTrue(ok, state)
        self.assertIn("redrafted spec", text)
        self.assertIn(
            "widget.py:1 observed evidence", seen[0].draft_text.call_args.args[1]
        )
        self.assertEqual(seen[0].draft_text.call_count, 2)
        self.assertTrue(
            any(
                e["stage"] == "investigation"
                and "investigation was truncated" in e["message"]
                for e in pipe.events
            )
        )

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

    def test_repair_passes_scoped_paths_but_not_generated_spec(self):
        with tempfile.TemporaryDirectory() as root:
            graph = pipeline.Pipeline(root, "fix", issue_id=1359).make_coder(
                "### Allowed Files\n- `a.py`\n- `docs/specs/issue-1359.md`\n"
            )
            with (
                mock.patch.object(
                    coder,
                    "run_coder",
                    return_value=("done", {}, [], 0, "ollama:test", "trace", True, 0),
                ) as executor,
                mock.patch("ledger.record"),
            ):
                graph.implement_fn(
                    "task", "1. Fix [files: b.py]", root, ["scope failed"]
                )
            self.assertEqual(
                executor.call_args.kwargs["repair_files"], ["b.py", "a.py"]
            )

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
                    "findings": json.dumps(
                        {
                            "verified": True,
                            "findings": [
                                {"severity": severity, "description": "unsafe change"}
                            ],
                        }
                    ),
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
        for findings in ([], [{"severity": "low", "description": "naming"}]):
            state = {
                "valid": True,
                "errors": [],
                "findings": json.dumps({"verified": True, "findings": findings}),
            }
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
