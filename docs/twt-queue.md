# Durable TwT issue queue

`scripts/queue_runner.py` supervises the existing
`deep-agent/cli.py run-pipeline --submit-pr --keep-worktree` path, one issue at a
time. It requires the merged bounded-receipt contract in `twt_receipts.py` and a
TwT gateway that emits `cost_basis: upper_bound`. The supervisor uses Python's
standard library and `gh`; the worker needs the normal deep-agent dependencies,
canonical `td`, GitHub authentication and TwT access in its own runtime.

## Configuration

Copy `scripts/config/twt-queue.example.json` to an operator-owned JSON configuration
outside the checkout. Its `_comment` records why the operator holds are global.
Replace these
example absolute paths with persistent paths in your deployment:

```json
{
  "issues_file": "/absolute/remote-server/scripts/config/twt-queue-issues.json",
  "state_dir": "/absolute/queue-state",
  "runtime_state_dir": "/absolute/queue-state",
  "pipeline_checkout": "/absolute/remote-server",
  "repo_paths": {"arii/remote-server": "/absolute/remote-server"},
  "runtime_command": [],
  "worker_python": "/absolute/remote-server/deep-agent/.venv/bin/python",
  "cap_usd": 0.5,
  "max_common_failures": 3,
  "inter_issue_delay_seconds": 60,
  "environment": {"TWT_BASE_URL": "http://127.0.0.1:8000/v1"}
}
```

Run `python3 scripts/queue_runner.py --config /absolute/queue-config.json`, or set
`QUEUE_CONFIG` instead of passing `--config`. No runtime path is inferred from a
Devin session. `pipeline_checkout` and `repo_paths` are paths **inside the worker**;
`state_dir` is the supervisor's path and `runtime_state_dir` is the worker's path
to the **same persistent directory**. The worker source travels over stdin. The
launcher declares the configured checkout and its `deep-agent` directory in
`PYTHONPATH` before starting `worker_python`; no module mutates `sys.path`.

For a container, `runtime_command` can be
`["docker", "exec", "-i", "CONTAINER"]`, with `worker_python` set to the
interpreter path inside that container. This is an argv prefix, **not** the old
external runner's complete Python command; omit its trailing Python/`-` when migrating.
The checkout and state directory must be mounted there. Optional `gh_command`
(default `["gh"]`) configures the supervisor's GitHub CLI. Put only non-secret
runtime settings in `environment`; provider credentials belong in the TwT service,
not manifests, job files or this configuration. Set `TWT_UDS` there if required.

The committed manifest is a triaged starting snapshot, not a live issue crawler.
Copy and review it before operating a queue. Entries require `repo` and `number`;
`group: "code-reaper"` sorts ahead of other entries, oldest `created_at` first.
Ties retain manifest order. Supply timestamps to ensure chronological ordering.
Every issue is refreshed through GitHub before dispatch; closed issues are skipped.
Explicit `skip_reason`, `merged: true`, `fixed: true`, or `requires_human: true`
also skip an entry, including fixed issues that remain open. Holds #2/#23/#26
cannot dispatch in any repo: the operator did not name a repository for them.
Human-decision #1377 is held only in `arii/remote-server`; the same number in
another repository is not held. remote-server #1359 is always skipped because
its change merged via #1381. The snapshot preserves other recorded human/operator
holds; update the manifest deliberately rather than guessing whether an issue is fixed.

## Accounting and recovery

- Every issue has isolated `job.json`, `run.log`, `stages.json`, `ledger.jsonl`,
  receipt journals, automatic settlement, pipeline result and `summary.json`.
- The guard refreshes existing ledger owners from fresh receipts **before**
  checking unknown spend, at model construction as well as dispatch. Live receipts
  not yet owned by a completed turn count
  toward the budget without creating premature or duplicate owner rows.
- An incomplete, valid `upper_bound` receipt reserves that amount. It counts
  against the cap and passes reconciliation without being called an actual bill.
  Missing hosted receipts and unbounded or malformed costs still halt the queue.
- The cap is positive and at most **$0.50 per issue**. Checks happen before each
  subsequent model call: the last dispatched call can cross the cap. This is a
  recorded-spend gate, not a prediction of the next call's eventual charge.
- Shutdown requires automatic settlement, exactly one owner per receipt, and
  matching ledger/receipt totals. A worker exit inconsistent with its summary halts.
- Known-cost ordinary failures continue to the next issue; three consecutive
  failures with identical status/detail stop by default (`max_common_failures`).

`status.json` persists completed/skipped issues, the current issue and halt reason.
A global file lock prevents concurrent supervisors. Restarting skips completed
issues but never blindly retries an existing unfinished issue directory. Startup
first reconciles the saved `current` issue in its configured runtime, even if it
has since closed or disappeared from the manifest. An active worker holds an
issue lock, so recovery cannot race its writes. Failed recovery halts before any
GitHub lookup or new dispatch; successful recovery records the attempt as completed
without rerunning its pipeline. An existing explicit halt still requires operator
clearance. Settlement snapshots are merged and replaced atomically. Fully reported
saved receipts can reconcile offline; provisional reservations remain eligible for
later actual charges without discarding their existing snapshots. An actual,
reconciled ledger charge supersedes a stale reservation snapshot. Aggregate actual
rows retain per-trace costs in ledger usage so that recovery can repair snapshots
offline after an interrupted replacement. Legacy aggregate rows without that
breakdown refetch receipts rather than inventing a per-trace allocation. Stopping
creates a persistent `STOP` sentinel, also checked before model dispatch. After a
halt, inspect the issue, journal/settlement and ledger using supported receipt
recovery tooling. Successful reconciliation does not clear a saved worker's
`halt_queue` or an explicit supervisor halt. After inspecting and resolving the
blocker, an operator can acknowledge the saved halt and resume with
`python3 scripts/queue_runner.py --config /absolute/queue-config.json --clear-halt`.
This first reconciles the current issue, preserves the acknowledged halt reasons,
then clears the halt and `STOP`. Failed reconciliation cannot be overridden by
this flag. Do not delete an unfinished directory to force a retry or
edit ledger costs by hand. Keep state on persistent storage, not `/tmp`.

## User service

Install `scripts/twt-queue.service` in `~/.config/systemd/user/`. Create a mode-0600
`~/.config/twt-queue/environment` containing:

```text
QUEUE_RUNNER=/absolute/remote-server/scripts/queue_runner.py
QUEUE_CONFIG=/absolute/queue-config.json
```

Then run `systemctl --user daemon-reload` and
`systemctl --user start twt-queue.service`. Enable it only if the reviewed manifest
should start at login. Configure user lingering separately if needed. The service
deliberately has `Restart=no`; failures require inspection. Monitor
`journalctl --user -u twt-queue.service` and the persistent issue directories.

## Checks

With the existing deep-agent verification environment active:

```sh
PYTHONPATH=deep-agent:tests:. python -m unittest test_queue_runner test_twt_receipts
pnpm run lint
systemd-analyze --user verify scripts/twt-queue.service
```

Tests mock GitHub, model receipts and worker processes; they do not run inference,
open PRs, access S5 or change a live ledger.
