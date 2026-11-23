# Durable TwT issue queue

`scripts/queue_runner.py` supervises the existing
`python3 -m backend.shared.cli run-pipeline --submit-pr --keep-worktree` path, one issue at a
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
  "worker_python": "/opt/venvs/td/bin/python",
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

`worker_python` is the container interpreter `/opt/venvs/td/bin/python` (the `td`
venv that both Dockerfiles build). The old `deep-agent/.venv` no longer exists, so
a config that still names it fails at launch. Prerequisite: the interpreter must be
able to import `backend.shared` (for example `backend.shared.twt_receipts`). The
launcher runs `env PYTHONPATH=<pipeline_checkout>/deep-agent:<pipeline_checkout>
<worker_python> - <mode> <dir>`, so the checkout root is on `PYTHONPATH` and no
working-directory setting is needed; `pipeline_checkout` must be the repo root.
Check with `<runtime_command> env PYTHONPATH=<checkout> /opt/venvs/td/bin/python -c
"import backend.shared.twt_receipts"`. The $0.50 `cap_usd`, fail-closed
reconciliation and completed-issue state are unchanged by the interpreter change.

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

### Retry a transiently blocked issue

With the queue service idle, requeue one finished, failed issue (the plain service
start below assumes no STOP marker or saved halt):

```sh
python3 scripts/queue_runner.py --config /absolute/queue-config.json --retry 'arii/remote-server#1537'
systemctl --user start twt-queue.service
```

`--retry` is a state-only operator command: it never launches a pipeline. It takes
the supervisor lock, requires exactly one eligible manifest entry and a failed
summary without a saved worker halt, and runs canonical receipt reconciliation
before moving the old run directory to `state_dir/archive/ISSUE-TIMESTAMP`.
The runtime also moves a registered `.worktrees/issue-N` into the same repo's
`.worktrees/.queue-retry/ISSUE-TIMESTAMP`, renaming its branch to
`queue-retry/ISSUE-TIMESTAMP`. Dirty and untracked files survive. The archived
run's `worktree-archive.json` links the old path, preserved branch/HEAD and new
path. Both archive operations resume from durable intent after interruption;
the retry is not eligible until both complete. No worktree is deleted.
Logs, journals, ledger and summary are preserved after reconciliation (journal ledger paths
still describe the original run location). It removes only that issue from
`completed`, records the archive in `status.json`, and prioritizes its next attempt
ahead of the normal manifest order. No manifest edit is needed. A live supervisor,
an unfinished/current run, a successful run, or unresolved receipt costs prevent
retry. Only one retry can be pending at a time.

Prior reconciled spend, including reservations, reduces the next attempt's budget;
retry does not grant a fresh $0.50 allowance. Exhausted budgets cannot be retried.
Initial implementation keeps its 20-model/32-tool limit. If the TwT default
route exhausts the model limit, the coder makes at most one additional attempt
with a fresh 20-model/32-tool budget and checkpoint thread, preserving prior
edits. `CODER_FALLBACK_MODEL` selects its TwT model ID (default
`nebius/zai-org/GLM-5.3-Flash`); an empty value disables fallback. The explicit
model is sent without stage-specific provider maps, so it does not route back
through the free default waterfall. Both attempts share receipt accounting and
the remaining issue budget; TwT still enforces its configured $5 daily budget.
Repair, local-only execution and errors other than initial model-cap exhaustion
do not trigger this paid attempt.
The command does not clear STOP, explicit supervisor halts, or saved worker halts.
If shutdown left STOP, or a supervisor halt remains, explicitly acknowledge it
with the existing `--clear-halt` recovery procedure before resuming; a plain service
start stays stopped. The usual held/closed issue checks still apply. Do not remove a completed
entry or delete a run directory by hand to force a retry.

## Slop review gate

Each queue PR gets a hostile slop review (#1552) before it can reach review.
`scripts/slop_review.py` wraps `Pipeline.stage_submit` in `queue_runner.worker()`,
without editing `deep-agent/pipeline.py`. After `td agent submit-pr` opens the PR,
the wrapper diffs `origin/main...HEAD` in the worktree, runs the review with the
`.agents/skills/hostile-anti-slop-audit/SKILL.md` text read from `S5_CHECKOUT`
(`arii/skills` stays the source of truth), and always posts one PR comment
(`gh pr comment --body-file -`, capped at 3000 characters) before returning.

The model is the `audit` stage model chosen by the router (local first, no spec
override), limited to 2000 output tokens, under the same budget guard and
`NEBIUS_SPEND_CAP_USD` as the rest of the run. Each model call writes one
ledger row with script `slop_review.py`. The reply must be the strict verdict
JSON from `deep-agent/verdicts.py`. `READY` and `APPROVE` pass; `NEEDS-WORK` and
`BLOCK` fail. The review reports only and never edits the PR.

Results appear in the PR comment and in the `status` of `pipeline-result.json`
and `summary.json`:

| Status | Cause |
| --- | --- |
| `SLOP_REVIEW_FAILED` | Failing verdict, malformed or missing model output, model or git error, comment not posted, or diff over 40000 characters |
| `SLOP_REVIEW_CONFIG` | The skill file is missing or empty |

Both fail closed: the wrapper returns a failed submit, so the pipeline result is
never `APPROVED` and the PR is not handed to review. A diff over the limit is not
partly reviewed. It fails with detail `diff too large` and makes no model call.
These statuses are set after the repair loop and do not feed model tier
escalation.

Ordering with #1578: its `claims_pr_valid` is a deterministic check of the PR
body and files that is not wired in yet. The two mechanisms share no state and
both must pass. If #1578 wires its check into the same submit wrapper, it runs
first because it is free, then the slop review.

Tests use a fake model and fake `gh` (`tests/test_slop_review.py`). A live
review of a real PR needs Ari.

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
