"""#1377: the per-repo verify profile reaches every coder path."""

import json
import os
import tempfile
import types
import unittest
from unittest import mock


import coder  # noqa: E402
import pipeline  # noqa: E402
from backend.shared import s5tools  # noqa: E402

PORTFOLIO = "arii/portfolio"


class TestProfilePropagation(unittest.TestCase):
    def test_build_system_swaps_guidance_only_for_profiled_repo(self):
        default = coder.build_system("/w", "ctx")
        self.assertEqual(coder.build_system("/w", "ctx", None), default)
        self.assertEqual(coder.build_system("/w", "ctx", "arii/other"), default)
        self.assertEqual(coder.build_system("/w", "ctx", ""), default)
        profiled = coder.build_system("/w", "ctx", PORTFOLIO)
        self.assertIn("audit:fallow", profiled)
        self.assertNotIn("audit:fallow", default)

    def test_check_tool_repo_scoped(self):
        try:
            import langchain_core  # noqa: F401
        except ImportError:
            self.skipTest("agent dependencies not installed")
        with tempfile.TemporaryDirectory() as root:
            ok = types.SimpleNamespace(returncode=0, stdout="OK", stderr="")
            with mock.patch("subprocess.run", return_value=ok) as run:
                got = coder.build_check_tool(root, None, PORTFOLIO).invoke(
                    {"command": "pnpm run build"}
                )
                self.assertEqual(got, "exit=0\nOK")
                run.assert_called_once()
            for repo in (None, "arii/remote-server", "arii/unknown"):
                with mock.patch("subprocess.run", return_value=ok) as run:
                    got = coder.build_check_tool(root, None, repo).invoke(
                        {"command": "pnpm run build"}
                    )
                    self.assertTrue(got.startswith("REJECTED"), got)
                    run.assert_not_called()

    def test_trace_evidence_counts_profiled_command_only_with_repo(self):
        event = {
            "type": "tool_call",
            "name": "run_check",
            "args_preview": json.dumps({"command": "pnpm run build"}),
            "result_preview": "exit=0",
        }
        with tempfile.TemporaryDirectory() as root:
            trace = os.path.join(root, "t.jsonl")
            with open(trace, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(event) + "\n")
            self.assertEqual(
                len(coder.trace_verification_evidence(trace, PORTFOLIO)), 1
            )
            self.assertEqual(coder.trace_verification_evidence(trace), [])
            self.assertEqual(coder.trace_verification_evidence(trace, "arii/x"), [])

    def test_plan_prompt_uses_repo_context(self):
        model = mock.Mock()
        model.invoke.return_value = types.SimpleNamespace(content="1. a [files: x]")
        with mock.patch.object(coder, "build_model", return_value=(model, {})):
            coder._default_plan_fn("b", "/w", {"repo": PORTFOLIO})
            self.assertIn("audit:fallow", model.invoke.call_args.args[0])
            coder._default_plan_fn("b", "/w", {})
            self.assertNotIn("audit:fallow", model.invoke.call_args.args[0])

    def test_pipeline_passes_repo_and_default_runner(self):
        with tempfile.TemporaryDirectory() as base:
            repo = os.path.join(base, "repos", "portfolio")
            other = os.path.join(base, "elsewhere")
            os.makedirs(repo)
            os.makedirs(other)
            self.assertEqual(pipeline.Pipeline(repo, "x")._repo_slug(), PORTFOLIO)
            self.assertEqual(pipeline.Pipeline(other, "x")._repo_slug(), "")
            with (
                mock.patch.object(
                    coder,
                    "run_coder",
                    return_value=("d", {}, [], 0, "o:m", "trace", True, 0),
                ) as run,
                mock.patch("backend.shared.ledger.record"),
            ):
                pipeline.Pipeline(repo, "x").make_coder("spec").implement_fn(
                    "t", "p", repo, []
                )
                self.assertEqual(run.call_args.kwargs["repo"], PORTFOLIO)
                pipeline.Pipeline(other, "x").make_coder("spec").implement_fn(
                    "t", "p", other, []
                )
                self.assertEqual(run.call_args.kwargs["repo"], "")
            graph = pipeline.Pipeline(repo, "x").make_coder("spec")
            with mock.patch.object(
                s5tools, "run_repo_command", return_value="exit=0"
            ) as rr:
                graph.cmd_runner("/w", "pnpm run build")
                rr.assert_called_once_with("/w", "pnpm run build", PORTFOLIO)


if __name__ == "__main__":
    unittest.main()
