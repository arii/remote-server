import importlib.util
import json
import os
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import auditor
import coder
import ledger
import pipeline
import s5tools


class TestVerificationIsolation(unittest.TestCase):
    def test_auditor_checks_also_use_isolated_state(self):
        graph = auditor.AuditorGraph(
            "/tmp", verification_commands=[["python3", "-m", "pytest"]]
        )
        captured = []

        def execute(*args, **kwargs):
            captured.append(kwargs["env"]["EVAL_LEDGER"])
            self.assertNotEqual(captured[-1], "/live/ledger.jsonl")
            self.assertTrue(Path(captured[-1]).parent.is_dir())
            return types.SimpleNamespace(returncode=0, stdout="OK", stderr="")

        with (
            mock.patch.object(
                auditor, "run_td_gate", return_value=(True, {"passed": True})
            ),
            mock.patch("subprocess.run", side_effect=execute),
            mock.patch.dict(os.environ, {"EVAL_LEDGER": "/live/ledger.jsonl"}),
        ):
            self.assertTrue(graph.hard_gate()[0])
        self.assertFalse(Path(captured[0]).parent.exists())

    def test_check_child_cannot_append_to_parent_ledger(self):
        runners = [s5tools.run_repo_command]
        if importlib.util.find_spec("langchain_core"):
            runners.append(
                lambda root, command: coder.build_check_tool(root).invoke(
                    {"command": command}
                )
            )
        with tempfile.TemporaryDirectory() as root:
            Path(root, "tests").mkdir()
            Path(root, "tests/test_emit.py").write_text(
                "import ledger, os, time\n"
                "ledger.record('child', 'unrated', 'child', [], time.time())\n"
                "print(os.environ['EVAL_LEDGER'])\n"
            )
            live = Path(root, "live.jsonl")
            live.write_text("parent sentinel\n")
            env = {
                "EVAL_LEDGER": str(live),
                "DEEP_AGENT_STATE_DIR": root,
                "PYTHONPATH": str(Path(ledger.__file__).parent),
            }
            with mock.patch.dict(os.environ, env):
                for runner in runners:
                    output = runner(root, "python3 tests/test_emit.py")
                    self.assertTrue(output.startswith("exit=0\n"), output)
                    child_path = output.splitlines()[-1]
                    self.assertNotEqual(child_path, str(live))
                    self.assertFalse(Path(child_path).exists())
                    self.assertEqual(live.read_text(), "parent sentinel\n")
                self.assertEqual(os.environ["EVAL_LEDGER"], str(live))

    def test_cleanup_on_timeout(self):
        paths = []

        def fail(*args, **kwargs):
            paths.append(kwargs["env"]["DEEP_AGENT_STATE_DIR"])
            self.assertTrue(Path(paths[-1]).is_dir())
            raise subprocess.TimeoutExpired(args[0], 1)

        with mock.patch("subprocess.run", side_effect=fail):
            self.assertTrue(
                s5tools.run_repo_command("/tmp", "git status").startswith("TIMEOUT")
            )
        self.assertFalse(Path(paths[0]).exists())


