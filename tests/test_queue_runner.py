import contextlib
import io
import json
import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import HTTPSHandler

import backend.shared as shared
from backend.shared import ledger
from backend.shared import model_router
from backend.shared import runs
from backend.shared import twt_receipts
from scripts import queue_runner as runner
from scripts import ledger_post
from scripts.run_root_tests import block_google_docs_requests


def setUpModule():
    unittest.enterModuleContext(block_google_docs_requests())


class ClaimsPrValidTests(unittest.TestCase):
    def test_valid_pr(self):
        ok, reason = runner.claims_pr_valid(
            {"body": "Fixes stuff. Closes #12", "files": ["tests/test_x.py"]}
        )
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_missing_closes(self):
        ok, reason = runner.claims_pr_valid({"body": "no ref", "files": ["tests/x.py"]})
        self.assertFalse(ok)
        self.assertIn("Closes", reason)

    def test_no_test_file(self):
        ok, reason = runner.claims_pr_valid(
            {"body": "Closes #3", "files": ["src/a.py"]}
        )
        self.assertFalse(ok)
        self.assertIn("test", reason)

    def test_missing_body_or_files(self):
        self.assertFalse(runner.claims_pr_valid({})[0])
        self.assertFalse(runner.claims_pr_valid({"body": "Closes #3"})[0])


class OutOfScopeFindingTests(unittest.TestCase):
    def test_row_shape(self):
        row = ledger_post.format_out_of_scope_finding("#1553", "Probe", "out of scope")
        self.assertEqual(
            row, {"issue": "#1553", "title": "Probe", "problem": "out of scope"}
        )

    def test_keys_only(self):
        row = ledger_post.format_out_of_scope_finding("1", "t", "p")
        self.assertEqual(sorted(row), ["issue", "problem", "title"])

    def test_empty_fields_allowed(self):
        self.assertEqual(
            ledger_post.format_out_of_scope_finding("", "", ""),
            {"issue": "", "title": "", "problem": ""},
        )

    def test_post_uses_task_shape(self):
        calls = {}
        real = ledger_post.post_entry
        ledger_post.post_entry = lambda agent, note, status="Act", link="": (
            calls.update({"agent": agent, "note": note, "status": status, "link": link})
            or 200
        )
        try:
            rc = ledger_post.post_out_of_scope_finding(
                "queue", "#1553", "Extra", "out of scope"
            )
        finally:
            ledger_post.post_entry = real
        self.assertEqual(rc, 200)
        self.assertEqual(calls["agent"], "queue")
        self.assertIn("[#1553]", calls["note"])
        self.assertIn("Problem:", calls["note"])
        self.assertEqual(calls["status"], "Act")

    def test_claim_row_posts_act_keyed_by_issue_url(self):
        url = "https://github.com/arii/remote-server/issues/1537"
        with mock.patch.object(ledger_post, "post_entry", return_value=200) as post:
            rc = ledger_post.post_claim_row(url, "run123")
        self.assertEqual(rc, 200)
        post.assert_called_once_with(
            "S5-Queue", "claimed by queue run run123", status="Act", link=url
        )


class LedgerNetworkGuardTests(unittest.TestCase):
    def test_real_claim_post_fails_before_https_even_if_error_is_caught(self):
        with mock.patch.object(HTTPSHandler, "https_open", autospec=True) as https_open:
            with self.assertRaisesRegex(AssertionError, r"docs\.google\.com"):
                with block_google_docs_requests():
                    with contextlib.suppress(AssertionError):
                        ledger_post.post_claim_row(ISSUE_URL, "fixture")
            https_open.assert_not_called()


