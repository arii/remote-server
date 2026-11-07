"""Tests for deep-agent/pipeline.py + eval_pipeline.py.

Fast routing tests are stdlib-only (stubbed stages, no langgraph).
Full-graph evals need the deep-agent venv (langgraph) and run real
git fixtures + real pytest; they skip otherwise. The real-td smoke
test skips when no td binary is on PATH.
"""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import pipeline as pipeline_mod

try:
    import langgraph  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


class ScriptedPipeline(pipeline_mod.Pipeline):
    """Pipeline with scripted stage outcomes (no graphs, no td)."""

    def __init__(self, script, **kwargs):
        kwargs.setdefault("repo", "/tmp")
        kwargs.setdefault("intent", "test intent")
        super().__init__(**kwargs)
        self.script = list(script)
        self.calls = []

    def stage_init(self):
        return True, {"status": "ready"}

    def stage_spec(self):
        return True, {}, "scripted spec"

    def stage_code(self, brief, worktree_root=None, repair_notes=""):
        self.calls.append((worktree_root, bool(repair_notes)))
        return True, {}, worktree_root or "/tmp/wt-1"

    def stage_audit(self, worktree_root):
        return self.script.pop(0), {}, "scripted verdict"

    def stage_submit(self, worktree_root, title, body=""):
        return True, {"url": "https://example.invalid/pr/1"}

    def stage_cleanup(self, worktree_root):
        return True, ""


class TestHelpers(unittest.TestCase):
    def test_parse_json_output_ok(self):
        payload, error = pipeline_mod.parse_json_output('{"a": 1}')
        self.assertEqual(payload, {"a": 1})
        self.assertIsNone(error)

    def test_parse_json_output_bad(self):
        payload, error = pipeline_mod.parse_json_output("nope")
        self.assertIsNone(payload)
        self.assertTrue(error)

    def test_run_td_missing_binary(self):
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = ""
        try:
            code, out = pipeline_mod.run_td(["--version"], cwd="/tmp")
        finally:
            os.environ["PATH"] = old_path
        self.assertEqual(code, 127)
        self.assertIn("not found", out)

    def test_drive_to_settled_no_interrupt(self):
        state = pipeline_mod.drive_to_settled(
            lambda: {"valid": True}, lambda r: {}, lambda i, s: {})
        self.assertTrue(state["valid"])

    def test_drive_to_settled_policy(self):
        seen = []

        def first():
            return {"__interrupt__": [{"value": {"q": 1}}]}

        def resume_fn(resume):
            seen.append(resume)
            return {"valid": True}

        state = pipeline_mod.drive_to_settled(
            first, resume_fn, lambda i, s: {"a": 2})
        self.assertTrue(state["valid"])
        self.assertEqual(seen, [{"a": 2}])

    def test_final_interrupt_newest(self):
        interrupts = [{"value": {"n": 1}}, {"value": {"n": 2}}]
        self.assertEqual(
            pipeline_mod.final_interrupt(interrupts), {"n": 2})
        self.assertEqual(pipeline_mod.final_interrupt([]), {})


class TestPreviewUrlThreading(unittest.TestCase):
    """Pipeline preview_url reaches AuditorGraph (#1103)."""

    def _run_stage_audit(self, **kwargs):
        import auditor

        seen = {}

        class RecordingGraph:
            def __init__(self, worktree_root, **kw):
                seen.update(kw)
                seen["worktree_root"] = worktree_root

            def run(self, resume=None):  # pragma: no cover - never driven
                raise AssertionError("drive is stubbed in this test")

        real_graph = auditor.AuditorGraph
        real_drive = pipeline_mod.drive_to_settled
        auditor.AuditorGraph = RecordingGraph
        pipeline_mod.drive_to_settled = (
            lambda first, resume_fn, policy, limit=None: {
                "valid": True, "verdict": {"status": "APPROVED"}})
        try:
            pipe = pipeline_mod.Pipeline(
                repo="/tmp", intent="preview test",
                preview_url=kwargs.get("preview_url"))
            return pipe, pipe.stage_audit("/tmp/wt"), seen
        finally:
            auditor.AuditorGraph = real_graph
            pipeline_mod.drive_to_settled = real_drive

    def test_preview_url_reaches_auditor(self):
        _pipe, (status, _state, _detail), seen = self._run_stage_audit(
            preview_url="https://preview/x")
        self.assertEqual(status, "APPROVED")
        self.assertEqual(seen.get("preview_url"), "https://preview/x")

    def test_preview_url_defaults_to_none(self):
        _pipe, (_status, _state, _detail), seen = self._run_stage_audit()
        self.assertIn("preview_url", seen)
        self.assertIsNone(seen["preview_url"])


