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
  "repo_paths": {
    "arii/remote-server": "/absolute/remote-server",
    "arii/boomtick": "/absolute/boomtick"
  },
  "discover_repos": ["arii/remote-server", "arii/boomtick"],
  "runtime_command": [],
  "worker_python": "/opt/venvs/td/bin/python",
  "cap_usd": 0.5,
  "max_common_failures": 3,
  "strong_after_failures": 2,
  "inter_issue_delay_seconds": 60,
  "environment": {
    "TWT_BASE_URL": "http://127.0.0.1:8000/v1",
    "CODER_STRONG_MODEL": "route:strong"
  }
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

### Polling for `ready` issues

At each supervisor start, for every repo in `discover_repos` the queue runs
`gh issue list --repo R --state open --label ready --json number,title,createdAt,labels --limit 100`
and appends issues missing from the manifest after all manifest entries, oldest
`createdAt` first. They then run sequentially under the usual rules. Held
(`skip_reason`), completed and closed issues are not added, and each is still
refreshed through GitHub before dispatch. The merge is in memory only: the manifest
stays the human-edited file and is never rewritten, and the queue never adds or
removes labels. Every `discover_repos` entry must also be in `repo_paths`
(startup fails otherwise). If the key is absent or empty, nothing is discovered.
If `gh` fails for a repo, a `DISCOVERY` line is logged and that repo is skipped
for this refresh. If a repo returns 100 issues, the log warns that older ones
may be missing.
## Dispatch gate

The queue dispatches an issue only if, when it is refreshed just before dispatch,
it is open and carries the `ready` label. Only Ariel adds `ready`; the queue never
adds or removes `ready` or `ledger-intake`. A listed issue without `ready` logs
`SKIP owner/repo#N not ready`. It is not marked completed, skipped or failed, and no
failure counter changes, so the next queue run checks it again and runs it once
`ready` is added. If the refresh call fails or returns unusable data, the queue fails
closed: it logs `SKIP owner/repo#N refresh failed: <reason>` and does not dispatch.
Closed issues are marked completed without dispatch.

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

The diff is wrapped in a per-call random `<diff-TOKEN>` ... `</diff-TOKEN>` marker pair (`secrets.token_hex(16)`), and the prompt tells the model to ignore instructions inside it. A diff that contains the token fails closed with no model call.

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

## Agent Ledger rows

