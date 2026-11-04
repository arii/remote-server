"""review command tests. Hermetic: td subprocesses and the model are
stubbed; nothing touches the network, disk checkouts, or a real PR.
Run: python3 tests/test_review.py"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import core_cli.__main__ as dispatcher
from core_cli import review

DIFF = """diff --git a/backend/server.py b/backend/server.py
index 111..222 100644
--- a/backend/server.py
+++ b/backend/server.py
@@ -1,2 +1,3 @@
 x = 1
+y = 2
 z = 3
diff --git a/tests/test_server.py b/tests/test_server.py
index 333..444 100644
--- a/tests/test_server.py
+++ b/tests/test_server.py
@@ -1 +1,2 @@
 import server
+assert server.x == 1
"""

PR_DIFF_PAYLOAD = {
    "status": "success",
    "pr_number": 7,
    "files": [
        {"path": "backend/server.py", "status": "modified",
         "additions": 1, "deletions": 0},
        {"path": "tests/test_server.py", "status": "modified",
         "additions": 1, "deletions": 0},
    ],
    "diff_text": DIFF,
    "is_truncated": False,
}

PR_VIEW_PAYLOAD = {
    "status": "success",
    "pr_number": 7,
    "title": "Add y to server",
    "state": "OPEN",
    "draft": False,
}

MODEL_JSON = json.dumps({
    "summary": "Adds y alongside x with a test.",
    "findings": [
        {"file": "backend/server.py", "line": 2, "severity": "minor",
         "claim": "y is unused", "suggestion": "Use it or drop it."},
        {"file": "backend/server.py", "line": 0, "severity": "CRITICAL",
         "claim": "x is a global", "suggestion": ""},
        {"file": "", "claim": "dropped: no file"},
        {"file": "nope.py"},
    ],
    "verdict": "Needs attention: unused y.",
})


class FakeModel:
    def __init__(self, text):
        self.text = text
        self.prompts = []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return mock.Mock(content=self.text)


def stub_td(test, diff_payload=None, view_payload=None):
    """Route review._td_json to canned td payloads."""
    diff_payload = PR_DIFF_PAYLOAD if diff_payload is None else diff_payload
    view_payload = PR_VIEW_PAYLOAD if view_payload is None else view_payload

    def fake(argv, timeout=60, cwd=None):
        if argv[:2] == ["gh", "pr-diff"]:
            return dict(diff_payload)
        if argv[:2] == ["gh", "pr-view"]:
            return dict(view_payload)
        raise AssertionError(f"unexpected td argv: {argv}")

    return mock.patch.object(review, "_td_json", side_effect=fake)


class TestPlan(unittest.TestCase):
    def test_plan_shape(self):
        plan = review.build_plan("arii/remote-server", 7,
                                 dict(PR_VIEW_PAYLOAD),
                                 PR_DIFF_PAYLOAD["files"], DIFF)
        self.assertEqual(plan["repo"], "arii/remote-server")
        self.assertEqual(plan["pr"], 7)
        self.assertEqual(plan["title"], "Add y to server")
        self.assertEqual(plan["model"],
                         {"preferred": "ollama", "fallback": "nebius"})
        self.assertEqual(plan["scope"]["file_count"], 2)
        self.assertIn("backend", plan["scope"]["top_dirs"])
        self.assertEqual(len(plan["files"]), 2)
        entry = plan["files"][0]
        self.assertEqual(entry["path"], "backend/server.py")
        self.assertGreaterEqual(len(entry["risk_prompts"]), 3)

    def test_test_coverage_flags(self):
        plan = review.build_plan("r", 1, {}, PR_DIFF_PAYLOAD["files"], DIFF)
        cov = plan["test_coverage"]
        self.assertTrue(cov["tests_changed"])
        self.assertEqual(cov["test_files"], ["tests/test_server.py"])
        self.assertEqual(cov["uncovered_src"], [])

    def test_no_tests_marks_uncovered_src(self):
        files = [dict(PR_DIFF_PAYLOAD["files"][0])]
        plan = review.build_plan("r", 1, {}, files, DIFF)
        cov = plan["test_coverage"]
        self.assertFalse(cov["tests_changed"])
        self.assertEqual(cov["uncovered_src"], ["backend/server.py"])

    def test_split_diff(self):
        parts = review._split_diff(DIFF)
        self.assertEqual([p for p, _ in parts],
                         ["backend/server.py", "tests/test_server.py"])
        self.assertEqual(review._split_diff(""), [])
        self.assertEqual(review._split_diff("garbage")[0][0], "")


class TestFindings(unittest.TestCase):
    def test_schema_and_normalization(self):
        findings = review.normalize_findings(json.loads(MODEL_JSON), [])
        self.assertEqual(len(findings), 2)
        first, second = findings
        self.assertEqual(first,
                         {"file": "backend/server.py", "line": 2,
                          "severity": "minor", "claim": "y is unused",
                          "suggestion": "Use it or drop it."})
        self.assertEqual(second["severity"], "critical")
        self.assertIsNone(second["line"])

    def test_bare_list_and_garbage(self):
        self.assertEqual(review.normalize_findings(
            [{"file": "a.py", "claim": "c", "severity": "bogus",
              "line": "xx"}], []),
            [{"file": "a.py", "line": None, "severity": "info",
              "claim": "c", "suggestion": ""}])
        self.assertEqual(review.normalize_findings(None, []), [])
        self.assertEqual(review.normalize_findings({"findings": "x"}, []), [])
        self.assertEqual(review.normalize_findings([None, "x"], []), [])

    def test_extract_json_shapes(self):
        self.assertEqual(review._extract_json('{"a": 1}'), {"a": 1})
        self.assertEqual(review._extract_json(
            'prose ```json\n{"a": 1}\n``` tail'), {"a": 1})
        self.assertEqual(review._extract_json("see [1, 2] ok"), [1, 2])
        self.assertIsNone(review._extract_json("no json here"))


class TestSynthesize(unittest.TestCase):
    def plan(self):
        return review.build_plan("arii/remote-server", 7,
                                 dict(PR_VIEW_PAYLOAD),
                                 PR_DIFF_PAYLOAD["files"], DIFF)

    def test_ollama_first_nebius_fallback(self):
        model = FakeModel(MODEL_JSON)
        calls = []

        def factory(name):
            calls.append(name)
            if name == "ollama":
                raise RuntimeError("ollama down")
            return model, {"provider": "nebius", "model": "glm"}

        buf = io.StringIO()
        with redirect_stderr(buf):
            findings, summary, verdict, label = review.synthesize(
                self.plan(), DIFF, "", prefer="ollama",
                model_factory=factory)
        self.assertEqual(calls, ["ollama", "nebius"])
        self.assertIn("fallback", label)
        self.assertEqual(len(findings), 2)
        self.assertIn("Adds y", summary)
        self.assertIn("Needs attention", verdict)
        self.assertIn("PR #7", model.prompts[0])
        self.assertIn("y = 2", model.prompts[0])

    def test_nebius_prefer_is_single_leg(self):
        model = FakeModel('{"findings": []}')
        calls = []

        def factory(name):
            calls.append(name)
            return model, {"provider": "nebius", "model": "glm"}

        findings, summary, verdict, label = review.synthesize(
            self.plan(), DIFF, "", prefer="nebius", model_factory=factory)
        self.assertEqual(calls, ["nebius"])
        self.assertEqual(findings, [])
        self.assertEqual(verdict, "Looks good")
        self.assertNotIn("fallback", label)

    def test_no_model_raises(self):
        def factory(name):
            raise RuntimeError("down")

        with self.assertRaises(review.ReviewError):
            review.synthesize(self.plan(), DIFF, "",
                              model_factory=factory)


class TestRender(unittest.TestCase):
    def test_markdown_sections(self):
        findings = review.normalize_findings(json.loads(MODEL_JSON), [])
        body = review.render_markdown(
            "arii/remote-server", 7, dict(PR_VIEW_PAYLOAD), findings,
            "Adds y.", "Needs attention.", "ollama:qwen3:8b")
        self.assertIn("## Summary", body)
        self.assertIn("## Findings", body)
        self.assertIn("## Verdict", body)
        self.assertIn("backend/server.py", body)
        self.assertIn("Needs attention.", body)

    def test_empty_findings(self):
        body = review.render_markdown("r", 1, {}, [], "s", "Looks good", "m")
        self.assertIn("No findings.", body)


class TestMain(unittest.TestCase):
    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = review.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_dry_run_prints_plan_only(self):
        with stub_td(self):
            with mock.patch.object(review, "synthesize") as synth:
                code, out, _ = self.run_main(
                    ["--repo", "arii/remote-server", "--pr", "7", "--dry-run"])
        self.assertEqual(code, 0)
        synth.assert_not_called()
        plan = json.loads(out)
        self.assertEqual(plan["pr"], 7)
        self.assertEqual(len(plan["files"]), 2)
        self.assertIn("risk_prompts", plan["files"][0])

    def test_full_run_prints_markdown(self):
        model = FakeModel(MODEL_JSON)
        factory = lambda name: (model, {"provider": "ollama",
                                        "model": "qwen3:8b"})
        with stub_td(self):
            with mock.patch.object(review, "gather_context",
                                   return_value=""), \
                 mock.patch.object(review, "_default_model_factory",
                                   side_effect=factory):
                code, out, _ = self.run_main(
                    ["--repo", "arii/remote-server", "--pr", "7"])
        self.assertEqual(code, 0)
        self.assertIn("## Summary", out)
        self.assertIn("## Verdict", out)

    def test_json_flag_prints_envelope(self):
        model = FakeModel(MODEL_JSON)
        factory = lambda name: (model, {"provider": "ollama",
                                        "model": "qwen3:8b"})
        with stub_td(self):
            with mock.patch.object(review, "gather_context",
                                   return_value=""), \
                 mock.patch.object(review, "_default_model_factory",
                                   side_effect=factory):
                code, out, _ = self.run_main(
                    ["--repo", "arii/remote-server", "--pr", "7", "--json"])
        self.assertEqual(code, 0)
        envelope = json.loads(out)
        self.assertEqual(envelope["pr"], 7)
        self.assertEqual(len(envelope["findings"]), 2)
        self.assertEqual(
            sorted(envelope["findings"][0]),
            ["claim", "file", "line", "severity", "suggestion"])

    def test_post_argv_never_executes(self):
        model = FakeModel(MODEL_JSON)
        factory = lambda name: (model, {"provider": "ollama",
                                        "model": "qwen3:8b"})
        with stub_td(self):
            with mock.patch.object(review, "gather_context",
                                   return_value=""), \
                 mock.patch.object(review, "_default_model_factory",
                                   side_effect=factory), \
                 mock.patch.object(review.subprocess, "run") as run:
                run.return_value = mock.Mock(returncode=0, stdout="ok",
                                             stderr="")
                code, out, err = self.run_main(
                    ["--repo", "arii/remote-server", "--pr", "7", "--post"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:7],
                         ["td", "gh", "post-comment", "--pr", "7",
                          "--repo", "arii/remote-server"])
        self.assertEqual(argv[7], "--file")
        self.assertTrue(argv[8].endswith(".md"))
        self.assertIn("## Summary", out)
        self.assertIn("posted to", err)

    def test_default_never_posts(self):
        model = FakeModel(MODEL_JSON)
        factory = lambda name: (model, {"provider": "ollama",
                                        "model": "qwen3:8b"})
        with stub_td(self):
            with mock.patch.object(review, "gather_context",
                                   return_value=""), \
                 mock.patch.object(review, "_default_model_factory",
                                   side_effect=factory), \
                 mock.patch.object(review, "post_body") as post:
                code, _, _ = self.run_main(
                    ["--repo", "arii/remote-server", "--pr", "7"])
        self.assertEqual(code, 0)
        post.assert_not_called()

    def test_invalid_inputs(self):
        code, _, err = self.run_main(
            ["--repo", "nope", "--pr", "7", "--dry-run"])
        self.assertEqual(code, 2)
        self.assertIn("owner/name", err)
        code, _, _ = self.run_main(
            ["--repo", "a/b", "--pr", "0", "--dry-run"])
        self.assertEqual(code, 2)
        code, _, _ = self.run_main(
            ["--repo", "a/b", "--pr", "1", "--dry-run", "--post"])
        self.assertEqual(code, 2)

    def test_fetch_failure_returns_1(self):
        with mock.patch.object(review, "_td_json",
                               side_effect=review.ReviewError("down")):
            code, _, err = self.run_main(
                ["--repo", "a/b", "--pr", "1", "--dry-run"])
        self.assertEqual(code, 1)
        self.assertIn("down", err)

    def test_dispatcher_routes_review(self):
        self.assertIn("review", dispatcher.COMMANDS)
        blurb, func = dispatcher.COMMANDS["review"]
        self.assertTrue(blurb)
        self.assertIs(func, review.main)


class TestTdCwd(unittest.TestCase):
    def test_checkout_mapping(self):
        with mock.patch.object(review.os.path, "isdir",
                               return_value=True):
            self.assertEqual(review._td_cwd("arii/remote-server"),
                             "/srv/remote-server")
            self.assertEqual(review._td_cwd("arii/boomtick"),
                             "/srv/repos/boomtick")

    def test_missing_checkout_inherits_cwd(self):
        with mock.patch.object(review.os.path, "isdir",
                               return_value=False):
            self.assertIsNone(review._td_cwd("arii/remote-server"))
            self.assertIsNone(review._td_cwd("arii/boomtick"))

    def test_dry_run_from_engine_root_aims_td_at_checkout(self):
        # Live shape (#925): the engine runs -m steps from
        # ENGINE_ROOT, which has no project_config.json, so the td
        # fetch calls must carry the checkout cwd explicitly.
        import tempfile
        bare = tempfile.mkdtemp(prefix="engine-root-")
        checkout = tempfile.mkdtemp(prefix="checkout-")
        prev = os.getcwd()
        self.addCleanup(os.chdir, prev)
        os.chdir(bare)

        def fake_run(argv, **kwargs):
            if argv[2:4] == ["gh", "pr-diff"]:
                payload = PR_DIFF_PAYLOAD
            elif argv[2:4] == ["gh", "pr-view"]:
                payload = PR_VIEW_PAYLOAD
            else:
                raise AssertionError(f"unexpected td argv: {argv}")
            return mock.Mock(returncode=0, stdout=json.dumps(payload),
                             stderr="")

        out = io.StringIO()
        with mock.patch.object(review, "_td_cwd",
                               return_value=checkout) as td_cwd, \
             mock.patch.object(review.subprocess, "run",
                               side_effect=fake_run) as run, \
             redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = review.main(
                ["--repo", "arii/remote-server", "--pr", "7", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(td_cwd.call_count, 2)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["cwd"], checkout)
        plan = json.loads(out.getvalue())
        self.assertEqual(plan["pr"], 7)

    def test_post_carries_checkout_cwd(self):
        import tempfile
        checkout = tempfile.mkdtemp(prefix="checkout-")
        with mock.patch.object(review, "_td_cwd",
                               return_value=checkout), \
             mock.patch.object(review.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="ok",
                                         stderr="")
            review.post_body("arii/remote-server", 7, "# body\n")
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs["cwd"], checkout)


if __name__ == "__main__":
    unittest.main()