class RetryWorktreeTests(unittest.TestCase):
    def git(self, repo, *args):
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE
        ).strip()

    def fixture(self, root, detached=False):
        root = Path(root)
        repo = root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        (repo / "tracked.txt").write_text("original")
        self.git(repo, "add", "tracked.txt")
        self.git(
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        )
        worktree = repo / ".worktrees/issue-7"
        worktree.parent.mkdir()
        args = ["--detach"] if detached else ["-b", "issue-7"]
        self.git(repo, "worktree", "add", *args, str(worktree))
        (worktree / "tracked.txt").write_text("dirty evidence")
        (worktree / "evidence.txt").write_text("untracked evidence")
        archive = root / "archive-7"
        archive.mkdir()
        runner.write_json(
            archive / "job.json",
            {"checkout": str(repo), "issue": {"repo": "owner/repo", "number": 7}},
        )
        return repo, worktree, archive

    def test_preserves_worktree_and_branch_and_allows_a_fresh_worktree(self):
        for detached in (False, True):
            with self.subTest(detached=detached), tempfile.TemporaryDirectory() as root:
                repo, worktree, archive = self.fixture(root, detached)
                self.assertEqual(runner.archive_issue_worktree(archive), 0)
                saved = runner.read_json(archive / "worktree-archive.json")
                destination = Path(saved["destination"])
                self.assertFalse(worktree.exists())
                self.assertEqual(
                    (destination / "evidence.txt").read_text(), "untracked evidence"
                )
                self.assertEqual(
                    (destination / "tracked.txt").read_text(), "dirty evidence"
                )
                self.assertTrue(saved["complete"])
                if not detached:
                    self.assertEqual(
                        self.git(repo, "rev-parse", saved["archive_branch"]),
                        saved["head"],
                    )
                args = ["--detach"] if detached else ["-b", "issue-7"]
                self.git(repo, "worktree", "add", *args, str(worktree))

    def test_host_dispatches_archival_in_the_configured_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            _, worktree, archive = self.fixture(root)
            config = {
                "runtime_state_dir": root,
                "runtime_command": [],
                "pipeline_checkout": os.getcwd(),
                "worker_python": sys.executable,
            }
            runner.archive_retry_worktree(config, Path(root), archive)
            self.assertTrue(
                runner.read_json(archive / "worktree-archive.json")["complete"]
            )
            self.assertFalse(worktree.exists())

    def test_branch_ref_rename_before_head_update_resumes(self):
        with tempfile.TemporaryDirectory() as root:
            repo, worktree, archive = self.fixture(root)
            original = runner.subprocess.check_output

            def command(args, **kwargs):
                if args[3:5] == ["branch", "-m"]:
                    self.git(repo, "update-ref", "refs/heads/" + args[-1], "HEAD")
                    self.git(repo, "update-ref", "-d", "refs/heads/issue-7")
                    raise OSError("interrupted before worktree HEAD update")
                return original(args, **kwargs)

            with (
                mock.patch.object(
                    runner.subprocess, "check_output", side_effect=command
                ),
                self.assertRaises(OSError),
            ):
                runner.archive_issue_worktree(archive)
            self.assertEqual(runner.archive_issue_worktree(archive), 0)
            saved = runner.read_json(archive / "worktree-archive.json")
            self.assertEqual(
                self.git(Path(saved["destination"]), "rev-parse", "HEAD"), saved["head"]
            )
            self.assertEqual(
                (Path(saved["destination"]) / "tracked.txt").read_text(),
                "dirty evidence",
            )

    def test_missing_worktree_is_an_idempotent_noop(self):
        with tempfile.TemporaryDirectory() as root:
            repo, worktree, archive = self.fixture(root)
            self.git(repo, "worktree", "remove", "--force", str(worktree))
            for _ in range(2):
                self.assertEqual(runner.archive_issue_worktree(archive), 0)
            self.assertTrue(
                runner.read_json(archive / "worktree-archive.json")["absent"]
            )

    def test_interrupted_branch_move_and_final_write_resume_safely(self):
        for failure in ("move", "metadata", "status"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                repo, worktree, archive = self.fixture(root)
                original = runner.subprocess.check_output
                write = runner.write_json

                def command(args, **kwargs):
                    if args[3:5] == ["worktree", "move"]:
                        if failure == "move":
                            raise OSError("interrupted move")
                        if failure == "metadata":
                            worktree.rename(Path(args[-1]))
                            raise OSError("interrupted metadata update")
                    return original(args, **kwargs)

                def persist(path, value, owner=None):
                    if failure == "status" and value.get("complete"):
                        raise OSError("interrupted final write")
                    write(path, value, owner)

                with (
                    mock.patch.object(
                        runner.subprocess, "check_output", side_effect=command
                    ),
                    mock.patch.object(runner, "write_json", side_effect=persist),
                    self.assertRaises(OSError),
                ):
                    runner.archive_issue_worktree(archive)
                self.assertEqual(runner.archive_issue_worktree(archive), 0)
                saved = runner.read_json(archive / "worktree-archive.json")
                self.assertEqual(
                    (Path(saved["destination"]) / "evidence.txt").read_text(),
                    "untracked evidence",
                )
                self.assertFalse(worktree.exists())

    def test_conflicting_archive_and_unregistered_directory_fail_closed(self):
        for conflict in (True, False):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as root:
                repo, worktree, archive = self.fixture(root)
                if conflict:
                    (worktree.parent / ".queue-retry" / archive.name).mkdir(
                        parents=True
                    )
                else:
                    self.git(repo, "worktree", "remove", "--force", str(worktree))
                    worktree.mkdir()
                    (worktree / "evidence.txt").write_text("unregistered evidence")
                with self.assertRaises(
                    (runner.SafetyStop, subprocess.CalledProcessError)
                ):
                    runner.archive_issue_worktree(archive)
                self.assertTrue((worktree / "evidence.txt").exists())


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(
            mock.patch.object(ledger_post, "post_entry", return_value=200)
        )
        for name, value in (
            ("ledger_write", mock.Mock(return_value=200)),
            ("ledger_rows", mock.Mock(return_value=[])),
        ):
            patcher = mock.patch.object(runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        archive = mock.patch.object(runner, "archive_retry_worktree")
        self.archive_worktree = archive.start()
        self.addCleanup(archive.stop)

    def test_retry_worktree_failure_keeps_durable_intent_and_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            config, state, directory = self.failed_run(root)
            self.archive_worktree.side_effect = OSError("interrupted worktree archive")
            with (
                mock.patch.object(
                    runner.subprocess,
                    "Popen",
                    return_value=self.retry_process(directory),
                ),
                self.assertRaisesRegex(OSError, "interrupted worktree"),
            ):
                runner.retry_issue(config, "owner/repo#7")
            saved = runner.read_json(state / "status.json")
            intent = saved["retry_intent"]
            self.assertIn("owner/repo#7", saved["completed"])
            self.assertTrue((Path(intent["directory"]) / "ledger.jsonl").exists())
            self.archive_worktree.side_effect = None
            runner.finish_retry(
                state, state / "status.json", saved, runner.read_json(config)
            )
            self.assertEqual(self.archive_worktree.call_count, 2)
            self.assertNotIn(
                "owner/repo#7", runner.read_json(state / "status.json")["completed"]
            )

    def test_receipts_normal_empty_and_local_missing(self):
        self.assertEqual(runner.receipt_total([], [], []), 0)
        self.assertEqual(runner.receipt_total(["local"], ["local"], []), 0)
        self.assertEqual(
            runner.receipt_total(
                ["paid"],
                [],
                [{"trace_id": "paid", "cost_complete": True, "cost_usd": 0.1}],
            ),
            0.1,
        )

    def test_missing_unknown_invalid_and_duplicate_receipts_fail_closed(self):
        for receipts in (
            [],
            [{"trace_id": "paid", "cost_usd": None}],
            [{"trace_id": "paid", "cost_usd": True, "cost_complete": True}],
            [{"trace_id": "paid", "cost_usd": -1, "cost_complete": True}],
            [{"trace_id": "other", "cost_usd": 0, "cost_complete": True}],
            [{"trace_id": "paid"}, {"trace_id": "paid"}],
        ):
            with self.subTest(receipts=receipts), self.assertRaises(runner.SafetyStop):
                runner.receipt_total(["paid"], [], receipts)

    def test_cap_validation(self):
        self.assertEqual(runner.checked_cap(0.5), 0.5)
        for value in (True, 0, -1, 0.51, float("nan"), float("inf"), "0.5"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                runner.checked_cap(value)

    def test_bounds_use_the_shared_contract_and_local_costs_are_zero(self):
        bounded = {
            "trace_id": "paid",
            "cost_complete": False,
            "cost_usd": None,
            "cost_basis": "upper_bound",
            "cost_upper_bound_usd": 0.4,
        }
        self.assertEqual(runner.receipt_total(["paid"], [], [bounded]), 0.4)
        for value in (None, -1, True, "0.4", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(runner.SafetyStop):
                runner.receipt_total(
                    ["paid"], [], [{**bounded, "cost_upper_bound_usd": value}]
                )
        with self.assertRaises(runner.SafetyStop):
            runner.receipt_total(["paid"], ["paid"], [bounded])

    def test_manifest_order_and_policy_skips(self):
        issues = [
            {
                "repo": "repo/name",
                "number": 10,
                "group": "followups",
                "created_at": "2020-01-01",
            },
            {
                "repo": "repo/name",
                "number": 12,
                "group": "code-reaper",
                "created_at": "2020-01-03",
            },
            {
                "repo": "repo/name",
                "number": 11,
                "group": "code-reaper",
                "created_at": "2020-01-02",
            },
        ]
        self.assertEqual(
            [i["number"] for i in runner.ordered_issues(issues)], [11, 12, 10]
        )
        for number in (2, 23, 26, 1377, 1359):
            self.assertTrue(
                runner.skip_reason({"repo": "arii/remote-server", "number": number})
            )
        for field in ("fixed", "merged", "requires_human"):
            self.assertTrue(
                runner.skip_reason({"repo": "repo/name", "number": 10, field: True})
            )
        self.assertEqual(
            runner.skip_reason(
                {"repo": "repo/name", "number": 10, "skip_reason": "operator hold"}
            ),
            "operator hold",
        )
        for issue in (
            {"repo": "bad/path/escape", "number": 7},
            {"repo": "repo/name", "number": True},
        ):
            with self.assertRaises(ValueError):
                runner.ordered_issues([issue])
        manifest = runner.read_json("scripts/config/twt-queue-issues.json")
        self.assertTrue(
            next(
                i
                for i in manifest
                if i["repo"] == "arii/remote-server" and i["number"] == 1359
            )["skip_reason"]
        )

    def test_config_can_come_from_environment(self):
        with (
            mock.patch.dict(os.environ, {"QUEUE_CONFIG": "/config.json"}),
            mock.patch("sys.argv", ["queue_runner.py"]),
            mock.patch.object(runner, "supervise", return_value=0) as supervise,
        ):
            self.assertEqual(runner.main(), 0)
            supervise.assert_called_once_with(Path("/config.json"), clear_halt=False)

    def test_human_decision_skips_are_repo_scoped_but_operator_holds_are_global(self):
        self.assertTrue(
            runner.skip_reason({"repo": "arii/remote-server", "number": 1377})
        )
        self.assertIsNone(runner.skip_reason({"repo": "arii/boomtick", "number": 1377}))
        for repo in ("arii/remote-server", "arii/boomtick", "other/repo"):
            for number in (2, 23, 26):
                self.assertTrue(runner.skip_reason({"repo": repo, "number": number}))

    @mock.patch.object(runner.signal, "signal")
    def test_stop_sentinel_prevents_dispatch(self, _signal):
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            state.mkdir()
            (state / "STOP").touch()
            with mock.patch.object(runner.subprocess, "check_output") as github:
                self.assertEqual(runner.supervise(config), 0)
                github.assert_not_called()

    def fixture(self, root):
        root = Path(root)
        issue = {"repo": "owner/repo", "number": 7}
        runner.write_json(root / "issues.json", [issue])
        config = {
            "issues_file": str(root / "issues.json"),
            "state_dir": str(root / "state"),
            "runtime_state_dir": "/runtime",
            "repo_paths": {"owner/repo": "/repo"},
            "pipeline_checkout": "/pipeline",
            "runtime_command": ["runtime"],
            "worker_python": "/runtime/venv/bin/python",
            "inter_issue_delay_seconds": 0,
        }
        runner.write_json(root / "config.json", config)
        return root / "config.json", root / "state"

    def failed_run(self, root):
        config, state = self.fixture(root)
        directory = state / "owner_repo-7"
        directory.mkdir(parents=True)
        runner.write_json(
            directory / "job.json", {"issue": {"repo": "owner/repo", "number": 7}}
        )
        runner.write_json(
            directory / "summary.json", {"exit_code": 1, "halt_queue": False}
        )
        (directory / "ledger.jsonl").write_text("original ledger evidence\n")
        runner.write_json(
            state / "status.json",
            {"completed": ["owner/repo#7", "owner/repo#9"], "done": True},
        )
        return config, state, directory

    def retry_process(self, directory, cost=0.1, reconciled=True):
        process = mock.Mock(returncode=0 if reconciled else 2)
        process.communicate.side_effect = lambda _: runner.write_json(
            directory / "recovery.json",
            {"reconciled": reconciled, "cost_usd": cost},
        )
        return process

    @mock.patch.object(runner.signal, "signal")
    def test_interrupted_retry_finishes_archival_on_restart(self, _signal):
        for failure in ("move", "status"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                config, state, directory = self.failed_run(root)
                original_write = runner.write_json
                original_rename = Path.rename

                def write(path, value, failure=failure):
                    if Path(path) == state / "status.json" and value.get("retry"):
                        if failure == "status":
                            raise OSError("interrupted status write")
                    original_write(path, value)

                def rename(path, target, failure=failure):
                    if path == directory and failure == "move":
                        raise OSError("interrupted archive move")
                    return original_rename(path, target)

                with (
                    mock.patch.object(
                        runner.subprocess,
                        "Popen",
                        return_value=self.retry_process(directory),
                    ),
                    mock.patch.object(runner, "write_json", side_effect=write),
                    mock.patch.object(Path, "rename", rename),
                    self.assertRaises(OSError),
                ):
                    runner.retry_issue(config, "owner/repo#7")
                saved = runner.read_json(state / "status.json")
                self.assertIn("retry_intent", saved)
                self.assertEqual(saved["retry_intent"]["prior_spend_usd"], 0.1)
                (state / "STOP").touch()
                with mock.patch.object(runner.subprocess, "Popen") as spawn:
                    self.assertEqual(runner.supervise(config), 0)
                    spawn.assert_not_called()
                saved = runner.read_json(state / "status.json")
                self.assertNotIn("retry_intent", saved)
                self.assertNotIn("owner/repo#7", saved["completed"])
                self.assertEqual(saved["retry"], "owner/repo#7")
                self.assertEqual(saved["retry_spend"]["owner/repo#7"], 0.1)
                self.assertEqual(len(saved["retry_archives"]), 1)
                archive = Path(saved["retry_archives"][0]["directory"])
                self.assertEqual(
                    (archive / "ledger.jsonl").read_text(), "original ledger evidence\n"
                )
                self.assertFalse(directory.exists())

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess,
        "check_output",
        return_value=json.dumps(
            {"state": "open", "labels": [{"name": "ready"}], "title": "Fix"}
        ),
    )
    def test_lowered_cap_halts_retry_without_creating_empty_directory(
        self, _read, _signal
    ):
        for cap in (0.2, 0.3):
            with self.subTest(cap=cap), tempfile.TemporaryDirectory() as root:
                config, state = self.fixture(root)
                settings = runner.read_json(config)
                settings["cap_usd"] = cap
                runner.write_json(config, settings)
                state.mkdir()
                runner.write_json(
                    state / "status.json",
                    {
                        "completed": [],
                        "retry": "owner/repo#7",
                        "retry_spend": {"owner/repo#7": 0.3},
                    },
                )
                with mock.patch.object(runner.subprocess, "Popen") as spawn:
                    self.assertEqual(runner.supervise(config), 2)
                    with self.assertRaisesRegex(runner.SafetyStop, "queue is halted"):
                        runner.supervise(config)
                    spawn.assert_not_called()
                self.assertFalse((state / "owner_repo-7").exists())
                saved = runner.read_json(state / "status.json")
                self.assertTrue(saved["halted"])
                self.assertIsNone(saved.get("current"))
                self.assertIn("budget", saved["reason"])
                self.assertEqual(saved["retry"], "owner/repo#7")

    def test_retry_archives_evidence_and_preserves_halts_stop_and_other_issues(self):
        with tempfile.TemporaryDirectory() as root:
            config, state, directory = self.failed_run(root)
            status = runner.read_json(state / "status.json")
            status.update(halted=True, reason="operator hold")
            runner.write_json(state / "status.json", status)
            (state / "STOP").touch()
            with mock.patch.object(
                runner.subprocess,
                "Popen",
                return_value=self.retry_process(directory),
            ) as spawn:
                self.assertEqual(runner.retry_issue(config, "owner/repo#7"), 0)
            self.assertIn("--reconcile", spawn.call_args.args[0])
            self.archive_worktree.assert_called_once()
            self.assertFalse(directory.exists())
            status = runner.read_json(state / "status.json")
            archive = Path(status["retry_archives"][0]["directory"])
            self.assertEqual(
                (archive / "ledger.jsonl").read_text(), "original ledger evidence\n"
            )
            self.assertTrue((archive / "summary.json").exists())
            self.assertTrue(runner.read_json(archive / "recovery.json")["reconciled"])
            self.assertEqual(status["completed"], ["owner/repo#9"])
            self.assertEqual(status["retry"], "owner/repo#7")
            self.assertEqual(status["retry_spend"], {"owner/repo#7": 0.1})
            self.assertFalse(status["done"])
            self.assertTrue(status["halted"])
            self.assertEqual(status["reason"], "operator hold")
            self.assertTrue((state / "STOP").exists())
            with self.assertRaises(runner.SafetyStop):
                runner.retry_issue(config, "owner/repo#7")

    def test_retry_accepts_budget_exhausted_as_operator_approval(self):
        with tempfile.TemporaryDirectory() as root:
            config, state, directory = self.failed_run(root)
            runner.write_json(
                directory / "summary.json",
                {
                    "exit_code": 1,
                    "halt_queue": False,
                    "result": {"status": "BUDGET_EXHAUSTED"},
                    "budget": {"parent": {"calls_used": 60, "exhausted": True}},
                },
            )
            with mock.patch.object(
                runner.subprocess, "Popen", return_value=self.retry_process(directory)
            ):
                self.assertEqual(runner.retry_issue(config, "owner/repo#7"), 0)
            status = runner.read_json(state / "status.json")
            self.assertEqual(status["retry"], "owner/repo#7")
            self.assertNotIn("owner/repo#7", status["completed"])
            # The archived run (with its spent budget.json) is gone from the
            # job dir, so the retried run starts a fresh parent budget.
            self.assertFalse((directory / "summary.json").exists())

    def test_retry_refuses_active_supervisor(self):
        with tempfile.TemporaryDirectory() as root:
            config, state, directory = self.failed_run(root)
            with (state / "queue.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(runner.subprocess, "Popen") as spawn:
                    with self.assertRaises(BlockingIOError):
                        runner.retry_issue(config, "owner/repo#7")
                    spawn.assert_not_called()
            self.assertTrue(directory.exists())

    def test_retry_rejects_missing_held_successful_or_unfinished_runs(self):
        for case in ("missing", "held", "success", "unfinished", "halt", "identity"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as root:
                config, state, directory = self.failed_run(root)
                if case in ("missing", "held"):
                    runner.write_json(
                        Path(root) / "issues.json",
                        []
                        if case == "missing"
                        else [
                            {"repo": "owner/repo", "number": 7, "requires_human": True}
                        ],
                    )
                elif case == "identity":
                    runner.write_json(
                        directory / "job.json",
                        {"issue": {"repo": "foreign/repo", "number": 7}},
                    )
                elif case == "unfinished":
                    runner.write_json(
                        state / "status.json",
                        {"current": "owner/repo#7", "completed": []},
                    )
                else:
                    runner.write_json(
                        directory / "summary.json",
                        {
                            "exit_code": 0 if case == "success" else 1,
                            "halt_queue": case == "halt",
                        },
                    )
                with mock.patch.object(runner.subprocess, "Popen") as spawn:
                    with self.assertRaises(runner.SafetyStop):
                        runner.retry_issue(config, "owner/repo#7")
                    spawn.assert_not_called()
                self.assertTrue(directory.exists())

    def test_retry_failed_reconciliation_or_invalid_spend_never_archives(self):
        for cost, reconciled in (
            (0, False),
            (None, True),
            (-1, True),
            (float("nan"), True),
            (True, True),
            (0.5, True),
        ):
            with (
                self.subTest(cost=cost, reconciled=reconciled),
                tempfile.TemporaryDirectory() as root,
            ):
                config, state, directory = self.failed_run(root)
                before = runner.read_json(state / "status.json")
                process = self.retry_process(directory, cost, reconciled)
                if isinstance(cost, float) and not runner.math.isfinite(cost):
                    process.communicate.side_effect = lambda _, directory=directory: (
                        directory / "recovery.json"
                    ).write_text('{"reconciled": true, "cost_usd": NaN}')
                with (
                    mock.patch.object(runner.subprocess, "Popen", return_value=process),
                    self.assertRaises((runner.SafetyStop, runner.BudgetStop)),
                ):
                    runner.retry_issue(config, "owner/repo#7")
                self.assertTrue(directory.exists())
                self.assertEqual(runner.read_json(state / "status.json"), before)

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess,
        "check_output",
        return_value=json.dumps(
            {
                "state": "open",
                "labels": [{"name": "ready"}],
                "title": "Fix",
                "body": "scope",
            }
        ),
    )
    def test_retried_issue_dispatches_first_with_remaining_budget_once(
        self, _read, _signal
    ):
        with tempfile.TemporaryDirectory() as root:
            config, state, directory = self.failed_run(root)
            runner.write_json(
                Path(root) / "issues.json",
                [
                    {"repo": "owner/repo", "number": 8, "group": "code-reaper"},
                    {"repo": "owner/repo", "number": 7},
                ],
            )
            seeded = runner.read_json(state / "status.json")
            seeded["tier_failures"] = {"owner/repo#7": 2}
            runner.write_json(state / "status.json", seeded)
            with mock.patch.object(
                runner.subprocess, "Popen", return_value=self.retry_process(directory)
            ):
                runner.retry_issue(config, "owner/repo#7")
            self.assertEqual(
                runner.read_json(state / "status.json")["tier_failures"],
                {"owner/repo#7": 2},
            )
            dispatched = []

            def spawn(command, **_kwargs):
                name = Path(command[-1]).name
                dispatched.append(name)
                job = runner.read_json(state / name / "job.json")
                self.assertAlmostEqual(
                    job["cap_usd"], 0.4 if name.endswith("-7") else 0.5
                )
                self.assertEqual(
                    job["model_tier"], "strong" if name.endswith("-7") else "default"
                )
                process = mock.Mock(returncode=0)
                process.communicate.side_effect = lambda _: runner.write_json(
                    state / name / "summary.json",
                    {
                        "halt_queue": False,
                        "reconciliation": {"reconciled": True},
                        "result": {"status": "DONE"},
                        "exit_code": 0,
                    },
                )
                return process

            with mock.patch.object(runner.subprocess, "Popen", side_effect=spawn):
                self.assertEqual(runner.supervise(config), 0)
                self.assertEqual(runner.supervise(config), 0)
            self.assertEqual(dispatched, ["owner_repo-7", "owner_repo-8"])
            self.assertNotIn("retry", runner.read_json(state / "status.json"))

    def test_retry_cli_and_worker_mode_conflicts(self):
        with (
            mock.patch(
                "sys.argv",
                [
                    "queue_runner.py",
                    "--config",
                    "/config.json",
                    "--retry",
                    "owner/repo#7",
                ],
            ),
            mock.patch.object(runner, "retry_issue", return_value=0) as retry,
        ):
            self.assertEqual(runner.main(), 0)
            retry.assert_called_once_with(Path("/config.json"), "owner/repo#7")
        with (
            mock.patch(
                "sys.argv",
                ["queue_runner.py", "--worker", "/job", "--retry", "owner/repo#7"],
            ),
            self.assertRaises(SystemExit),
        ):
            runner.main()

    def test_retry_empty_state_and_invalid_keys_do_not_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            config, _state = self.fixture(root)
            with mock.patch.object(runner.subprocess, "Popen") as spawn:
                for key in ("bad", "owner/repo#not-a-number", "../escape/repo#7"):
                    with self.subTest(key=key), self.assertRaises(ValueError):
                        runner.retry_issue(config, key)
                with self.assertRaises(FileNotFoundError):
                    runner.retry_issue(config, "owner/repo#7")
                spawn.assert_not_called()

    def test_retry_counts_all_previous_attempt_spend(self):
        with tempfile.TemporaryDirectory() as root:
            config, state, directory = self.failed_run(root)
            status = runner.read_json(state / "status.json")
            status["retry_spend"] = {"owner/repo#7": 0.3}
            runner.write_json(state / "status.json", status)
            with (
                mock.patch.object(
                    runner.subprocess,
                    "Popen",
                    return_value=self.retry_process(directory, cost=0.2),
                ),
                self.assertRaises(runner.BudgetStop),
            ):
                runner.retry_issue(config, "owner/repo#7")
            self.assertTrue(directory.exists())
            self.assertEqual(runner.read_json(state / "status.json"), status)

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess, "check_output", return_value=json.dumps({"state": "closed"})
    )
    def test_retry_of_now_closed_issue_clears_pending_without_dispatch(
        self, _read, _signal
    ):
        with tempfile.TemporaryDirectory() as root:
            config, state, directory = self.failed_run(root)
            with mock.patch.object(
                runner.subprocess, "Popen", return_value=self.retry_process(directory)
            ):
                runner.retry_issue(config, "owner/repo#7")
            with mock.patch.object(runner.subprocess, "Popen") as spawn:
                self.assertEqual(runner.supervise(config), 0)
                spawn.assert_not_called()
            self.assertNotIn("retry", runner.read_json(state / "status.json"))

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess, "check_output", return_value=json.dumps({"state": "closed"})
    )
    def test_saved_current_is_reconciled_before_closed_issue_can_be_skipped(
        self, github, _signal
    ):
        for success in (False, True):
            with self.subTest(success=success), tempfile.TemporaryDirectory() as root:
                config, state = self.fixture(root)
                directory = state / "owner_repo-7"
                directory.mkdir(parents=True)
                runner.write_json(
                    directory / "job.json",
                    {"issue": {"repo": "owner/repo", "number": 7}},
                )
                runner.write_json(
                    state / "status.json", {"current": "owner/repo#7", "completed": []}
                )
                process = mock.Mock(returncode=0 if success else 2)

                def finish(_source):
                    runner.write_json(
                        directory / "recovery.json", {"reconciled": success}
                    )

                process.communicate.side_effect = finish
                with mock.patch.object(
                    runner.subprocess, "Popen", return_value=process
                ) as spawn:
                    self.assertEqual(runner.supervise(config), 0 if success else 2)
                spawn.assert_called_once()
                self.assertIn("--reconcile", spawn.call_args.args[0])
                github.assert_not_called()
                status = runner.read_json(state / "status.json")
                self.assertEqual(
                    status["completed"], ["owner/repo#7"] if success else []
                )

    @mock.patch.object(runner.signal, "signal")
    def test_successful_recovery_preserves_worker_halt_until_operator_clears_it(
        self, _signal
    ):
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            directory = state / "owner_repo-7"
            directory.mkdir(parents=True)
            runner.write_json(
                directory / "job.json", {"issue": {"repo": "owner/repo", "number": 7}}
            )
            runner.write_json(
                directory / "summary.json",
                {"halt_queue": True, "halt_reasons": ["ownership mismatch"]},
            )
            runner.write_json(
                state / "status.json", {"current": "owner/repo#7", "completed": []}
            )
            process = mock.Mock(returncode=0)
            process.communicate.side_effect = lambda _: runner.write_json(
                directory / "recovery.json", {"reconciled": True}
            )
            with mock.patch.object(runner.subprocess, "Popen", return_value=process):
                self.assertEqual(runner.supervise(config), 2)
                self.assertTrue(
                    runner.read_json(directory / "summary.json")["halt_queue"]
                )
                self.assertTrue(runner.read_json(state / "status.json")["halted"])
                (state / "STOP").touch()
                self.assertEqual(runner.supervise(config, clear_halt=True), 0)
                self.assertFalse(
                    runner.read_json(directory / "summary.json")["halt_queue"]
                )
                self.assertFalse(runner.read_json(state / "status.json")["halted"])
                self.assertFalse((state / "STOP").exists())
                self.assertEqual(
                    runner.read_json(directory / "summary.json")[
                        "acknowledged_halt_reasons"
                    ],
                    ["ownership mismatch"],
                )

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess,
        "check_output",
        return_value=json.dumps(
            {
                "state": "open",
                "labels": [{"name": "ready"}],
                "title": "Fix issue",
                "body": "scope",
            }
        ),
    )
    def test_normal_worker_failure_continues_but_unknown_cost_halts(
        self, _read, _signal
    ):
        for halt in (False, True):
            with self.subTest(halt=halt), tempfile.TemporaryDirectory() as root:
                config, state = self.fixture(root)
                process = mock.Mock()
                process.returncode = 1

                def finish(_source):
                    runner.write_json(
                        state / "owner_repo-7" / "summary.json",
                        {
                            "halt_queue": halt,
                            "halt_reasons": ["unknown"] if halt else [],
                            "reconciliation": {"reconciled": not halt},
                            "result": {"status": "BLOCKED_DETERMINISTIC"},
                            "exit_code": 1,
                        },
                    )

                process.communicate.side_effect = finish
                with mock.patch.object(
                    runner.subprocess, "Popen", return_value=process
                ):
                    self.assertEqual(runner.supervise(config), 2 if halt else 0)
                status = runner.read_json(state / "status.json")
                self.assertEqual(bool(status.get("halted")), halt)
                self.assertEqual(status["completed"], [] if halt else ["owner/repo#7"])

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess, "check_output", return_value=json.dumps({"state": "closed"})
    )
    def test_closed_issues_are_not_dispatched(self, _read, _signal):
        with tempfile.TemporaryDirectory() as root:
            config, _state = self.fixture(root)
            with mock.patch.object(runner.subprocess, "Popen") as spawn:
                self.assertEqual(runner.supervise(config), 0)
                spawn.assert_not_called()

    @mock.patch.object(runner.signal, "signal")
    def test_only_ready_open_issues_are_dispatched(self, _signal):
        for labels, refresh_error, dispatched in (
            ([{"name": "ledger-intake"}], None, False),
            ([], None, False),
            (None, None, False),
            ([{"name": "ledger-intake"}, {"name": "ready"}], None, True),
            (None, ValueError("boom"), False),
        ):
            with tempfile.TemporaryDirectory() as root:
                config, state = self.fixture(root)
                payload = {"state": "open", "title": "Fix", "body": "scope"}
                if labels is not None:
                    payload["labels"] = labels
                github = mock.Mock(
                    side_effect=refresh_error, return_value=json.dumps(payload)
                )
                process = mock.Mock(returncode=0)
                process.communicate.side_effect = lambda _source: runner.write_json(
                    state / "owner_repo-7" / "summary.json",
                    {
                        "halt_queue": False,
                        "reconciliation": {"reconciled": True},
                        "result": {"status": "DONE"},
                        "exit_code": 0,
                    },
                )
                with (
                    self.subTest(labels=labels, error=refresh_error),
                    mock.patch.object(runner.subprocess, "check_output", github),
                    mock.patch.object(
                        runner.subprocess, "Popen", return_value=process
                    ) as spawn,
                    mock.patch("builtins.print") as log,
                ):
                    self.assertEqual(runner.supervise(config), 0)
                    status = runner.read_json(state / "status.json")
                    if dispatched:
                        spawn.assert_called_once()
                        self.assertEqual(status["completed"], ["owner/repo#7"])
                        continue
                    spawn.assert_not_called()
                    self.assertEqual(status["completed"], [])
                    self.assertNotIn("skipped", status)
                    self.assertFalse(status.get("consecutive_failures"))
                    lines = [call.args[0] for call in log.call_args_list]
                    expected = (
                        "SKIP owner/repo#7 refresh failed: boom"
                        if refresh_error
                        else "SKIP owner/repo#7 not ready"
                    )
                    self.assertIn(expected, lines)

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess,
        "check_output",
        return_value=json.dumps(
            {
                "state": "open",
                "labels": [{"name": "ready"}],
                "title": "Fix issue",
                "body": "scope",
            }
        ),
    )
    def test_no_summary_and_interrupted_runs_never_restart_blindly(
        self, _read, _signal
    ):
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            with mock.patch.object(runner.subprocess, "Popen"):
                self.assertEqual(runner.supervise(config), 2)
            with mock.patch.object(
                runner.subprocess, "Popen", side_effect=OSError("runtime unavailable")
            ):
                self.assertEqual(runner.supervise(config), 2)
            self.assertTrue(runner.read_json(state / "status.json")["halted"])

    @mock.patch.object(runner.signal, "signal")
    def test_dispatch_is_sequential_ordered_and_skips_finished_and_held(self, _signal):
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            issues = [
                {"repo": "owner/repo", "number": 10, "group": "followups"},
                {
                    "repo": "owner/repo",
                    "number": 12,
                    "group": "code-reaper",
                    "created_at": "2020-01-03",
                },
                {
                    "repo": "owner/repo",
                    "number": 11,
                    "group": "code-reaper",
                    "created_at": "2020-01-02",
                },
                {
                    "repo": "owner/repo",
                    "number": 100,
                    "group": "code-reaper",
                    "created_at": "2020-01-01",
                },
                *[{"repo": "owner/repo", "number": number} for number in (2, 23, 26)],
                {"repo": "arii/remote-server", "number": 1377},
                {"repo": "arii/remote-server", "number": 1359},
                {"repo": "owner/repo", "number": 101, "fixed": True},
                {"repo": "owner/repo", "number": 102, "requires_human": True},
            ]
            runner.write_json(Path(root) / "issues.json", issues)
            dispatched, active = [], []

            def fresh(command, **_kwargs):
                number = int(command[-1].rsplit("/", 1)[-1])
                return json.dumps(
                    {
                        "state": "closed" if number == 100 else "open",
                        "labels": [{"name": "ready"}],
                        "title": "Fix",
                        "body": "scope",
                    }
                )

            def spawn(command, **_kwargs):
                self.assertFalse(active)
                self.assertEqual(
                    command[:-1],
                    [
                        "runtime",
                        "env",
                        "PYTHONPATH=/pipeline/deep-agent:/pipeline",
                        "/runtime/venv/bin/python",
                        "-",
                        "--worker",
                    ],
                )
                active.append(True)
                name = Path(command[-1]).name
                dispatched.append(int(name.rsplit("-", 1)[-1]))
                process = mock.Mock(returncode=0)

                def finish(_source):
                    active.clear()
                    runner.write_json(
                        state / name / "summary.json",
                        {
                            "halt_queue": False,
                            "reconciliation": {"reconciled": True},
                            "result": {"status": "DONE"},
                            "exit_code": 0,
                        },
                    )

                process.communicate.side_effect = finish
                return process

            with (
                mock.patch.object(runner.subprocess, "check_output", side_effect=fresh),
                mock.patch.object(runner.subprocess, "Popen", side_effect=spawn),
            ):
                self.assertEqual(runner.supervise(config), 0)
                self.assertEqual(runner.supervise(config), 0)
            self.assertEqual(dispatched, [11, 12, 10])
            self.assertEqual(len(runner.read_json(state / "status.json")["skipped"]), 7)

    def ready_fake(self, listings, failing=()):
        def fake(command, **_kwargs):
            if command[1:3] == ["issue", "list"]:
                repo = command[command.index("--repo") + 1]
                if repo in failing:
                    raise runner.subprocess.CalledProcessError(1, command)
                return json.dumps(listings.get(repo, []))
            return json.dumps(
                {
                    "state": "open",
                    "title": "Fix",
                    "body": "scope",
                    "labels": [{"name": "ready"}],
                }
            )

        return fake

    @staticmethod
    def listed(number, created, labels=("ready",)):
        return {
            "number": number,
            "title": "t",
            "createdAt": created,
            "labels": [{"name": name} for name in labels],
        }

    def test_ready_issues_are_discovered_filtered_ordered_and_not_duplicated(self):
        config = {"discover_repos": ["owner/repo", "owner/other"]}
        manifest = [{"repo": "owner/repo", "number": 7}]
        listings = {
            "owner/repo": [
                self.listed(7, "2020-01-01"),
                self.listed(9, "2020-01-09"),
                self.listed(8, "2020-01-08"),
                self.listed(5, "2020-01-05"),
                self.listed(6, "2020-01-06", labels=("bug",)),
                self.listed(23, "2020-01-01"),
            ],
            "owner/other": [self.listed(3, "2020-01-03")],
            "owner/denied": [self.listed(1, "2020-01-01")],
        }
        with mock.patch.object(
            runner.subprocess, "check_output", side_effect=self.ready_fake(listings)
        ) as gh:
            found = runner.discover_ready(config, manifest, ["owner/repo#5"])
            again = runner.discover_ready(config, manifest + found, ["owner/repo#5"])
        self.assertEqual(
            [(i["repo"], i["number"]) for i in found],
            [("owner/other", 3), ("owner/repo", 8), ("owner/repo", 9)],
        )
        self.assertEqual(again, [])
        self.assertNotIn("owner/denied", str(gh.call_args_list))
        self.assertIn("--label", gh.call_args_list[0].args[0])
        self.assertEqual(runner.discover_ready({}, manifest, []), [])

    def test_failed_listing_skips_only_that_repo(self):
        config = {"discover_repos": ["owner/repo", "owner/other"]}
        listings = {"owner/other": [self.listed(3, "2020-01-03")]}
        fake = self.ready_fake(listings, failing=("owner/repo",))
        with mock.patch.object(runner.subprocess, "check_output", side_effect=fake):
            found = runner.discover_ready(config, [], [])
        self.assertEqual([i["number"] for i in found], [3])

    @mock.patch.object(runner.signal, "signal")
    def test_supervise_dispatches_discovered_ready_issue_after_manifest(self, _signal):
        with tempfile.TemporaryDirectory() as root:
            config_path, state = self.fixture(root)
            config = runner.read_json(config_path)
            config["discover_repos"] = ["owner/repo"]
            runner.write_json(config_path, config)
            listings = {
                "owner/repo": [
                    self.listed(7, "2020-01-01"),
                    self.listed(4, "2020-01-04"),
                ]
            }
            dispatched = []

            def spawn(command, **_kwargs):
                name = Path(command[-1]).name
                dispatched.append(name)
                process = mock.Mock(returncode=0)
                process.communicate.side_effect = lambda _source: runner.write_json(
                    state / name / "summary.json",
                    {
                        "halt_queue": False,
                        "reconciliation": {"reconciled": True},
                        "result": {"status": "DONE"},
                        "exit_code": 0,
                    },
                )
                return process

            with (
                mock.patch.object(
                    runner.subprocess,
                    "check_output",
                    side_effect=self.ready_fake(listings),
                ),
                mock.patch.object(runner.subprocess, "Popen", side_effect=spawn),
            ):
                self.assertEqual(runner.supervise(config_path), 0)
                self.assertEqual(runner.supervise(config_path), 0)
            self.assertEqual(dispatched, ["owner_repo-7", "owner_repo-4"])
            self.assertEqual(
                runner.read_json(state / "status.json")["completed"],
                ["owner/repo#7", "owner/repo#4"],
            )

    @mock.patch.object(runner.signal, "signal")
    def test_discover_repos_must_have_a_checkout(self, _signal):
        with tempfile.TemporaryDirectory() as root:
            config_path, _state = self.fixture(root)
            config = runner.read_json(config_path)
            config["discover_repos"] = ["owner/unmapped"]
            runner.write_json(config_path, config)
            with self.assertRaises(ValueError):
                runner.supervise(config_path)

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess,
        "check_output",
        return_value=json.dumps(
            {
                "state": "open",
                "labels": [{"name": "ready"}],
                "title": "Fix",
                "body": "scope",
            }
        ),
    )
    def test_common_failures_halt_after_three_and_worker_exit_must_match(
        self, _read, _signal
    ):
        for bad_exit in (False, True):
            with self.subTest(bad_exit=bad_exit), tempfile.TemporaryDirectory() as root:
                config, state = self.fixture(root)
                runner.write_json(
                    Path(root) / "issues.json",
                    [{"repo": "owner/repo", "number": n} for n in range(7, 11)],
                )

                def spawn(command, **_kwargs):
                    directory = state / Path(command[-1]).name
                    process = mock.Mock(returncode=-9 if bad_exit else 1)
                    process.communicate.side_effect = lambda _: runner.write_json(
                        directory / "summary.json",
                        {
                            "halt_queue": False,
                            "reconciliation": {"reconciled": True},
                            "exit_code": 1,
                            "result": {
                                "status": "BLOCKED_DETERMINISTIC",
                                "detail": "common blocker",
                            },
                        },
                    )
                    return process

                with mock.patch.object(
                    runner.subprocess, "Popen", side_effect=spawn
                ) as start:
                    self.assertEqual(runner.supervise(config), 2)
                self.assertEqual(start.call_count, 1 if bad_exit else 3)

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess,
        "check_output",
        return_value=json.dumps(
            {
                "state": "open",
                "labels": [{"name": "ready"}],
                "title": "Fix",
                "body": "scope",
            }
        ),
    )
    def test_real_stdin_worker_uses_declared_paths_without_live_pipeline(
        self, _read, _signal
    ):
        with tempfile.TemporaryDirectory() as root:
            config_path, state = self.fixture(root)
            checkout = Path(root) / "checkout"
            agent = checkout / "deep-agent"
            agent.mkdir(parents=True)
            modules = checkout / "backend" / "shared"
            modules.mkdir(parents=True)
            (checkout / "backend" / "__init__.py").write_text("")
            (modules / "__init__.py").write_text("")
            (modules / "model_router.py").write_text(
                "class _BoundedHistory:\n    def _bounded_kwargs(self, kwargs):\n        return kwargs\n"
                "def _remote_spec(*args): return {}\n"
            )
            (modules / "ledger.py").write_text(
                "import os\nLEDGER = os.environ['EVAL_LEDGER']\n"
            )
            (modules / "runs.py").write_text(
                "def total_metered_spend():\n    return {'spent': 0, 'unknown': 0}\n"
            )
            (modules / "twt_receipts.py").write_text(
                "def receipt_cost(row):\n    return 0\n"
                "def _settled_trace_ids(): return set()\n"
                "def ledger_receipts(ids, receipts): return receipts\n"
            )
            (checkout / "scripts").mkdir()
            (checkout / "scripts" / "slop_review.py").write_text(
                "def gated_submit(original, state, **kwargs): return original\n"
                "def override_result(value, state): return value\n"
                "def checkout_root(): return None\n"
                "def default_model(thread_id): raise AssertionError\n"
            )
            (agent / "pipeline.py").write_text(
                "class Pipeline:\n"
                "    def stage_init(self): return True, {}\n"
                "    stage_spec = stage_code = stage_audit = stage_submit = stage_cleanup = stage_init\n"
                "    def run(self):\n"
                "        for stage in ('init', 'spec', 'code', 'audit', 'submit', 'cleanup'):\n"
                "            getattr(self, 'stage_' + stage)()\n"
                "        return {'status': 'DONE'}\n"
            )
            (modules / "cli.py").write_text(
                "import pipeline\n"
                "def main(argv):\n"
                "    assert '--submit-pr' in argv and '--keep-worktree' in argv\n"
                "    pipeline.Pipeline().run()\n"
                "    return 0\n"
            )
            config = runner.read_json(config_path)
            config.update(
                runtime_command=[],
                worker_python=sys.executable,
                runtime_state_dir=str(state),
                pipeline_checkout=str(checkout),
                repo_paths={"owner/repo": str(checkout)},
            )
            runner.write_json(config_path, config)
            self.assertEqual(runner.supervise(config_path), 0)
            directory = state / "owner_repo-7"
            summary = runner.read_json(directory / "summary.json")
            self.assertTrue(summary["reconciliation"]["reconciled"])
            self.assertEqual(summary["reconciliation"]["cost_usd"], 0)
            self.assertEqual(len(summary["stages"]), 6)
            self.assertTrue((directory / "state").is_dir())