`python3 scripts/ledger_intake_review.py OWNER/REPO NUMBER` audits one new `ledger-intake` issue through local Ollama/TwT and may post one finding comment. It checks the issue body for the canonical `problem`, `scope` and `done_when` fields (the hook's `Problem`, `Out of scope` and `Done when` sections; `title` and `repo` come from the issue itself) and reports gaps by those names. It never changes labels or issue state.

### Missing routing fields (#1585)

#### Problem

Intake rows can arrive without a repo, scope or done-when. Guessing them would
route work to the wrong place.

#### Done when

- A row missing `repo`, `scope` or `done_when` is `NEEDS_INFO`, with the
  canonical names in `missing_fields`. Blank values stay blank.
- The reviewer only reports field names. Any value it returns is ignored.
- `POST /api/ledger-intake/{id}/approve` returns 400 until all five required
  fields (`title`, `repo`, `problem`, `scope`, `done_when`) are present, from
  the row or from the human's input.
- Dispatch still needs Ariel's `ready` label. Nothing retries automatically.

#### Out of scope

Deriving `READY` in `status_for` (#1613) and any new required field.

The supervisor reads the response Sheet via `scripts/ledger_read.py` and posts
claims and results as `S5-Queue` via `scripts/ledger_post.py`. The only agent
writers are this script and BoomTick's `scripts/agent_ledger.py`. Ledger rows
are data, not a source of work. Ariel dispatches Kiro herself per issue; Devin
never starts Kiro. Kiro uses the BoomTick CLI to post its own claim and result.

Key: the `Link` column is always the issue URL
(`https://github.com/<repo>/issues/<n>`), because `ledger_read.py` folds the
latest row per `Link`. The PR URL goes in the note, not in `Link`, so the
folded row for the issue stays the latest state.

| When | Status | Note |
| --- | --- | --- |
| Before dispatch | `Act` | `claimed by queue run <run-id>` |
| PR opened | `Act` | PR URL, TwT cost total with its `cost_basis`, `tests pass` |
| Failed, blocked, slop review failed | `Later` | the pipeline status code, or `max_common_failures` |
| Queue halted on the issue | `Later` | `queue_halted` |
| Spend cap reached | `Needs Okay` | `spend cap reached` |
| PR seen merged at a later run | `Done` | `merged <sha> <PR URL>` |

Before dispatching, the supervisor reads the latest row for the issue URL. A
row from another source with status `Act` newer than `ledger_stale_hours`
(config, default `6`) makes the supervisor skip the issue, log
`LEDGER skip ...`, and record it in `status.json` `ledger_skipped`. The issue is
not marked completed, so the next run checks again. Rows from `S5-Queue`, older
claims, and other statuses do not block. The supervisor reads the response
Sheet through Drive CSV export, not the hourly JSONL snapshot. It interprets
timestamps as UTC.

If the ledger cannot be read, the check fails closed: the issue is skipped and
recorded in `ledger_skipped` as `ledger unreadable (<ErrorType>), failing closed`.
The next run checks it again. The reader is standard library only, so the
service's `/usr/bin/python3` can run it. On the S5 host set `GOOGLE_TOKEN_FILE` in
`~/.config/twt-queue/environment` to the host path of
`gdocs-access/token.json`, because the default is the container path.
[PR #1619](https://github.com/arii/remote-server/pull/1619) merged and was
deployed on S5: the queue worktree was on main at `3d6c58c` with
`GOOGLE_TOKEN_FILE` set. A live claim check read 215 ledger rows and skipped a
claimed issue. This proves the live claim check, not the full claim-to-result
handoff tracked in [#1537](https://github.com/arii/remote-server/issues/1537).

Nov 23 status: `twt-queue.service` failed at 04:10 and a restart was in progress
when reported. Separately, main hit a test-only lint failure fixed by merged
[PR #1621](https://github.com/arii/remote-server/pull/1621); two root test
failures on main are tracked in [#1623](https://github.com/arii/remote-server/issues/1623).

A failed post never fails the run. The row is saved in `status.json`
`ledger_pending`, retried once at the next boundary (the start of the next
`supervise` run or the next dispatch), and dropped with a log line if that retry
fails too. Merge confirmation also happens at the start of each run: for PRs in
`status.json` `ledger_open_prs`, the supervisor runs `gh pr view` and posts
`Done` once when the PR is merged. Closed PRs are forgotten.

Notes hold only fixed text, the run id, a status code, the PR URL, and the cost
figure. They never include pipeline detail, repo content, or environment values,
because the Form is public.

Not posted: a `Needs Okay` row for login or CAPTCHA walls (the queue cannot
detect them), a pass/fail result against the issue's "done when" check, and an
issue comment per result. See [#1537](https://github.com/arii/remote-server/issues/1537)
for the remaining result requirements and live claim-to-result proof.

Root tests run behind a network guard for `docs.google.com`
([#1617](https://github.com/arii/remote-server/pull/1617)). Stub ledger reads
and writes in queue tests; never point a test at the live Form.

### Human confirm for intake rows (#1584)

Agents flag intake rows; a human decides. Duplicate detection (exact hash or fuzzy title match) and `ledger_intake_review.py` only flag. They never link, merge, reset or delete a row.

Gated actions on the Ledger triage screen, each a two-step tap and each rejected with 400 by the backend unless the body carries `confirmed: true` and the matching intent:

| Action | Endpoint | Intent |
| --- | --- | --- |
| Merge duplicate (links `canonical_intake_id`, appends notes) | `POST /api/ledger-intake/{id}/merge` | `ledger-intake-merge` |
| Not a duplicate (resets to `NEW`, never `READY`, drops the link, remembers the pair so it is not flagged again) | `POST /api/ledger-intake/{id}/distinct` | `ledger-intake-distinct` |

Merge always needs the confirm, for exact-hash and fuzzy matches alike. This overrides the one-tap Merge in #1583. The hook may still set `DUPLICATE` on an exact dedupe hash at submit, because that is a status and not a link. Those rows stay in triage until a human merges or marks them distinct, and `approve` rejects them with 400 until then. Merge and distinct both only accept the candidate the detector flags.

The intake code and routes contain no delete path. The only path that removes them is `POST /api/ledger/clear` (intent `ledger-clear`), which truncates the whole ledger including intake rows. `backend/tests/test_briefing_ledger.py` guards this in `test_ledger_intake_no_delete_path`, a tripwire on call names and direct file writes in `scripts/ledger_intake*.py` and the intake routes, not a proof. Only Ari adds `ready` to a `ledger-intake` issue.

### High-blast-radius lock (#1586)

A schema-complete intake row that matches a high-blast-radius category gets
status `NEEDS_APPROVAL` instead of `READY`. Detection is deterministic keyword
matching (no model) over `title`, `problem`, `scope`, `done_when` and `notes`,
case-insensitive, at word starts. False positives are expected and cost one
confirm. The keyword constants live in `backend/core/intake_risk.py`.

| Category | Keywords |
| --- | --- |
| `production_mutation` | deploy, migrate, migration, delete prod, production data, drop table/database/schema/column/index, truncate table, rm -rf, compose up, restart service(s) |
| `external_comms` | email, e-mail, tweet, post to, post a, slack, ntfy, publish, comment on, send a message, announce |
| `billing_permission` | billing, payment, spend cap, quota, permission, iam, token scope, branch protection, secret, admin |

Status precedence is unchanged: `DUPLICATE`, then `NEEDS_INFO`, then
`NEEDS_APPROVAL`, then `READY`. `scripts/ledger_intake.py` `build_row` writes
`risk_categories`. The boomtick hook still writes `NEW`, so
`GET /api/ledger-intake` re-derives the categories server side and lists
`NEW`, `NEEDS_INFO`, `NEEDS_APPROVAL` and suspected duplicates.

Nothing promotes `NEEDS_APPROVAL` automatically. A human approves it in the
Ledger triage view, which names the categories and asks inline before sending
`POST /api/ledger-intake/{id}/approve` with
`{"intent": "ledger-intake-approve-high-risk", "confirmed": true}`. Without
that confirm the route returns 400. The five required fields (`title`, `repo`,
`problem`, `scope`, `done_when`) must still be non-blank. The queue dispatches
only on the GitHub `ready` label; no intake status maps to that label, so a
`NEEDS_APPROVAL` row cannot be claimed.

## Strong-tier routing

The worker always runs with `NEBIUS_ONLY=1` and no `CODER_MODEL`, so an ordinary
attempt uses TwT model `default`, which TwT routes local-first and can burn its
whole timeout on a hard issue. A **strong** attempt instead sends the explicit
`CODER_STRONG_MODEL` to TwT from the first coder call. With the configured
`route:strong` profile, TwT tries the local strong model before eligible free
providers and Nebius last. The coder's ordinary `default` route is not used,
so the bounded fallback above (it fires only for `default`) cannot
double-escalate a strong attempt. That
fallback stays as the in-attempt safety net for ordinary attempts. The tier chosen
here is only the starting tier; once strong, pipeline repair cycles stay strong.

Starting tier is decided per attempt by the supervisor:

1. The GitHub issue has the label `model:strong` (read from the live issue
   payload at dispatch): reason `label:model:strong`. Add the label to route an
   expected-hard issue straight to strong; remove it to stop.
2. Otherwise, once the issue has `strong_after_failures` (default 2) counted
   failures, reason `auto-escalation:<count>`.
3. Otherwise `default`.

Counted failures are finished runs with a nonzero exit, no halt, no budget stop,
and result status `BLOCKED_DETERMINISTIC` or `CHANGES_REQUESTED`. Not counted:
`CONFIG_ERROR`, `STRONG_BUDGET_INSUFFICIENT`, `PAUSED_BY_USER`, `RUNNER_ERROR`,
worker halts, budget or STOP interruptions. A successful run clears the counter;
uncounted outcomes leave it unchanged. The counter is `status.json`
`tier_failures["owner/repo#N"]`, so it survives restarts and `--retry`.

Spend: strong attempts use the same per-issue cap (`NEBIUS_SPEND_CAP_USD` is set
from the remaining issue budget) and the same `install_budget_guard`; there is
no second cap. Before the pipeline starts, the worker reads recorded spend and
refuses the attempt with `STRONG_BUDGET_INSUFFICIENT` if less than $0.05 is left.
That attempt makes no model call, does not fall back to local, and does not
count toward escalation.

A strong attempt without `CODER_STRONG_MODEL` in the config `environment` fails
before any model call with `CONFIG_ERROR` and **halts the queue** (every later
strong issue would fail the same way). Set the variable, then use `--clear-halt`.

Where to read the tier: the supervisor prints
`START owner/repo#N tier=strong reason=label:model:strong`; the worker logs
`tier=... reason=... model=...` to `run.log`, followed by the coder's
`coder via twt:<model> (tier=strong)`; `job.json` has `model_tier` and
`tier_reason`; `summary.json` has `tier`, `tier_reason`, `strong_model`; and
`status.json` `attempts["owner/repo#N"]` lists tier, reason, status and exit code
per attempt. Known gaps: the plan and resume coder paths still call
`build_model(CODER_MODEL)` without a tier, and ledger rows carry no tier tag.

## Coder call budget, splitting and resume

The per-turn coder limits (`INITIAL_MODEL_LIMIT` 20, `REPAIR_MODEL_LIMIT` 12) are not raised. Instead the pipeline fits work to the budget and bounds recovery.

**Size budget and split rule** (`deep-agent/pipeline.py`). A brief over `BRIEF_LINE_BUDGET` (60 non-blank lines) or `BRIEF_FILE_BUDGET` (4 distinct files, an unvalidated guess) is split deterministically (no model call) from its acceptance / done-when items into at most `SPLIT_MAX_PARTS` (3) sequential sub-briefs, each with its own acceptance list. Sub-briefs run in the same worktree. Each must be coder-verified and pass `pre_audit_check` before the next starts. One audit and one PR cover the whole diff, only when every sub-brief is done. An over-budget brief with fewer than 2 acceptance items ends as `BUDGET_UNSPLITTABLE` with no PR and no model call. Like exhaustion it posts a `Needs Okay` ledger row (`Needs Okay: BUDGET_UNSPLITTABLE, queue run <id>`): add explicit done-when items or split the issue by hand.

**Shared state.** The worker sets `S5_BUDGET_STATE=<job_dir>/budget.json`. Shape: `{parent:{calls_used,max_calls,exhausted,verified}, sub_briefs:[{id,title,acceptance,status,calls,continuations,verified,verification}]}`. It is written atomically after each sub-brief transition and copied into `summary.json["budget"]`. Small briefs get a one-entry record. Small briefs also get one continuation, so a capped turn resumes once before the TwT fallback (up to 20+20 calls before the fallback, still under the 60 total). The `code` entry in `stages.json` carries `calls` (parent total).

**Resume and caps.** When a coder turn hits its call limit, it continues once on the same checkpointed thread with a fresh per-turn budget (`MAX_CONTINUATIONS_PER_SUBBRIEF` = 1). Total model calls per parent task stop at `MAX_CALLS_PER_PARENT` (60 = 3 x 20, above the 21-40 bucket on purpose). The cap is checked before every new model-call turn. Continuation is never unbounded.

**Exhaustion.** The run ends `BUDGET_EXHAUSTED` (exit nonzero, no PR, worktree kept, `result["partial"]` lists done and not_done acceptance items). It is not in `COUNTED_STATUSES`: it does not bump or reset `tier_failures`, so it never triggers auto-escalation. The ledger gets `Needs Okay: BUDGET_EXHAUSTED, queue run <id>` with state `Needs Okay`. The supervisor does not requeue it. Operator options:

- split the issue by hand into smaller issues with explicit done-when items;
- `--retry owner/repo#N`, which is the explicit approval and starts a fresh parent budget. The old job dir, including `budget.json`, is archived and done sub-briefs are not reused: the retry re-runs every sub-brief from the start (`budget.json` is a record, not resume input);
- add the `model:strong` label to start on the strong tier.

**Reading `--budget-report`.** `python3 scripts/queue_runner.py --config <config> --budget-report` reads every job's `summary.json["budget"]` under `state_dir` and prints, offline (runs with 0 calls are skipped), runs and verified completion rate per call bucket, calls per verified completion, and the budget-exhaustion rate. Sample from fixtures:

```
bucket  runs  verified  rate
1-5        1         1  100%
6-10       1         1  100%
11-20      1         0  0%
21-40      1         1  100%
>40        1         0  0%
runs=5 verified=3 calls_per_verified=13.7
budget_exhaustion_rate=20% (1/5)
```

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

## Out-of-scope findings

Queue PRs must pass `claims_pr_valid` (a `Closes #N` reference plus at least one
test file). Out-of-scope findings are never shipped as code: submit them via
`scripts/ledger_post.py` as task-shaped rows (`format_out_of_scope_finding`
returns `{"issue", "title", "problem"}`), posted to the Agent Ledger.

### Strong routing profile prerequisite

The example above uses `CODER_STRONG_MODEL=route:strong`. It requires a TwT
gateway version with the strong profile (Boomtick PR1407). Configure stronger
provider model IDs using the gateway's `TWT_STRONG_MODELS` JSON map and an
installed local model using `TWT_OLLAMA_STRONG_MODEL`, then sync its catalog.
The profile tries local Ollama first, eligible free providers next and Nebius
last, with the existing daily budget and receipts. Keep the gateway's
`TWT_DAILY_BUDGET_USD=5` cap; this example does not change the cap or authorize
extra provider calls. Do not use this value against an older gateway that
only accepts default routes and exact model IDs. This is documentation, not
a change to a running coder or host configuration.
