import json
import fcntl
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.shared import ledger
from backend.shared import model_router
from backend.shared import runs
from backend.shared import twt_receipts
from scripts import queue_runner as runner


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
        return_value=json.dumps({"state": "open", "title": "Fix"}),
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
        return_value=json.dumps({"state": "open", "title": "Fix", "body": "scope"}),
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
            with mock.patch.object(
                runner.subprocess, "Popen", return_value=self.retry_process(directory)
            ):
                runner.retry_issue(config, "owner/repo#7")
            dispatched = []

            def spawn(command, **_kwargs):
                name = Path(command[-1]).name
                dispatched.append(name)
                job = runner.read_json(state / name / "job.json")
                self.assertAlmostEqual(
                    job["cap_usd"], 0.4 if name.endswith("-7") else 0.5
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
            {"state": "open", "title": "Fix issue", "body": "scope"}
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
    @mock.patch.object(
        runner.subprocess,
        "check_output",
        return_value=json.dumps(
            {"state": "open", "title": "Fix issue", "body": "scope"}
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

    @mock.patch.object(runner.signal, "signal")
    @mock.patch.object(
        runner.subprocess,
        "check_output",
        return_value=json.dumps({"state": "open", "title": "Fix", "body": "scope"}),
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
        return_value=json.dumps({"state": "open", "title": "Fix", "body": "scope"}),
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


if __name__ == "__main__":
    unittest.main()