ISSUE_URL = "https://github.com/owner/repo/issues/7"
PR_URL = "https://github.com/owner/repo/pull/99"
SECRET = "sk-test-SECRET-value-123"


class LedgerTests(unittest.TestCase):
    """Agent Ledger rows from the supervisor; writer and reader are faked."""

    def setUp(self):
        self.posts = []
        self.rows = []
        self.fail_posts = 0
        self.now = 1_700_000_000.0
        for name, value in (
            ("ledger_write", self.write),
            ("ledger_rows", lambda: list(self.rows)),
            ("ledger_log", lambda _message: None),
        ):
            patcher = mock.patch.object(runner, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # post_claim_row writes through ledger_post.post_entry, not runner.ledger_write
        patcher = mock.patch.object(
            ledger_post,
            "post_entry",
            lambda agent, note, status="Act", link="": self.write(
                agent, note, status, link
            ),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        for patcher in (
            mock.patch.object(runner.signal, "signal"),
            mock.patch.object(runner.time, "time", lambda: self.now),
            mock.patch.object(
                runner.subprocess,
                "check_output",
                side_effect=lambda *a, **k: json.dumps(
                    {
                        "state": "open",
                        "title": "Fix",
                        "body": "scope",
                        "labels": [{"name": "ready"}],
                    }
                ),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, agent, note, status, link):
        if self.fail_posts:
            self.fail_posts -= 1
            self.attempts = getattr(self, "attempts", 0) + 1
            raise OSError("ledger down " + SECRET)
        self.posts.append((agent, note, status, link))
        return 200

    def fixture(self, root, count=1):
        root = Path(root)
        runner.write_json(
            root / "issues.json",
            [{"repo": "owner/repo", "number": 7 + n} for n in range(count)],
        )
        runner.write_json(
            root / "config.json",
            {
                "issues_file": str(root / "issues.json"),
                "state_dir": str(root / "state"),
                "runtime_state_dir": "/runtime",
                "repo_paths": {"owner/repo": "/repo"},
                "pipeline_checkout": "/pipeline",
                "runtime_command": ["runtime"],
                "worker_python": "/runtime/venv/bin/python",
                "inter_issue_delay_seconds": 0,
                "environment": {"API_TOKEN": SECRET},
            },
        )
        return root / "config.json", root / "state"

    def spawn(self, state, summary):
        def start(command, **_kwargs):
            directory = state / Path(command[-1]).name
            process = mock.Mock(returncode=summary["exit_code"])
            process.communicate.side_effect = lambda _: runner.write_json(
                directory / "summary.json",
                {"halt_queue": False, "reconciliation": {"reconciled": True}} | summary,
            )
            return process

        return mock.patch.object(runner.subprocess, "Popen", side_effect=start)

    def run_one(self, summary, count=1):
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root, count)
            with self.spawn(state, summary):
                code = runner.supervise(config)
            return code, runner.read_json(state / "status.json"), config

    def foreign(self, status="Act", source="BoomTick", hours=1.0, link=ISSUE_URL):
        stamp = runner.datetime.datetime.fromtimestamp(
            self.now - hours * 3600, runner.zoneinfo.ZoneInfo(runner.LEDGER_TIMEZONE)
        )
        return {
            "Timestamp": stamp.strftime("%m/%d/%Y %H:%M:%S"),
            "Agent / Source": source,
            "Item / Note": "working",
            "Status": status,
            "Link": link,
        }

    SUCCESS = {
        "exit_code": 0,
        "result": {"status": "APPROVED", "submit_url": PR_URL},
        "reconciliation": {
            "reconciled": True,
            "cost_usd": 0.0123,
            "cost_basis": "upper_bound",
        },
    }

    def test_start_posts_one_act_claim_keyed_by_issue_url(self):
        self.run_one(self.SUCCESS)
        agent, note, status, link = self.posts[0]
        self.assertEqual((agent, status, link), ("S5-Queue", "Act", ISSUE_URL))
        self.assertRegex(note, r"^claimed by queue run [0-9a-f]{12}$")
        self.assertEqual(sum(p[1].startswith("claimed") for p in self.posts), 1)

    def test_success_posts_act_with_pr_cost_and_tests(self):
        _code, status, _config = self.run_one(self.SUCCESS)
        _agent, note, state, link = self.posts[-1]
        self.assertEqual((state, link), ("Act", ISSUE_URL))
        self.assertIn(PR_URL, note)
        self.assertIn("$0.0123 (upper_bound)", note)
        self.assertIn("tests pass", note)
        self.assertEqual(
            status["ledger_open_prs"]["owner/repo#7"],
            {"issue": ISSUE_URL, "pr": PR_URL},
        )

    def test_failure_posts_later_with_reason_not_detail(self):
        summary = {
            "exit_code": 1,
            "result": {"status": "BLOCKED_DETERMINISTIC", "detail": "secret repo text"},
        }
        self.run_one(summary)
        _agent, note, state, link = self.posts[-1]
        self.assertEqual((state, link), ("Later", ISSUE_URL))
        self.assertIn("BLOCKED_DETERMINISTIC", note)
        self.assertNotIn("secret repo text", note)

    def test_common_failure_limit_posts_max_common_failures(self):
        summary = {"exit_code": 1, "result": {"status": "BLOCKED_DETERMINISTIC"}}
        code, _status, _config = self.run_one(summary, count=4)
        self.assertEqual(code, 2)
        later = [p for p in self.posts if p[2] == "Later"]
        self.assertEqual(len(later), 3)
        self.assertIn("max_common_failures", later[-1][1])
        self.assertNotIn("max_common_failures", later[0][1])

    def test_spend_cap_posts_needs_okay(self):
        message = ""
        with mock.patch.object(runner, "refresh_spend", return_value=0.6):
            try:
                runner.check_budget(None, 0.5)
            except runner.BudgetStop as error:
                message = str(error)
        self.assertTrue(runner.is_spend_cap({"detail": "BudgetStop: " + message}))
        summary = {
            "exit_code": 1,
            "result": {"status": "RUNNER_ERROR", "detail": "BudgetStop: " + message},
        }
        self.run_one(summary)
        self.assertEqual(self.posts[-1][2], "Needs Okay")
        self.assertEqual(self.posts[-1][3], ISSUE_URL)

    def test_budget_exhausted_posts_needs_okay_and_counts_toward_escalation(self):
        summary = {
            "exit_code": 1,
            "result": {"status": "BUDGET_EXHAUSTED", "detail": "cap"},
        }
        code, status, _config = self.run_one(summary)
        self.assertEqual(code, 0)
        self.assertEqual(self.posts[-1][2], "Needs Okay")
        self.assertIn("Needs Okay: BUDGET_EXHAUSTED, queue run", self.posts[-1][1])
        self.assertIn("BUDGET_EXHAUSTED", runner.COUNTED_STATUSES)
        self.assertEqual(status["tier_failures"], {"owner/repo#7": 1})
        self.assertEqual(status["completed"], ["owner/repo#7"])

    def test_budget_unsplittable_posts_needs_okay(self):
        summary = {"exit_code": 1, "result": {"status": "BUDGET_UNSPLITTABLE"}}
        self.run_one(summary)
        self.assertEqual(self.posts[-1][2], "Needs Okay")
        self.assertIn("Needs Okay: BUDGET_UNSPLITTABLE, queue run", self.posts[-1][1])

    def test_budget_exhausted_bumps_tier_failures(self):
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            state.mkdir()
            runner.write_json(
                state / "status.json",
                {"completed": [], "tier_failures": {"owner/repo#7": 1}},
            )
            summary = {"exit_code": 1, "result": {"status": "BUDGET_EXHAUSTED"}}
            with self.spawn(state, summary):
                runner.supervise(config)
            status = runner.read_json(state / "status.json")
        self.assertEqual(status["tier_failures"], {"owner/repo#7": 2})

    def test_fresh_foreign_act_claim_skips_the_issue(self):
        self.rows = [self.foreign(hours=1)]
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            with mock.patch.object(runner.subprocess, "Popen") as popen:
                self.assertEqual(runner.supervise(config), 0)
            popen.assert_not_called()
            saved = runner.read_json(state / "status.json")
        self.assertEqual(self.posts, [])
        self.assertIn("BoomTick", saved["ledger_skipped"]["owner/repo#7"])
        self.assertNotIn("owner/repo#7", saved["completed"])

    def test_stale_foreign_claim_and_non_act_rows_proceed(self):
        for row in (
            self.foreign(hours=7),
            self.foreign(status="Later", hours=1),
            self.foreign(link="https://github.com/owner/repo/issues/8"),
        ):
            with self.subTest(row=row):
                self.posts.clear()
                self.rows = [row]
                self.run_one(self.SUCCESS)
                self.assertTrue(self.posts[0][1].startswith("claimed"))

    def test_stale_window_is_one_config_value(self):
        self.rows = [self.foreign(hours=7)]
        with tempfile.TemporaryDirectory() as root:
            config, _state = self.fixture(root)
            value = runner.read_json(config) | {"ledger_stale_hours": 8}
            runner.write_json(config, value)
            with mock.patch.object(runner.subprocess, "Popen") as popen:
                runner.supervise(config)
            popen.assert_not_called()

    def test_own_previous_claim_proceeds(self):
        self.rows = [self.foreign(source="S5-Queue", hours=0.1)]
        self.run_one(self.SUCCESS)
        self.assertTrue(self.posts[0][1].startswith("claimed"))

    def test_latest_row_for_key_wins(self):
        self.rows = [self.foreign(hours=1), self.foreign(status="Done", hours=0.5)]
        self.run_one(self.SUCCESS)
        self.assertTrue(self.posts)

    def test_reader_outage_fails_closed(self):
        def broken():
            raise ModuleNotFoundError("no google client")

        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            with (
                mock.patch.object(runner, "ledger_rows", broken),
                mock.patch.object(runner.subprocess, "Popen") as popen,
            ):
                self.assertEqual(runner.supervise(config), 0)
            popen.assert_not_called()
            saved = runner.read_json(state / "status.json")
        self.assertEqual(self.posts, [])
        self.assertIn(
            "ledger unreadable (ModuleNotFoundError)",
            saved["ledger_skipped"]["owner/repo#7"],
        )
        self.assertNotIn("owner/repo#7", saved["completed"])

    def test_stop_during_ledger_read_does_not_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)

            def slow_read():
                (state / "STOP").touch()
                return []

            with (
                mock.patch.object(runner, "ledger_rows", slow_read),
                mock.patch.object(runner.subprocess, "Popen") as popen,
            ):
                self.assertEqual(runner.supervise(config), 0)
            popen.assert_not_called()
            saved = runner.read_json(state / "status.json")
        self.assertTrue(saved["stopped"])
        self.assertEqual(saved["completed"], [])

    def test_ledger_timestamps_are_sheet_local_time(self):
        now = runner.datetime.datetime(
            2026, 11, 23, 12, 15, tzinfo=runner.datetime.timezone.utc
        )
        self.assertAlmostEqual(runner.ledger_age_hours("11/23/2026 3:15:00", now), 1.0)

    def test_post_failure_keeps_run_green_retries_once_then_drops(self):
        self.fail_posts = 3
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            with self.spawn(state, self.SUCCESS):
                self.assertEqual(runner.supervise(config), 0)
            saved = runner.read_json(state / "status.json")
            self.assertEqual(self.posts, [])
            self.assertEqual(len(saved["ledger_pending"]), 2)
            self.assertEqual(self.attempts, 2)
            self.assertNotIn(SECRET, json.dumps(saved["ledger_pending"]))
            self.fail_posts = 1
            runner.supervise(config)
            saved = runner.read_json(state / "status.json")
            self.assertEqual(self.attempts, 3)
            self.assertEqual([p[2] for p in self.posts], ["Act"])
            self.assertNotIn("ledger_pending", saved)
            runner.supervise(config)
            self.assertEqual(self.attempts, 3)
            self.assertEqual(len(self.posts), 1)

    def test_merge_confirm_posts_done_once_at_next_run(self):
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            with self.spawn(state, self.SUCCESS):
                runner.supervise(config)
            merged = json.dumps(
                {"state": "MERGED", "mergeCommit": {"oid": "abcdef123456"}}
            )
            with mock.patch.object(
                runner.subprocess, "check_output", return_value=merged
            ):
                runner.supervise(config)
                runner.supervise(config)
            done = [p for p in self.posts if p[2] == "Done"]
            self.assertEqual(len(done), 1)
            self.assertEqual(done[0][3], ISSUE_URL)
            self.assertEqual(done[0][1], f"merged abcdef1 {PR_URL}")
            self.assertEqual(
                runner.read_json(state / "status.json")["ledger_open_prs"], {}
            )

    def test_open_pr_stays_tracked_until_merged(self):
        with tempfile.TemporaryDirectory() as root:
            config, state = self.fixture(root)
            with self.spawn(state, self.SUCCESS):
                runner.supervise(config)
            runner.supervise(config)
            self.assertFalse([p for p in self.posts if p[2] == "Done"])
            self.assertIn(
                "owner/repo#7",
                runner.read_json(state / "status.json")["ledger_open_prs"],
            )

    def test_notes_never_carry_secrets_or_free_text(self):
        failing = {
            "exit_code": 1,
            "result": {
                "status": "RUNNER_ERROR",
                "detail": SECRET,
                "submit_url": SECRET,
            },
        }
        weird = {"exit_code": 1, "result": {"status": SECRET + " bad status"}}
        for summary in (self.SUCCESS, failing, weird):
            self.run_one(summary)
        self.assertTrue(self.posts)
        pattern = (
            r"^(claimed by queue run [0-9a-f]{12}"
            r"|PR https://github\.com/[\w.-]+/[\w.-]+/pull/\d+ opened, queue run [0-9a-f]{12}, "
            r"cost \$[\d.]+ \((upper_bound|reported)\), tests pass"
            r"|Later: [A-Z][A-Z0-9_]+, queue run [0-9a-f]{12})$"
        )
        for _agent, note, _status, link in self.posts:
            self.assertRegex(note, pattern)
            self.assertNotIn(SECRET, note + link)
            self.assertNotIn("API_TOKEN", note)
        self.assertEqual(sorted({p[3] for p in self.posts}), [ISSUE_URL])


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for module in (ledger, runs):
            patch = mock.patch.object(module, "LEDGER", str(self.root / "ledger.jsonl"))
            patch.start()
            self.addCleanup(patch.stop)
        patch = mock.patch.object(ledger, "state_dir", return_value=str(self.root))
        patch.start()
        self.addCleanup(patch.stop)
        self.scope = twt_receipts.ReceiptRun("queue-port", "http://localhost/v1")

    def bound(self, trace, amount=0.4):
        return {
            "trace_id": trace,
            "cost_usd": None,
            "cost_complete": False,
            "cost_basis": "upper_bound",
            "cost_upper_bound_usd": amount,
            "http_status": 502,
        }

    def record(self, ids):
        ledger.record(
            "queue.py",
            "default",
            "queue-port",
            [],
            0,
            provider="twt",
            usage={"twt_trace_ids": ids},
            extra={"llm_call": True},
        )

    def rows(self):
        path = Path(ledger.LEDGER)
        return (
            [json.loads(line) for line in path.read_text().splitlines()]
            if path.exists()
            else []
        )

    def test_empty_budget_and_coverage(self):
        self.assertEqual(runner.check_budget(None, 0.5), 0)
        self.assertTrue(runner.coverage(self.root)["reconciled"])

    def test_startup_recovery_uses_canonical_settlement_for_bounds_and_unknowns(self):
        self.scope.path = self.root / "state/receipts/run.jsonl"
        trace = self.scope.request()
        runner.write_json(self.root / "job.json", {"environment": {}})
        for bound in (None, 0.4):
            with (
                self.subTest(bound=bound),
                mock.patch.dict(os.environ, {}),
                mock.patch.object(
                    twt_receipts, "fetch", return_value=[self.bound(trace, bound)]
                ),
            ):
                self.assertEqual(runner.recover(self.root), 2 if bound is None else 0)
                self.assertEqual(
                    runner.read_json(self.root / "recovery.json")["reconciled"],
                    bound is not None,
                )

    def test_settled_recovery_preserves_snapshots_without_gateway_access(self):
        self.scope.path = self.root / "state/receipts/run.jsonl"
        trace = self.scope.request()
        receipt = {"trace_id": trace, "cost_complete": True, "cost_usd": 0.1}
        self.scope.reconcile(lambda *_: [receipt])
        runner.write_json(self.root / "job.json", {"environment": {}})
        with (
            mock.patch.dict(os.environ, {}),
            mock.patch.object(
                twt_receipts, "fetch", side_effect=OSError("gateway offline")
            ) as fetch,
        ):
            self.assertEqual(runner.recover(self.root), 0)
            fetch.assert_not_called()
        self.assertEqual(
            runner.read_json(self.scope.path.with_suffix(".settlement.json"))[
                "receipts"
            ],
            [receipt],
        )

    def test_partial_settlement_snapshots_merge_with_later_receipts(self):
        first, second = self.scope.request(), self.scope.request()
        one = {"trace_id": first, "cost_complete": True, "cost_usd": 0.1}
        two = {"trace_id": second, "cost_complete": True, "cost_usd": 0.2}
        self.scope.reconcile(lambda *_: [one], timeout=0)
        self.scope.reconcile(lambda *_: [two], timeout=0)
        snapshots = runner.read_json(self.scope.path.with_suffix(".settlement.json"))[
            "receipts"
        ]
        self.assertEqual({row["trace_id"] for row in snapshots}, {first, second})

    def check_actual_charge_supersedes_reservation_after_snapshot_crash(self, count):
        self.scope.path = self.root / "state/receipts/run.jsonl"
        traces = [self.scope.request() for _ in range(count)]
        self.record(traces)
        self.scope.reconcile(lambda *_: [self.bound(trace, 0.4) for trace in traces])
        path = self.scope.path.with_suffix(".settlement.json")
        old_snapshot = runner.read_json(path)
        actual = [
            {"trace_id": trace, "cost_complete": True, "cost_usd": 0.1 * (index + 1)}
            for index, trace in enumerate(traces)
        ]
        replace = os.replace

        def crash(source, target):
            if Path(target) == path:
                raise OSError("crash after actual ledger commit")
            return replace(source, target)

        self.scope.settled = False
        with mock.patch.object(twt_receipts.os, "replace", side_effect=crash):
            with self.assertRaisesRegex(OSError, "actual ledger commit"):
                self.scope.reconcile(lambda *_: actual)
        total = sum(row["cost_usd"] for row in actual)
        self.assertAlmostEqual(self.rows()[0]["cost_usd"], total)
        self.assertAlmostEqual(runner.coverage(self.root / "state")["cost_usd"], total)
        runner.write_json(path, old_snapshot)
        runner.write_json(self.root / "job.json", {"environment": {}})
        with (
            mock.patch.dict(os.environ, {}),
            mock.patch.object(
                twt_receipts, "fetch", side_effect=OSError("gateway offline")
            ) as fetch,
        ):
            self.assertEqual(runner.recover(self.root), 0)
            fetch.assert_not_called()
        self.assertTrue(
            all(row["cost_complete"] for row in runner.read_json(path)["receipts"])
        )
        self.assertAlmostEqual(
            sum(row["cost_usd"] for row in runner.read_json(path)["receipts"]), total
        )

    def test_actual_ledger_charge_repairs_stale_reservation_snapshot(self):
        self.check_actual_charge_supersedes_reservation_after_snapshot_crash(1)

    def test_aggregate_actual_ledger_charge_repairs_stale_reservation_snapshots(self):
        self.check_actual_charge_supersedes_reservation_after_snapshot_crash(2)

    def test_legacy_actual_aggregate_refetches_instead_of_inventing_allocation(self):
        traces = [self.scope.request(), self.scope.request()]
        self.record(traces)
        self.scope.reconcile(lambda *_: [self.bound(trace) for trace in traces])
        actual = [
            {"trace_id": trace, "cost_complete": True, "cost_usd": 0.1 * (index + 1)}
            for index, trace in enumerate(traces)
        ]
        twt_receipts.merge_receipts(
            set(traces),
            {row["trace_id"]: row for row in actual},
            self.scope.run_id,
            self.scope.started,
        )
        (legacy,) = self.rows()
        legacy["usage"].pop("twt_receipt_costs")
        Path(ledger.LEDGER).write_text(json.dumps(legacy) + "\n")
        with mock.patch.object(
            twt_receipts, "fetch", side_effect=OSError("gateway offline")
        ):
            with self.assertRaises(OSError):
                runner.coverage(self.root)
        with mock.patch.object(twt_receipts, "fetch", return_value=actual):
            self.assertAlmostEqual(runner.coverage(self.root)["cost_usd"], 0.3)

    def test_recovery_refuses_a_still_running_worker(self):
        with (self.root / "worker.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with mock.patch(
                "sys.argv", ["queue_runner.py", "--reconcile", str(self.root)]
            ):
                with self.assertRaises(BlockingIOError):
                    runner.main()

    def test_refresh_existing_owner_before_unknown_check_and_enforce_cap(self):
        first = self.scope.request()
        self.record([first])
        self.assertEqual(runs.total_metered_spend()["unknown"], 1)
        with mock.patch.object(twt_receipts, "fetch", return_value=[self.bound(first)]):
            self.assertEqual(runner.check_budget(self.scope, 0.5), 0.4)
        self.assertEqual(self.rows()[0]["cost_basis"], "upper_bound")
        second = self.scope.request()
        receipts = [self.bound(first), self.bound(second, 0.1)]
        with mock.patch.object(twt_receipts, "fetch", return_value=receipts):
            with self.assertRaises(runner.BudgetStop):
                runner.check_budget(self.scope, 0.5)
        self.assertEqual(len(self.rows()), 1)

    def test_model_construction_refreshes_bounds_before_its_cap_check(self):
        trace = self.scope.request()
        self.record([trace])
        token = twt_receipts.current.set(self.scope)
        self.addCleanup(twt_receipts.current.reset, token)
        for amount, error in (
            (None, runner.SafetyStop),
            (0.4, None),
            (0.5, runner.BudgetStop),
        ):
            with (
                self.subTest(amount=amount),
                mock.patch.dict(os.environ, {"NEBIUS_SPEND_CAP_USD": "0.5"}),
                mock.patch.object(model_router, "api_key", return_value="twt"),
                mock.patch.object(
                    model_router, "_remote_spec", model_router._remote_spec
                ),
                mock.patch.object(
                    model_router._BoundedHistory,
                    "_bounded_kwargs",
                    model_router._BoundedHistory._bounded_kwargs,
                ),
                mock.patch.object(
                    twt_receipts, "fetch", return_value=[self.bound(trace, amount)]
                ),
            ):
                runner.install_budget_guard(0.5, self.root / "issue", [])
                if error:
                    with self.assertRaises(error):
                        model_router._remote_spec("twt", "default", self.scope.base_url)
                else:
                    self.assertEqual(
                        model_router._remote_spec(
                            "twt", "default", self.scope.base_url
                        )["provider"],
                        "twt",
                    )

    def test_live_unowned_receipts_do_not_create_or_double_count_owners(self):
        first, second = self.scope.request(), self.scope.request()
        receipts = [self.bound(first, 0.1), self.bound(second, 0.2)]
        with mock.patch.object(twt_receipts, "fetch", return_value=receipts):
            self.assertAlmostEqual(runner.check_budget(self.scope, 0.5), 0.3)
            self.assertEqual(self.rows(), [])
            self.record([first, second])
            self.assertAlmostEqual(runner.check_budget(self.scope, 0.5), 0.3)
        self.assertEqual(len(self.rows()), 1)
        self.scope.reconcile(lambda *_: receipts)
        self.assertTrue(runner.coverage(self.root)["reconciled"])
        self.assertEqual(runner.coverage(self.root)["cost_basis"], "upper_bound")

    def test_unbounded_or_missing_receipts_and_gateway_failure_halt(self):
        trace = self.scope.request()
        for response in (
            [],
            [{"trace_id": trace, "cost_complete": False}],
            [self.bound(trace, None)],
        ):
            with (
                self.subTest(response=response),
                mock.patch.object(twt_receipts, "fetch", return_value=response),
            ):
                with self.assertRaises(runner.SafetyStop):
                    runner.check_budget(self.scope, 0.5)
        with mock.patch.object(
            twt_receipts, "fetch", side_effect=OSError("gateway unavailable")
        ):
            with self.assertRaises(OSError):
                runner.check_budget(self.scope, 0.5)

    def test_exact_guard_refresh_still_has_automatic_settlement_coverage(self):
        trace = self.scope.request()
        self.record([trace])
        receipt = {"trace_id": trace, "cost_complete": True, "cost_usd": 0.1}
        with mock.patch.object(twt_receipts, "fetch", return_value=[receipt]):
            self.assertEqual(runner.check_budget(self.scope, 0.5), 0.1)
            self.scope.reconcile()
            self.assertTrue(runner.coverage(self.root)["reconciled"])
        self.scope.path.with_suffix(".settlement.json").unlink()
        with self.assertRaisesRegex(runner.SafetyStop, "no automatic settlement"):
            runner.coverage(self.root)

    def test_coverage_rejects_unknown_costs_and_ownership_or_cost_mismatches(self):
        trace = self.scope.request()
        self.scope.reconcile(lambda *_: [self.bound(trace)])
        self.assertTrue(runner.coverage(self.root)["reconciled"])
        (row,) = self.rows()
        for changes, duplicate in (
            ({"cost_usd": 0.7}, False),
            ({"cost_usd": None, "cost_upper_bound_usd": None}, False),
            ({}, True),
        ):
            changed = {**row, **changes}
            Path(ledger.LEDGER).write_text(
                json.dumps(changed)
                + "\n"
                + (json.dumps(changed) + "\n" if duplicate else "")
            )
            with (
                self.subTest(changes=changes, duplicate=duplicate),
                self.assertRaises(runner.SafetyStop),
            ):
                runner.coverage(self.root)


class BudgetReportTests(unittest.TestCase):
    def job(self, root, name, calls=None, verified=False, exhausted=False):
        directory = Path(root) / name
        directory.mkdir()
        budget = None
        if calls is not None:
            budget = {
                "parent": {
                    "calls_used": calls,
                    "max_calls": 60,
                    "exhausted": exhausted,
                    "verified": verified,
                },
                "sub_briefs": [],
            }
        runner.write_json(directory / "summary.json", {"budget": budget})
        return directory

    def test_report_buckets_rates_and_exhaustion(self):
        with tempfile.TemporaryDirectory() as root:
            self.job(root, "a", 3, verified=True)
            self.job(root, "b", 8, verified=True)
            self.job(root, "c", 19, verified=False)
            self.job(root, "d", 30, verified=True)
            self.job(root, "e", 60, exhausted=True)
            self.job(root, "f", None)
            self.job(root, "z", 0)
            (Path(root) / "g").mkdir()
            report = runner.budget_report(root)
        lines = report.splitlines()
        self.assertEqual(lines[1].split(), ["1-5", "1", "1", "100%"])
        self.assertEqual(lines[2].split(), ["6-10", "1", "1", "100%"])
        self.assertEqual(lines[3].split(), ["11-20", "1", "0", "0%"])
        self.assertEqual(lines[4].split(), ["21-40", "1", "1", "100%"])
        self.assertEqual(lines[5].split(), [">40", "1", "0", "0%"])
        self.assertIn("runs=5 verified=3 calls_per_verified=13.7", report)
        self.assertIn("budget_exhaustion_rate=20% (1/5)", report)

    def test_report_empty_input(self):
        with tempfile.TemporaryDirectory() as root:
            report = runner.budget_report(root)
        self.assertIn("runs=0 verified=0 calls_per_verified=n/a", report)
        self.assertIn("budget_exhaustion_rate=n/a (0/0)", report)

    def test_budget_report_flag_reads_state_dir(self):
        with tempfile.TemporaryDirectory() as root:
            state = Path(root) / "state"
            state.mkdir()
            self.job(state, "a", 4, verified=True)
            runner.write_json(Path(root) / "c.json", {"state_dir": str(state)})
            out = io.StringIO()
            with (
                mock.patch.object(
                    sys, "argv", ["q", "--config", f"{root}/c.json", "--budget-report"]
                ),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(runner.main(), 0)
        self.assertIn("runs=1 verified=1 calls_per_verified=4.0", out.getvalue())


class CapEscalationTests(unittest.TestCase):
    def test_counts_exhausted_only(self):
        def summary(status, **extra):
            return {
                "exit_code": 1,
                "halt_queue": False,
                "result": {"status": status},
                **extra,
            }

        self.assertTrue(runner.counts_toward_escalation(summary("BUDGET_EXHAUSTED")))
        for case in (
            summary("BUDGET_UNSPLITTABLE"),
            summary("BUDGET_EXHAUSTED", halt_queue=True),
            summary("BUDGET_EXHAUSTED", budget_stop=True),
            summary("PAUSED_BY_USER"),
            {**summary("BUDGET_EXHAUSTED"), "exit_code": 0},
        ):
            self.assertFalse(runner.counts_toward_escalation(case), case)


class StrongRoutingTests(unittest.TestCase):
    fixture = RunnerTests.fixture

    def run_one(self, root, summary, labels=("ready",), seed=None, config_extra=None):
        """Dispatch issue 7 once with a stubbed worker; return (status, job, out)."""
        config, state = self.fixture(root)
        if config_extra:
            runner.write_json(config, {**runner.read_json(config), **config_extra})
        if seed:
            state.mkdir(exist_ok=True)
            runner.write_json(state / "status.json", seed)
        # The dispatch gate (#1589) requires a ready label on refresh; routing
        # behavior is orthogonal, so every refresh here carries ready.
        names = dict.fromkeys([*labels, "ready"])
        payload = {
            "state": "open",
            "title": "Fix",
            "body": "scope",
            "labels": [{"name": name} for name in names],
        }

        def spawn(command, **_kwargs):
            directory = state / Path(command[-1]).name
            process = mock.Mock(returncode=summary["exit_code"])
            process.communicate.side_effect = lambda _: runner.write_json(
                directory / "summary.json",
                {
                    "halt_queue": False,
                    "reconciliation": {"reconciled": True},
                    **summary,
                },
            )
            return process

        out = io.StringIO()
        with (
            mock.patch.object(ledger_post, "post_entry", return_value=200),
            mock.patch.object(runner, "ledger_rows", return_value=[]),
            mock.patch.object(runner.signal, "signal"),
            mock.patch.object(
                runner.subprocess, "check_output", return_value=json.dumps(payload)
            ),
            mock.patch.object(runner.subprocess, "Popen", side_effect=spawn),
            mock.patch.object(runner, "ledger_rows", return_value=[]),
            contextlib.redirect_stdout(out),
        ):
            runner.supervise(config)
        return (
            runner.read_json(state / "status.json"),
            runner.read_json(state / "owner_repo-7" / "job.json"),
            out.getvalue(),
        )

    def failed(self, status="BLOCKED_DETERMINISTIC", **extra):
        return {"exit_code": 1, "result": {"status": status, "detail": "x"}, **extra}

    def test_label_routes_strong_from_the_first_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            status, job, out = self.run_one(
                root, self.failed(), labels=("bug", "model:strong")
            )
        self.assertEqual(
            (job["model_tier"], job["tier_reason"]),
            ("strong", "label:model:strong"),
        )
        self.assertIn("START owner/repo#7 tier=strong reason=label:model:strong", out)
        self.assertEqual(
            status["attempts"]["owner/repo#7"][0]["tier"],
            "strong",
        )

    def test_unlabeled_first_attempt_stays_on_the_default_route(self):
        with tempfile.TemporaryDirectory() as root:
            status, job, out = self.run_one(root, self.failed(), labels=("bug",))
        self.assertEqual(
            (job["model_tier"], job["tier_reason"]), ("default", "default")
        )
        self.assertIn("START owner/repo#7 tier=default reason=default", out)
        self.assertEqual(status["tier_failures"], {"owner/repo#7": 1})

    def test_default_threshold_is_two_failures(self):
        for failures, tier in ((1, "default"), (2, "strong"), (5, "strong")):
            with self.subTest(failures=failures), tempfile.TemporaryDirectory() as root:
                _status, job, _out = self.run_one(
                    root,
                    self.failed(),
                    seed={"completed": [], "tier_failures": {"owner/repo#7": failures}},
                )
                self.assertEqual(job["model_tier"], tier)
                if tier == "strong":
                    self.assertEqual(job["tier_reason"], f"auto-escalation:{failures}")

    def test_capped_run_escalates_to_strong_once_then_stops(self):
        capped = self.failed("BUDGET_EXHAUSTED")
        one = {"strong_after_failures": 1}
        with tempfile.TemporaryDirectory() as root:
            status, job, _out = self.run_one(root, capped, config_extra=one)
            self.assertEqual(job["model_tier"], "default")
            self.assertEqual(status["tier_failures"], {"owner/repo#7": 1})
        for expected_failures in (2, 3):
            with tempfile.TemporaryDirectory() as root:
                seed = {**status, "completed": []}
                status, job, _out = self.run_one(
                    root, capped, seed=seed, config_extra=one
                )
            self.assertEqual(
                (job["model_tier"], job["tier_reason"]),
                ("strong", f"auto-escalation:{expected_failures - 1}"),
            )
            self.assertEqual(
                status["tier_failures"], {"owner/repo#7": expected_failures}
            )
        tiers = [a["tier"] for a in status["attempts"]["owner/repo#7"]]
        self.assertEqual(tiers, ["default", "strong", "strong"])
        # One supervise pass dispatches one worker: nothing retries by itself.
        self.assertEqual(status["completed"], ["owner/repo#7"])

    def test_threshold_is_configurable_and_validated(self):
        seed = {"completed": [], "tier_failures": {"owner/repo#7": 1}}
        with tempfile.TemporaryDirectory() as root:
            _s, job, _o = self.run_one(
                root,
                self.failed(),
                seed=seed,
                config_extra={"strong_after_failures": 1},
            )
            self.assertEqual(job["model_tier"], "strong")
        for bad in (0, True, "2"):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(ValueError, "strong_after_failures"):
                    self.run_one(
                        root, self.failed(), config_extra={"strong_after_failures": bad}
                    )

    def test_counter_increments_on_coder_failures_and_success_resets_it(self):
        key = "owner/repo#7"
        for status_name in ("BLOCKED_DETERMINISTIC", "CHANGES_REQUESTED"):
            with self.subTest(status_name), tempfile.TemporaryDirectory() as root:
                status, _j, _o = self.run_one(
                    root,
                    self.failed(status_name),
                    seed={"completed": [], "tier_failures": {key: 1}},
                )
                self.assertEqual(status["tier_failures"][key], 2)
        with tempfile.TemporaryDirectory() as root:
            status, _j, _o = self.run_one(
                root,
                {"exit_code": 0, "result": {"status": "APPROVED"}},
                seed={"completed": [], "tier_failures": {key: 2}},
            )
        self.assertNotIn(key, status["tier_failures"])

    def test_non_coder_outcomes_do_not_count(self):
        key = "owner/repo#7"
        cases = [
            self.failed("CONFIG_ERROR"),
            self.failed("STRONG_BUDGET_INSUFFICIENT"),
            self.failed("PAUSED_BY_USER"),
            self.failed("RUNNER_ERROR"),
            self.failed(budget_stop=True),
        ]
        for summary in cases:
            with (
                self.subTest(summary=summary),
                tempfile.TemporaryDirectory() as root,
                block_google_docs_requests(),
            ):
                status, _j, _o = self.run_one(
                    root,
                    summary,
                    seed={"completed": [], "tier_failures": {key: 1}},
                )
                self.assertEqual(status["tier_failures"][key], 1)

    def test_summary_status_and_job_record_tier_and_reason(self):
        with tempfile.TemporaryDirectory() as root:
            status, job, _o = self.run_one(
                root,
                self.failed(tier="strong", tier_reason="label:model:strong"),
                labels=("model:strong",),
            )
            entry = status["attempts"]["owner/repo#7"][-1]
        self.assertEqual(entry["reason"], "label:model:strong")
        self.assertEqual(status["last"]["tier"], "strong")
        self.assertEqual(job["model_tier"], "strong")

    def test_strong_start_requires_model_and_budget(self):
        fatal = []
        with mock.patch.dict(os.environ, {"CODER_STRONG_MODEL": " "}):
            with self.assertRaises(runner.StrongRefused) as caught:
                runner.check_strong_start(0.5, fatal)
        self.assertEqual(caught.exception.status, "CONFIG_ERROR")
        self.assertIn("CODER_STRONG_MODEL", fatal[0])
        with (
            mock.patch.dict(os.environ, {"CODER_STRONG_MODEL": "big"}),
            mock.patch.object(runner, "refresh_spend", return_value=0.47),
        ):
            with self.assertRaises(runner.StrongRefused) as caught:
                runner.check_strong_start(0.5, [])
            self.assertEqual(caught.exception.status, "STRONG_BUDGET_INSUFFICIENT")
        with (
            mock.patch.dict(os.environ, {"CODER_STRONG_MODEL": "big"}),
            mock.patch.object(runner, "refresh_spend", return_value=0.5),
        ):
            with self.assertRaises(runner.StrongRefused) as caught:
                runner.check_strong_start(0.5, [])
            self.assertEqual(caught.exception.status, "STRONG_BUDGET_INSUFFICIENT")
        with (
            mock.patch.dict(os.environ, {"CODER_STRONG_MODEL": "big"}),
            mock.patch.object(runner, "refresh_spend", return_value=0.1),
        ):
            self.assertEqual(runner.check_strong_start(0.5, []), "big")

    def worker_job(self, root, tier, environment):
        directory = Path(root) / "job"
        directory.mkdir()
        runner.write_json(
            directory / "job.json",
            {
                "issue": {"repo": "owner/repo", "number": 7, "title": "t", "body": ""},
                "cap_usd": 0.5,
                "checkout": str(root),
                "environment": environment,
                "model_tier": tier,
                "tier_reason": "label:model:strong",
            },
        )
        return directory

    def run_worker(self, root, tier, environment, spend=0.0):
        directory = self.worker_job(root, tier, environment)
        cli = mock.Mock(main=mock.Mock(return_value=0))
        original = os.getcwd()
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(runner, "install_budget_guard"),
            mock.patch.object(runner, "refresh_spend", return_value=spend),
            mock.patch.object(runner, "coverage", return_value={"reconciled": True}),
            mock.patch.dict(sys.modules, {"pipeline": mock.MagicMock()}),
            mock.patch.object(shared, "cli", cli, create=True),
            mock.patch.object(shared, "runs", mock.MagicMock(), create=True),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            os.environ.pop("CODER_STRONG_MODEL", None)
            try:
                code = runner.worker(directory)
                seen = os.environ.get("S5_START_MODEL_TIER")
                cli_calls = cli.main.call_count
            finally:
                os.chdir(original)
        return code, runner.read_json(directory / "summary.json"), seen, cli_calls

    def test_worker_strong_exports_tier_and_runs_pipeline(self):
        with tempfile.TemporaryDirectory() as root:
            code, summary, seen, calls = self.run_worker(
                root, "strong", {"CODER_STRONG_MODEL": "big"}
            )
        self.assertEqual((code, seen, calls), (0, "strong", 1))
        self.assertEqual(
            (summary["tier"], summary["tier_reason"], summary["strong_model"]),
            ("strong", "label:model:strong", "big"),
        )

    def test_worker_default_exports_no_strong_tier(self):
        with tempfile.TemporaryDirectory() as root:
            _c, summary, seen, _n = self.run_worker(root, "default", {})
        self.assertIsNone(seen)
        self.assertEqual(summary["tier"], "default")

    def test_worker_without_strong_model_fails_before_the_pipeline_and_halts(self):
        with tempfile.TemporaryDirectory() as root:
            code, summary, _s, calls = self.run_worker(root, "strong", {})
        self.assertEqual((code, calls), (2, 0))
        self.assertEqual(summary["result"]["status"], "CONFIG_ERROR")
        self.assertTrue(summary["halt_queue"])
        self.assertIn("CODER_STRONG_MODEL", summary["halt_reasons"][0])

    def test_worker_with_insufficient_budget_does_not_start_strong(self):
        with tempfile.TemporaryDirectory() as root:
            code, summary, _s, calls = self.run_worker(
                root, "strong", {"CODER_STRONG_MODEL": "big"}, spend=0.48
            )
        self.assertEqual((code, calls), (1, 0))
        self.assertEqual(summary["result"]["status"], "STRONG_BUDGET_INSUFFICIENT")
        self.assertFalse(summary["halt_queue"])


if __name__ == "__main__":
    unittest.main()
