import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import auditor
import coder
import issue_generator
import ledger
import model_router
import observe
import pipeline


class TestVerificationContract(unittest.TestCase):
    def test_long_recorded_checks_keep_the_summary_tail(self):
        for code in (0, 1):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                trace = Path(root, "trace.jsonl")
                recorder = observe.TraceRecorder(trace)
                output = (
                    f"exit={code}\nstart\n"
                    + "log line\n" * 1000
                    + f"summary exit {code}"
                )
                recorder.tool_call(
                    "run_check",
                    {"command": "python3 -m pytest"},
                    0.1,
                    True,
                    result=output,
                )
                recorder.close()
                evidence = coder.trace_verification_evidence(trace)
                self.assertEqual(evidence[0]["passed"], code == 0)
                self.assertTrue(evidence[0]["output"].endswith(f"summary exit {code}"))
                self.assertIn("start", evidence[0]["output"])
                self.assertLessEqual(
                    len(evidence[0]["output"]), coder.CHECK_OUTPUT_LIMIT
                )

    def test_edits_invalidate_prior_checks_until_a_fresh_check(self):
        check = {
            "type": "tool_call",
            "name": "run_check",
            "ok": True,
            "args_preview": json.dumps({"command": "python3 -m pytest"}),
            "result_preview": "exit=0\n1 passed",
        }
        with tempfile.TemporaryDirectory() as root:
            trace = Path(root, "trace.jsonl")
            for name in ("write_file", "edit_file", "delete"):
                for ok in (True, False):
                    with self.subTest(name=name, ok=ok):
                        events = [check, {"type": "tool_call", "name": name, "ok": ok}]
                        trace.write_text(
                            "\n".join(json.dumps(event) for event in events)
                        )
                        self.assertTrue(
                            coder.trace_verification_evidence(trace)[0].get("stale")
                        )
                        self.assertFalse(coder.trace_has_passing_check(trace))
                        events.append(check)
                        trace.write_text(
                            "\n".join(json.dumps(event) for event in events)
                        )
                        evidence = coder.trace_verification_evidence(trace)
                        self.assertTrue(evidence[0]["stale"])
                        self.assertFalse(evidence[-1].get("stale", False))
                        self.assertTrue(coder.trace_has_passing_check(trace))

    def test_read_only_events_do_not_invalidate_a_check(self):
        with tempfile.TemporaryDirectory() as root:
            trace = Path(root, "trace.jsonl")
            events = [
                {
                    "type": "tool_call",
                    "name": "run_check",
                    "ok": True,
                    "args_preview": json.dumps({"command": "python3 -m pytest"}),
                    "result_preview": "exit=0\n1 passed",
                },
                {"type": "tool_call", "name": "read_file", "ok": True},
            ]
            trace.write_text("\n".join(json.dumps(event) for event in events))
            self.assertFalse(
                coder.trace_verification_evidence(trace)[0].get("stale", False)
            )
            self.assertTrue(coder.trace_has_passing_check(trace))

    def test_check_logs_cannot_overflow_an_otherwise_valid_audit(self):
        from langchain_core.messages import HumanMessage

        with tempfile.TemporaryDirectory() as root:
            Path(root, "spec.md").write_text("Fix helper")
            diff = "+" + "code" * 14000
            logs = [
                {
                    "command": "python3 -m pytest",
                    "passed": passed,
                    "output": "start" + "x" * 30000 + "summary",
                }
                for passed in (False, True)
            ]
            graph = auditor.AuditorGraph(
                root, spec_file="spec.md", verification_evidence=logs
            )
            gate = {
                "verification": {
                    "passed": True,
                    "commands": [{"exit": 0, "output": "y" * 20000 + "gate summary"}],
                }
            }
            with mock.patch.object(
                auditor.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=diff),
            ):
                payload = graph.semantic_payload(gate)
            model_router.bounded_history([HumanMessage(content=json.dumps(payload))])
            model_router.bounded_history(
                [HumanMessage(content=graph.semantic_prompt(payload))]
            )
            self.assertEqual(payload["diff"], diff)
            self.assertEqual(
                [entry["passed"] for entry in payload["verification"]], [False, True]
            )
            self.assertTrue(payload["verification"][0]["output"].endswith("summary"))
            self.assertLess(len(payload["verification"][0]["output"]), 30000)
            self.assertEqual(len(logs[0]["output"]), 30012)

    def test_evidence_budget_does_not_silently_cut_diff_or_spec(self):
        graph = auditor.AuditorGraph("/tmp")
        with self.assertRaisesRegex(ValueError, "diff/spec"):
            graph.bound_verification(
                {"diff": "x" * 70000, "spec": "spec", "gate": {}, "verification": []}
            )
        payload = {"diff": "diff", "spec": "spec", "gate": {}, "verification": []}
        self.assertEqual(graph.bound_verification(payload), payload)

    def test_configured_checks_include_observed_outputs_and_fail_closed(self):
        for code in (0, 1):
            with self.subTest(code=code):
                graph = auditor.AuditorGraph(
                    "/tmp",
                    td_runner=lambda args: (0, '{"status":"success","passed":true}'),
                    verification_commands=[["python3", "-m", "pytest"]],
                )
                with mock.patch.object(
                    auditor.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess(
                        [], code, stdout="observed stdout", stderr="observed stderr"
                    ),
                ):
                    valid, gates, errors = graph.hard_gate()
                self.assertEqual(valid, code == 0)
                self.assertEqual(gates["verification"]["passed"], code == 0)
                self.assertEqual(
                    gates["verification"]["commands"][0],
                    {
                        "command": ["python3", "-m", "pytest"],
                        "exit": code,
                        "output": "observed stdoutobserved stderr",
                    },
                )
                self.assertEqual(bool(errors), code != 0)

    def test_untracked_dependencies_omitted_but_tracked_changes_still_reviewed(self):
        with tempfile.TemporaryDirectory() as root:

            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=root, capture_output=True, text=True, check=True
                ).stdout

            git("init", "-q")
            Path(root, "spec.md").write_text("## Goal\nFix helper")
            Path(root, ".gitignore").write_text("node_modules/\n")
            for directory in ("cache-a", "cache-b", "frontend", "vendor"):
                Path(root, directory).mkdir()
            tracked = Path(root, "vendor/node_modules")
            tracked.symlink_to(Path(root, "cache-a"), target_is_directory=True)
            git("add", "-f", "spec.md", ".gitignore", "vendor/node_modules")
            git(
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "baseline",
            )
            git("update-ref", "refs/remotes/origin/main", "HEAD")
            for name in ("node_modules", "frontend/node_modules"):
                Path(root, name).symlink_to(
                    Path(root, "cache-a"), target_is_directory=True
                )
            graph = auditor.AuditorGraph(root, spec_file="spec.md")
            with self.assertRaisesRegex(ValueError, "no diff"):
                graph.semantic_payload({})
            tracked.unlink()
            tracked.symlink_to(Path(root, "cache-b"), target_is_directory=True)
            Path(root, "helper.py").write_text("def helper():\n    return 1\n")
            Path(root, "link.py").symlink_to("helper.py")
            diff = graph.semantic_payload({})["diff"]
            self.assertNotIn("diff --git a/node_modules ", diff)
            self.assertNotIn("diff --git a/frontend/node_modules ", diff)
            for name in ("helper.py", "link.py", "vendor/node_modules"):
                self.assertIn(f"diff --git a/{name} ", diff)

    def test_actual_check_evidence_reaches_semantic_review(self):
        with tempfile.TemporaryDirectory() as root:
            trace = Path(root, "trace.jsonl")
            events = [
                {
                    "type": "tool_call",
                    "name": "run_check",
                    "ok": True,
                    "args_preview": json.dumps(
                        {"command": "python3 -m pytest tests/test_helper.py -q"}
                    ),
                    "result_preview": output,
                }
                for output in ("exit=1\n1 failed", "exit=0\n1 passed")
            ]
            trace.write_text(
                "bad json\n"
                + "\n".join(json.dumps(e) for e in events)
                + '\n{"type":"tool_call","name":"run_check","args_preview":"broken"}\n'
            )
            evidence = coder.trace_verification_evidence(trace)
            self.assertEqual([e["passed"] for e in evidence], [False, True])
            self.assertIn("1 passed", evidence[-1]["output"])
            self.assertEqual(coder.trace_verification_evidence(None), [])
            self.assertEqual(
                coder.trace_verification_evidence(Path(root, "missing")), []
            )
            agent = pipeline.Pipeline(
                root,
                "fix",
                issue_id=1359,
                verification_commands=[["python3", "-m", "pytest"]],
            )
            with (
                mock.patch.object(
                    coder,
                    "run_coder",
                    return_value=(
                        "DONE #1359",
                        {},
                        [],
                        0,
                        "ollama:test",
                        str(trace),
                        True,
                        0,
                    ),
                ),
                mock.patch.object(ledger, "record"),
            ):
                agent.make_coder("fix").implement_fn("task", "plan", root, [])
            self.assertEqual(agent.verification_evidence, evidence)
            with (
                mock.patch.object(auditor, "AuditorGraph") as graph,
                mock.patch.object(
                    pipeline,
                    "drive_to_settled",
                    return_value={"valid": True, "verdict": {"status": "APPROVED"}},
                ),
            ):
                agent.stage_audit(root)
            self.assertEqual(graph.call_args.kwargs["verification_evidence"], evidence)
            self.assertEqual(
                graph.call_args.kwargs["verification_commands"],
                agent.verification_commands,
            )
            Path(root, "spec.md").write_text("## Goal\nFix")
            review = auditor.AuditorGraph(
                root, spec_file="spec.md", verification_evidence=evidence
            )
            with mock.patch.object(
                auditor.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout="+helper()"),
            ):
                self.assertEqual(review.semantic_payload({})["verification"], evidence)

    def test_new_file_is_in_diff_and_empty_diff_rejects(self):
        with tempfile.TemporaryDirectory() as root:

            def git(*args):
                return subprocess.run(
                    ["git", *args], cwd=root, capture_output=True, text=True, check=True
                ).stdout

            git("init", "-q")
            Path(root, "spec.md").write_text("## Goal\nAdd helper")
            git("add", "spec.md")
            git(
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "baseline",
            )
            git("update-ref", "refs/remotes/origin/main", "HEAD")
            graph = auditor.AuditorGraph(root, spec_file="spec.md")
            with self.assertRaisesRegex(ValueError, "no diff"):
                graph.semantic_payload({})
            Path(root, "helper.py").write_text("def helper():\n    return 1\n")
            payload = graph.semantic_payload({})
            self.assertIn("diff --git a/helper.py b/helper.py", payload["diff"])
            self.assertIn("+def helper():", payload["diff"])
            self.assertEqual(git("diff", "--cached", "--", "helper.py"), "")

    def test_pipeline_spec_scope_is_canonical_and_idempotent(self):
        draft = "## Goal\nFix\n### Allowed Files\n- `widget.py`\n## Acceptance Criteria\nHelper used\n"
        agent = pipeline.Pipeline("/tmp", "fix", issue_id=1357)
        with mock.patch.object(
            pipeline, "drive_to_settled", return_value={"valid": True, "draft": draft}
        ):
            ok, _state, spec = agent.stage_spec()
        self.assertTrue(ok)
        self.assertEqual(
            issue_generator.explicit_allowed_paths(spec), [agent.spec_path, "widget.py"]
        )
        self.assertEqual(spec.count(agent.spec_path), 1)
        self.assertEqual(
            issue_generator.completed_spec(
                {"draft": spec}, allowed_path=agent.spec_path
            ),
            spec,
        )
        self.assertIn("(new)", spec)

    def test_pipeline_passes_written_spec_to_auditor(self):
        agent = pipeline.Pipeline("/tmp", "fix", issue_id=1357)
        with (
            mock.patch.object(auditor, "AuditorGraph") as graph,
            mock.patch.object(
                pipeline,
                "drive_to_settled",
                return_value={"valid": True, "verdict": {"status": "APPROVED"}},
            ),
        ):
            agent.stage_audit("/tmp")
        self.assertEqual(
            graph.call_args.kwargs["spec_file"], "docs/specs/issue-1357.md"
        )

    def test_explicit_verified_required_even_when_human_approves(self):
        for fields in (
            {},
            {"verified": False},
            {"verified": "true"},
            {"verified": 1},
            {"verified": True},
        ):
            review = {
                **fields,
                "findings": [{"severity": "low", "description": "naming"}],
            }
            state = {
                "approved": True,
                "valid": True,
                "errors": [],
                "findings": json.dumps(review),
            }
            expected = fields.get("verified") is True
            with self.subTest(fields=fields):
                verdict = auditor.AuditorGraph("/tmp").build_verdict(state)
                self.assertEqual(verdict["verified"], expected)
                self.assertEqual(
                    verdict["status"], "APPROVED" if expected else "CHANGES_REQUESTED"
                )
                self.assertEqual(
                    pipeline.Pipeline("/tmp", "fix")._audit_policy([], state)[
                        "decision"
                    ],
                    "approve" if expected else "reject",
                )

    def test_malformed_or_prose_review_never_verifies(self):
        for value in (
            "verified: true; no problems",
            '```json\n{"verified":true,"findings":[]}\n```\nNotes: cannot verify',
            "{",
            [],
            {"verified": True},
            {"verified": True, "findings": ["low"]},
        ):
            with self.subTest(value=value):
                self.assertFalse(auditor.review_verified(value))
        self.assertTrue(
            auditor.review_verified('```json\n{"verified":true,"findings":[]}\n```')
        )

    def test_payload_contains_complete_diff_spec_and_gates(self):
        with tempfile.TemporaryDirectory() as root:
            spec = (
                "## Goal\nUse helper\n## Definition of Done\nTests pass\n## Acceptance Criteria\nHelper called\n"
                + "x" * 5000
            )
            Path(root, "spec.md").write_text(spec)
            diff = "-inline\n+helper()\n" + "y" * 5000
            graph = auditor.AuditorGraph(root, spec_file="spec.md")
            with mock.patch.object(
                auditor.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, stdout=diff),
            ) as run:
                payload = graph.semantic_payload({"audit-code": {"passed": True}})
            self.assertEqual(payload["diff"], diff)
            self.assertEqual(payload["spec"], spec)
            self.assertEqual(payload["gate"], {"audit-code": {"passed": True}})
            self.assertEqual(
                run.call_args.args[0],
                [
                    "git",
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "origin/main",
                    "--",
                ],
            )
            self.assertTrue(run.call_args.kwargs["check"])

    def test_missing_spec_and_failed_diff_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                auditor.AuditorGraph(root).semantic_payload({})
            with self.assertRaises(FileNotFoundError):
                auditor.AuditorGraph(root, spec_file="missing.md").semantic_payload({})
            Path(root, "spec.md").write_text("## Goal\nfix")
            with (
                mock.patch.object(
                    auditor.subprocess,
                    "run",
                    side_effect=subprocess.CalledProcessError(128, "git"),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                auditor.AuditorGraph(root, spec_file="spec.md").semantic_payload({})


@unittest.skipUnless(
    importlib.util.find_spec("langgraph"), "agent dependencies not installed"
)
class TestSemanticLimits(unittest.TestCase):
    def exercise(
        self, responses, limit="25", tool=None, error=None, full=False, approved=True
    ):
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
                mock.patch.object(
                    graph,
                    "semantic_payload",
                    return_value={
                        "diff": "-old\n+new",
                        "spec": "Goal: fix",
                        "gate": {},
                    },
                ),
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
                    graph.max_loops = 1
                    with mock.patch.object(
                        graph, "hard_gate", return_value=(True, {}, [])
                    ):
                        graph.run()
                        state = graph.run(resume={"decision": "approve"})
                    findings = state["findings"]
                    verdict = state.get("verdict") or graph.build_verdict(state)
                    self.assertEqual(
                        verdict["status"],
                        "APPROVED" if approved else "CHANGES_REQUESTED",
                    )
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

    def test_unverified_reply_cannot_be_approved_by_human(self):
        for review in ({"verified": False, "findings": []}, {"findings": []}):
            with self.subTest(review=review):
                self.exercise(
                    [self.message(json.dumps(review))], full=True, approved=False
                )

    def test_agent_receives_untruncated_diff_and_spec(self):
        import langchain.agents

        with tempfile.TemporaryDirectory() as root:
            spec = (
                "## Goal\nFix\n## Definition of Done\nPass\n## Acceptance Criteria\nHelper\n"
                + "x" * 5000
            )
            diff = "-inline\n+helper()\n" + "y" * 5000
            Path(root, "spec.md").write_text(spec)
            graph = auditor.AuditorGraph(root, spec_file="spec.md")
            agent = mock.Mock()
            agent.invoke.return_value = {
                "messages": [self.message('{"verified": true, "findings": []}')]
            }
            model_spec = {"provider": "nebius", "model": "zai-org/GLM-5.3-Flash"}
            with (
                mock.patch.object(langchain.agents, "create_agent", return_value=agent),
                mock.patch.object(model_router, "resolve_llm", return_value=model_spec),
                mock.patch.object(
                    model_router,
                    "build_chat_model",
                    return_value=(object(), model_spec),
                ),
                mock.patch.object(
                    auditor.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout=diff),
                ),
            ):
                graph.audit_once({"passed": True})
            prompt = agent.invoke.call_args.args[0]["messages"][0]["content"]
            self.assertIn(
                json.dumps(
                    {
                        "diff": diff,
                        "spec": spec,
                        "gate": {"passed": True},
                        "verification": [],
                    }
                ),
                prompt,
            )

    def test_normal_final_verdict_and_per_call_usage(self):
        graph, findings, rows = self.exercise(
            [self.message('{"verified": true, "findings": []}')]
        )
        self.assertFalse(graph.audit_truncated)
        self.assertEqual(json.loads(findings), {"verified": True, "findings": []})
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
            [self.message('{"verified": true, "findings": []}')], full=True
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
