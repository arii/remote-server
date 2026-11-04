"""Coder dispatch: approved proposal -> worktree executor (issue #884)."""

import json
import os
import tempfile
import unittest
from unittest import mock

from backend import proposals

import deep_chat  # noqa: E402  (deep-agent/ is on sys.path via proposals)


class FillExecutorPromptTest(unittest.TestCase):
    def test_substitutes_every_placeholder(self):
        out = proposals.fill_executor_prompt(
            repo="arii/demo", worktree_path="/tmp/wt-1",
            branch="wt-1", base_branch="main",
            contract="BUILD: make", proposal='{"kind": "coder"}',
            brief="Implement X", dispatcher="sess:0")
        for token in ("{{REPO}}", "{{WORKTREE_PATH}}", "{{BRANCH}}",
                      "{{BASE_BRANCH}}", "{{AGENT_CONTRACT}}",
                      "{{PROPOSAL}}", "{{TASK_BRIEF}}",
                      "{{DISPATCHER}}"):
            self.assertNotIn(token, out)
        for value in ("arii/demo", "/tmp/wt-1", "wt-1", "main",
                      "BUILD: make", "Implement X", "sess:0"):
            self.assertIn(value, out)

    def test_doc_example_token_is_not_a_fill_target(self):
        out = proposals.fill_executor_prompt(
            repo="r", worktree_path="w", branch="b",
            base_branch="m", contract="", proposal="", brief="t",
            dispatcher="d")
        # Line 3 documents the convention with a literal example.
        self.assertIn("{{PLACEHOLDER}}", out)

    def test_unknown_placeholder_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            template = os.path.join(tmp, "EXECUTOR_PROMPT.md")
            with open(template, "w") as fh:
                fh.write("Do {{REPO}} then {{NEW_THING}}.\n")
            with mock.patch.object(proposals, "EXECUTOR_PROMPT_PATH",
                                   template):
                with self.assertRaises(ValueError) as ctx:
                    proposals.fill_executor_prompt(
                        repo="r", worktree_path="w", branch="b",
                        base_branch="m", contract="", proposal="",
                        brief="t", dispatcher="d")
        self.assertIn("NEW_THING", str(ctx.exception))

    def test_template_resolves_beside_module_not_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                out = proposals.fill_executor_prompt(
                    repo="arii/demo", worktree_path="w", branch="b",
                    base_branch="m", contract="", proposal="",
                    brief="t", dispatcher="d")
            finally:
                os.chdir(cwd)
        self.assertIn("arii/demo", out)


class ValidateCoderDispatchTest(unittest.TestCase):
    def test_rejects_non_arii_repos(self):
        with self.assertRaises(ValueError):
            proposals.validate_dispatch(
                {"repo": "other/demo", "branch": "main",
                 "task": "Implement X"})

    def test_accepts_arii_shape(self):
        self.assertEqual(
            proposals.validate_dispatch(
                {"kind": "coder", "repo": "arii/demo",
                 "branch": "main", "task": "Implement X"}),
            ("arii/demo", "main", "Implement X"))


class StartCoderSessionTest(unittest.TestCase):
    def test_spawns_detached_with_log(self):
        with tempfile.TemporaryDirectory() as wt:
            proc = mock.Mock()
            proc.pid = 4242
            with mock.patch("subprocess.Popen",
                             return_value=proc) as popen:
                out = proposals.start_coder_session(
                    wt, "system prompt", "Implement X")
            self.assertEqual(out["pid"], 4242)
            self.assertEqual(out["log"],
                             os.path.join(wt, ".coder-dispatch.log"))
            self.assertTrue(os.path.exists(out["log"]))
            (argv,), kwargs = popen.call_args
        # [python, coder.py, -p, --system-prompt, prompt, brief]
        self.assertEqual(argv[2:], ["-p", "--system-prompt",
                                   "system prompt", "Implement X"])
        self.assertTrue(argv[1].endswith("coder.py"))
        self.assertEqual(kwargs["cwd"], wt)
        self.assertTrue(kwargs["start_new_session"])
        proc.wait.assert_not_called()
        proc.communicate.assert_not_called()

    def test_no_secrets_in_argv(self):
        with tempfile.TemporaryDirectory() as wt:
            with mock.patch("subprocess.Popen") as popen:
                proposals.start_coder_session(wt, "prompt", "brief")
        (argv,), _ = popen.call_args
        blob = json.dumps(argv)
        for secret_word in ("token", "key", "secret", "password"):
            self.assertNotIn(secret_word, blob.lower())