class TestCoderAccounting(unittest.TestCase):
    def test_pipeline_coder_tracks_actual_implementation_model(self):
        pipe = pipeline.Pipeline("/tmp", "fix", issue_id=1357)
        graph = pipe.make_coder("fix")
        with (
            mock.patch.object(
                coder,
                "run_coder",
                return_value=(
                    "done",
                    {},
                    [],
                    0,
                    "nebius:zai-org/GLM-5.3-Flash",
                    "trace",
                    True,
                    {},
                ),
            ),
            mock.patch.object(ledger, "record"),
        ):
            graph.implement_fn("fix", "plan", "/tmp", [])
        self.assertEqual(
            graph.last_model_spec,
            {"provider": "nebius", "model": "zai-org/GLM-5.3-Flash"},
        )

    @unittest.skipUnless(
        importlib.util.find_spec("langgraph"), "langgraph not installed"
    )
    def test_coder_summary_uses_planner_model_and_is_not_metered(self):
        spec = {"provider": "nebius", "model": "zai-org/GLM-5.3-Flash"}
        with tempfile.TemporaryDirectory() as root:
            graph = coder.InitCoderGraph(
                root,
                1,
                "fix",
                plan_fn=lambda *args: {"plan": "fix", "model_spec": spec},
                implement_fn=lambda *args: "done",
                thread_id="proof-coder-summary",
            )
            path = Path(root, "ledger.jsonl")
            with (
                mock.patch.object(
                    graph, "provision_worktree", return_value=(root, None)
                ),
                mock.patch.object(
                    graph, "run_gate", return_value={"passed": True, "errors": []}
                ),
                mock.patch.object(ledger, "LEDGER", str(path)),
            ):
                graph.run()
                self.assertTrue(graph.run(resume={"decision": "approve"})["valid"])
            row = json.loads(path.read_text())
            self.assertEqual(row["model"], spec["model"])
            self.assertEqual(row["provider"], spec["provider"])
            self.assertEqual(row["extra"]["phase"], "summary")
            self.assertFalse(row["metered"])

    def test_planner_records_usage_and_resolved_model_on_success_and_error(self):
        spec = {"provider": "nebius", "model": "zai-org/GLM-5.3-Flash"}
        usage = {"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100}
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                model = mock.Mock()
                model.invoke.return_value = types.SimpleNamespace(
                    content="1. Fix [files: widget.py]", usage_metadata=usage
                )
                if failure:
                    model.invoke.side_effect = RuntimeError("model failed")
                path = Path(root, "ledger.jsonl")
                with (
                    mock.patch.object(coder, "build_model", return_value=(model, spec)),
                    mock.patch.object(ledger, "LEDGER", str(path)),
                    mock.patch.dict(os.environ, {"DEEP_AGENT_STATE_DIR": root}),
                ):
                    if failure:
                        with self.assertRaises(RuntimeError):
                            coder._default_plan_fn("fix", root, {})
                    else:
                        result = coder._default_plan_fn("fix", root, {})
                        self.assertEqual(result["model_spec"], spec)
                row = json.loads(path.read_text())
                self.assertEqual(row["model"], spec["model"])
                self.assertEqual(row["provider"], "nebius")
                self.assertEqual(row["extra"]["phase"], "plan")
                if failure:
                    self.assertEqual(row["extra"]["error"], "RuntimeError")
                else:
                    self.assertNotIn("error", row["extra"])
                self.assertEqual(row["cost_usd"], None if failure else 0.0002)
                self.assertTrue(Path(row["extra"]["trace"]).exists())

    def test_non_llm_summary_is_not_unpriced_metered_traffic(self):
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(ledger, "LEDGER", str(Path(root, "ledger.jsonl"))),
        ):
            row = ledger.record(
                "coder.py",
                "none",
                "proof-summary",
                [],
                0,
                extra={"llm_call": False, "phase": "summary"},
            )
            self.assertEqual(row["cost_usd"], 0)
            self.assertFalse(row["metered"])

    def test_planner_without_text_or_usage_is_unknown_not_free(self):
        spec = {"provider": "nebius", "model": "zai-org/GLM-5.3-Flash"}
        model = mock.Mock()
        model.invoke.return_value = types.SimpleNamespace(
            content="", usage_metadata=None
        )
        with (
            tempfile.TemporaryDirectory() as root,
            mock.patch.object(coder, "build_model", return_value=(model, spec)),
            mock.patch.object(ledger, "LEDGER", str(Path(root, "ledger.jsonl"))),
            mock.patch.dict(os.environ, {"DEEP_AGENT_STATE_DIR": root}),
        ):
            result = coder._default_plan_fn("fix", root, {})
            row = json.loads(Path(root, "ledger.jsonl").read_text())
        self.assertEqual(result["plan"], "")
        self.assertEqual(result["allowed_files"], [])
        self.assertTrue(row["extra"]["usage_missing"])
        self.assertIsNone(row["cost_usd"])
        self.assertTrue(row["metered"])
