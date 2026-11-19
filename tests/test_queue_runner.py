import json
import fcntl
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ledger
import model_router
import runs
import twt_receipts
from scripts import queue_runner as runner


class RunnerTests(unittest.TestCase):
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
            supervise.assert_called_once_with(Path("/config.json"))

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
            modules = checkout / "deep-agent"
            modules.mkdir(parents=True)
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
            )
            (modules / "pipeline.py").write_text(
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