class RunCoderDispatchTest(unittest.TestCase):
    def _run(self, tmp, contract=None):
        if contract is not None:
            with open(os.path.join(tmp, "AGENT_CONTRACT.md"),
                      "w") as fh:
                fh.write(contract)
        seen = {}

        def fake_create(checkout, base):
            seen["checkout"] = checkout
            seen["base"] = base
            return {"id": "wt-9", "path": tmp, "branch": "wt-9"}

        def fake_start(path, prompt, brief):
            seen["prompt"] = prompt
            seen["brief"] = brief
            return {"pid": 99,
                    "log": os.path.join(path, ".coder-dispatch.log")}

        with mock.patch.object(proposals, "create_managed_worktree",
                               side_effect=fake_create), \
                mock.patch.object(proposals, "start_coder_session",
                                  side_effect=fake_start), \
                mock.patch.object(proposals, "get_checkout_path",
                                  return_value="/srv/repos/demo"):
            out = proposals.run_coder_dispatch(
                "arii/demo", "main", "Implement X",
                proposal='{"kind": "coder"}', dispatcher="sess:0")
        return out, seen

    def test_returns_worktree_branch_log_pid(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, seen = self._run(tmp)
        self.assertEqual(out, {"worktree": tmp, "branch": "wt-9",
                               "log": os.path.join(
                                   tmp, ".coder-dispatch.log"),
                               "pid": 99})
        self.assertEqual(seen["checkout"], "/srv/repos/demo")
        self.assertEqual(seen["base"], "main")
        self.assertEqual(seen["brief"], "Implement X")

    def test_contract_from_worktree_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, seen = self._run(tmp, contract="BUILD: make")
        self.assertIn("BUILD: make", seen["prompt"])
        self.assertIn("wt-9", seen["prompt"])
        self.assertIn("sess:0", seen["prompt"])

    def test_missing_contract_is_empty_string(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, seen = self._run(tmp)
        self.assertNotIn("{{AGENT_CONTRACT}}", seen["prompt"])

    def test_base_branch_fills_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, seen = self._run(tmp)
        self.assertIn("base main", seen["prompt"])


class ExecuteCoderProposalTest(unittest.TestCase):
    def setUp(self):
        self._orig = proposals.run_coder_dispatch

    def tearDown(self):
        proposals.run_coder_dispatch = self._orig

    def _execute(self, args):
        return proposals.execute_proposal(
            args, "arii/demo", "demo", "sess:7")

    def test_ok_shape_mirrors_dispatch(self):
        proposals.run_coder_dispatch = lambda *a, **k: {
            "worktree": "/tmp/wt-1", "branch": "wt-1",
            "log": "/tmp/wt-1/.coder-dispatch.log", "pid": 4242}
        out = self._execute({"kind": "coder", "repo": "arii/demo",
                             "branch": "main", "task": "Implement X"})
        self.assertEqual(out, {
            "proposal_id": "sess:7", "kind": "coder",
            "target": "arii/demo@main", "ok": True,
            "detail": ("coder pid 4242 on wt-1 (/tmp/wt-1); "
                       "log /tmp/wt-1/.coder-dispatch.log"),
            "skipped": False})

    def test_bad_repo_skips(self):
        proposals.run_coder_dispatch = mock.Mock()
        out = self._execute({"kind": "coder", "repo": "other/demo",
                             "branch": "main", "task": "Implement X"})
        self.assertTrue(out["skipped"])
        self.assertFalse(out["ok"])
        self.assertIn("arii/", out["detail"])
        proposals.run_coder_dispatch.assert_not_called()

    def test_runner_failure_answers_no_raise(self):
        def boom(*a, **k):
            raise RuntimeError("no worktree today")

        proposals.run_coder_dispatch = boom
        out = self._execute({"kind": "coder", "repo": "arii/demo",
                             "branch": "main", "task": "Implement X"})
        self.assertFalse(out["skipped"])
        self.assertFalse(out["ok"])
        self.assertIn("no worktree today", out["detail"])

    def test_unknown_kind_message_lists_coder(self):
        out = self._execute({"kind": "teleport"})
        self.assertTrue(out["skipped"])
        self.assertIn("'coder'", out["detail"])


class CoderApprovalFlowTest(unittest.TestCase):
    """Gate and executor agree: approval flows proposal->executor."""

    def test_gate_accepts_coder_shape(self):
        ok, text = deep_chat.validate_proposal(
            {"kind": "coder", "repo": "arii/demo", "branch": "main",
             "task": "Implement X"})
        self.assertTrue(ok)
        self.assertIn("PROPOSE coder", text)

    def test_gate_rejects_bad_coder_shape(self):
        ok, text = deep_chat.validate_proposal(
            {"kind": "coder", "repo": "arii/demo", "branch": "main",
             "task": ""})
        self.assertFalse(ok)
        self.assertIn("REJECTED", text)

    def test_approval_flows_gate_to_executor(self):
        args = {"kind": "coder", "repo": "arii/demo",
                "branch": "main", "task": "Implement X"}
        ok, _ = deep_chat.validate_proposal(args)
        self.assertTrue(ok)
        with mock.patch.object(
                proposals, "run_coder_dispatch",
                return_value={"worktree": "/tmp/wt-1",
                              "branch": "wt-1",
                              "log": "/tmp/wt-1/.coder-dispatch.log",
                              "pid": 4242}):
            out = proposals.execute_proposal(
                args, "arii/demo", "demo", "sess:7")
        self.assertFalse(out["skipped"])
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind"], "coder")


if __name__ == "__main__":
    unittest.main()