class TestRouting(unittest.TestCase):
    def test_approved_submits_and_cleans(self):
        pipe = ScriptedPipeline(
            [pipeline_mod.APPROVED], submit_pr=True)
        result = pipe.run()
        self.assertEqual(result["status"], pipeline_mod.APPROVED)
        stages = [e["stage"] for e in pipe.events]
        self.assertIn("submit", stages)
        self.assertIn("cleanup", stages)

    def test_reject_routes_repair_same_worktree(self):
        pipe = ScriptedPipeline(
            [pipeline_mod.CHANGES_REQUESTED, pipeline_mod.APPROVED],
            submit_pr=False)
        result = pipe.run()
        self.assertEqual(result["status"], pipeline_mod.APPROVED)
        self.assertEqual(result["audit_cycles"], 1)
        self.assertEqual(pipe.calls,
                         [(None, False), ("/tmp/wt-1", True)])

    def test_reject_exhausts_cycles(self):
        pipe = ScriptedPipeline(
            [pipeline_mod.CHANGES_REQUESTED] * 4, max_audit_cycles=2)
        result = pipe.run()
        self.assertEqual(result["status"], pipeline_mod.CHANGES_REQUESTED)
        self.assertEqual(result["audit_cycles"], 2)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv (langgraph)")
class TestEvals(unittest.TestCase):
    def test_all_evals_pass(self):
        import eval_pipeline
        results, ok = eval_pipeline.run_all()
        for item in results:
            self.assertTrue(item["passed"], item)
        self.assertTrue(ok)


class TestCliCommands(unittest.TestCase):
    def test_no_args_usage(self):
        import cli
        self.assertEqual(cli.main([]), 2)

    def test_unknown_command(self):
        import cli
        self.assertEqual(cli.main(["bogus"]), 2)

    def test_run_pipeline_missing_args(self):
        import cli
        self.assertEqual(cli.main(["run-pipeline"]), 2)
        self.assertEqual(
            cli.main(["run-pipeline", "--intent", "x",
                      "--repo", "/tmp", "--bogus"]), 2)

    def test_run_pipeline_bad_issue(self):
        import cli
        self.assertEqual(
            cli.main(["run-pipeline", "--intent", "x",
                      "--repo", "/tmp", "--issue", "abc"]), 2)


class TestSubmitArgv(unittest.TestCase):
    def test_submit_uses_agent_submit_pr(self):
        import json as _json
        seen = []

        def fake_td(argv, cwd=None):
            seen.append(list(argv))
            return 0, _json.dumps({"status": "success",
                                   "url": "https://example.invalid/pr/9"})

        import pipeline as pipeline_mod
        pipe = pipeline_mod.Pipeline("/tmp", "intent", issue_id=7,
                                     td_run=fake_td)
        ok, url = pipe.stage_submit("/tmp/wt", "title", "body")
        self.assertTrue(ok)
        self.assertEqual(url, "https://example.invalid/pr/9")
        self.assertEqual(
            seen[0][:3], ["agent", "submit-pr", "--worktree"])
        self.assertIn("--issue", seen[0])
        self.assertIn("7", seen[0])


class TestRealTdSmoke(unittest.TestCase):
    @unittest.skipUnless(shutil.which("td"), "no td on PATH")
    def test_td_worktree_help(self):
        code, out = pipeline_mod.run_td(["worktree", "--help"])
        self.assertEqual(code, 0)
        self.assertIn("worktree", out.lower())


if __name__ == "__main__":
    unittest.main()
