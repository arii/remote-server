"""Opt-in pipeline-stage CLI fallback (issue #1610).

TwT stays first; this adapter runs only when every applicable TwT
attempt has already failed with a classified eligible trigger, and
only when the operator has explicitly switched it on. The fallback
ideally never runs.

Triggers (only these, after the existing applicable TwT attempts):
- ``gateway_unavailable``: the TwT gateway answered 500/502/503/504
  or the request failed with a connection/timeout error.
- ``routes_exhausted``: every eligible TwT route was tried and each
  failed with a retryable (transient) error.
- ``budget_blocked``: TwT refused the request for budget reasons
  (HTTP 402, or a budget/quota/credits-exhausted message).

Never on: test/audit/scope failures, generic exceptions, ordinary
coder model/tool-call caps (ModelCallLimitExceededError),
STOP/pause/hold, unknown-cost safety stops, or local-only mode.

Readiness is checked in the actual execution environment, never by
inference: binary presence plus a per-provider non-interactive auth
probe. No auth probe is registered until its command has been
verified live in the terminal container, so a provider without one
is skipped as unauthenticated-unknown (fail closed). Auth is never
inferred from ``--version``, never auto-logged-in, never printed,
and no inference is ever run just to probe. Headless argv is likewise
registered only for CLIs whose flags were probed live; today that is
``muse`` and ``agy`` via ``agent_turn.turn_argv``. ``copilot`` and
``kiro-cli`` have no verified headless argv yet and are skipped.

Claim before dispatch: the Agent Ledger claim for the issue and the
open Kiro PRs are checked immediately before spawn. A missing,
unreadable, ambiguous, conflicting, or failed claim or PR check
blocks the fallback. A durable attempt journal prevents a
double-spawn across supervisors, restarts, and replays.

Every CLI execution gets a mandatory TwT audit entry under the
BoomTick-owned external-execution contract (see
``record_external_audit``): stable issue/run/stage/attempt IDs, CLI
identity, subscription provider classification, trigger, timing,
outcome, and worktree/PR provenance. Cost is zero only when
authoritatively justified as zero incremental subscription cost
(``KNOWN_ZERO_COST`` registry, empty today); otherwise it is unknown
and fail-closed. Model/token quantities stay explicitly unknown when
the CLI does not expose them. A generic Agent Ledger or Buttons
record supplements but never substitutes the TwT audit entry.

Gateway-down audit recovery is durable: the audit intent is
journaled (fsync) before spawn, and a pending entry is reconciled
against TwT exactly once on recovery. The pipeline must not report
settled until TwT acknowledges the entry.

The adapter shares the stage's remaining wall-clock deadline across
TwT and CLI attempts (bounded probes included, process-group
cancellation, no timeout reset) and preserves prior paid spend,
reservations, receipts, and per-issue cap accounting. The approved
plan, worktree isolation, deterministic verification/scope checks,
semantic audit, done-when, and hostile-slop gates are unchanged: a
CLI success alone cannot report APPROVED or submit/merge past them.
The adapter returns a coder-attempt-shaped result and the normal
pipeline gates run on it unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

# Validated allowlist, in configured order. muse is last by default:
# its usage is limited, so it is the fallback of last resort.
FALLBACK_ORDER = ("agy", "copilot", "kiro-cli", "muse")

# Env switches. Off by default; the operator enables explicitly.
ENABLED_ENV = "PIPELINE_CLI_FALLBACK"
ORDER_ENV = "PIPELINE_CLI_FALLBACK_ORDER"

# TwT gateway statuses that mean the gateway itself is unavailable
# or erroring (not a request problem). 429 is deliberately absent:
# a rate limit is a route-exhaustion signal, not gateway downtime.
GATEWAY_STATUSES = frozenset({500, 502, 503, 504})

# TwT-side budget refusal: HTTP 402, or a budget/quota message.
# No such signal exists in the codebase today; this classification
# is defined here so a real TwT budget refusal is recognised when
# it arrives, instead of being treated as a generic error.
_BUDGET_MSG = (
    "budget",
    "quota exceeded",
    "insufficient funds",
    "credits exhausted",
    "out of credits",
)

# Bound for one readiness probe (binary + auth). Probes share the
# stage deadline; this is a per-probe ceiling, not a reset.
PROBE_TIMEOUT = 30

# Kiro PR signals for the open-PR check (gh pr list): the bot
# author that opens Kiro's PRs, and the head-branch prefix. Both
# are the best available signals; a gh failure blocks (fail closed).
KIRO_PR_AUTHOR = "kiro-agent[bot]"
KIRO_BRANCH_PREFIX = "kiro/"

# Providers authoritatively known to cost zero incremental
# subscription cost, with the justification. Empty today: no
# provider is authoritatively known-zero, so every CLI execution
# records cost as unknown (fail closed) until an operator records
# a justification here.
KNOWN_ZERO_COST: dict[str, str] = {}


def _env_enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "").strip().lower() in ("1", "true", "yes")


def _env_order() -> tuple:
    raw = os.environ.get(ORDER_ENV, "").strip()
    if not raw:
        return FALLBACK_ORDER
    return validate_order([p.strip() for p in raw.split(",") if p.strip()])


def validate_order(order) -> tuple:
    """Validate an allowlist ordering: known providers only, no dupes.

    Unknown names raise ValueError; an empty order raises too (the
    switch with no eligible CLI is a misconfiguration, not a silent
    no-op).
    """
    seen = set()
    out = []
    for name in order:
        if name not in FALLBACK_ORDER:
            raise ValueError(f"unknown CLI fallback provider: {name!r}")
        if name in seen:
            raise ValueError(f"duplicate CLI fallback provider: {name!r}")
        seen.add(name)
        out.append(name)
    if not out:
        raise ValueError("CLI fallback order must not be empty")
    return tuple(out)


class CliFallbackConfig:
    """Switch + order + injectable seams (tests stub every boundary)."""

    def __init__(
        self,
        *,
        enabled=None,
        order=None,
        state_dir=None,
        runner=None,
        ledger_reader=None,
        ledger_post=None,
        gh_runner=None,
        twt_post=None,
        clock=time.monotonic,
    ):
        self.enabled = _env_enabled() if enabled is None else bool(enabled)
        self.order = _env_order() if order is None else validate_order(order)
        self.state_dir = state_dir
        self.runner = runner
        self.ledger_reader = ledger_reader
        self.ledger_post = ledger_post
        self.gh_runner = gh_runner
        self.twt_post = twt_post
        self.clock = clock

    def journal_dir(self) -> Path:
        if self.state_dir is not None:
            return Path(self.state_dir) / "cli-fallback"
        from backend.shared import ledger

        return Path(ledger.state_dir()) / "cli-fallback"


# ------------------------------------------------------------------
# Trigger classification
# ------------------------------------------------------------------


def _status_code(exc):
    """Best-effort HTTP status from OpenAI-compat error shapes."""
    for obj in (exc, getattr(exc, "response", None)):
        if obj is None:
            continue
        for attr in ("status_code", "status"):
            code = getattr(obj, attr, None)
            if isinstance(code, bool):
                continue
            if isinstance(code, int):
                return code
            if isinstance(code, str) and code.strip().isdigit():
                return int(code.strip())
    return None


def _message(exc) -> str:
    return str(exc or "").lower()


def _is_gateway_error(exc) -> bool:
    """A TwT gateway failure: 5xx from the gateway, or a connection /
    timeout failure reaching it. Rate limits and auth/validation
    errors are not gateway failures."""
    code = _status_code(exc)
    if code is not None:
        return code in GATEWAY_STATUSES
    msg = _message(exc)
    return any(
        tok in msg
        for tok in (
            "bad gateway",
            "gateway timeout",
            "service unavailable",
            "temporarily unavailable",
            "connection refused",
            "connection reset",
            "connection aborted",
            "connection failed",
            "connection error",
            "connecterror",
            "connectionerror",
            "timed out",
            "timeout",
            "timed-out",
        )
    )


def _is_budget_blocked(exc) -> bool:
    code = _status_code(exc)
    if code == 402:
        return True
    msg = _message(exc)
    return any(tok in msg for tok in _BUDGET_MSG)


def _is_call_cap(exc) -> bool:
    """Ordinary coder model/tool-call cap: never a CLI trigger."""
    return any(c.__name__ == "ModelCallLimitExceededError" for c in type(exc).__mro__)


def classify_twt_failure(errors, *, stopped=False) -> str | None:
    """Classify TwT attempt failures into a CLI trigger, or None.

    ``errors`` is the ordered list of exceptions from the applicable
    TwT attempts (initial + model fallback). Exclusions win over
    everything: a call cap, a STOP/pause/hold, or a local-only
    refusal never triggers the fallback.
    """
    if stopped or not errors:
        return None
    if any(_is_call_cap(e) for e in errors):
        return None
    if any(_is_budget_blocked(e) for e in errors):
        return "budget_blocked"
    if any(_is_gateway_error(e) for e in errors):
        return "gateway_unavailable"
    try:
        from backend.shared import model_router

        if all(model_router.is_retryable_error(e) for e in errors):
            return "routes_exhausted"
    except Exception:  # noqa: BLE001 - router import must never break classification
        return None
    return None


# ------------------------------------------------------------------
# Readiness probe + selection
# ------------------------------------------------------------------

# Non-interactive auth-probe argv per provider, registered ONLY
# after live verification in the terminal container. Empty today:
# no CLI has a verified auth probe, so every provider is skipped as
# unauthenticated-unknown (fail closed) until its probe is verified
# and registered here. Never infer auth from --version.
AUTH_PROBE_COMMANDS: dict[str, list] = {}

# Headless argv builders per provider, registered ONLY after live
# verification. muse and agy reuse agent_turn.turn_argv (verified
# flags, issue #320). copilot and kiro-cli have no verified headless
# argv yet and are skipped until one is verified and registered.
HEADLESS_PROVIDERS = ("muse", "agy")


def _default_runner(argv, *, timeout, cwd=None):
    """Run argv with stdin closed and a process group, bounded."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        start_new_session=True,
        check=False,
    )


def probe_cli(provider, *, config, deadline):
    """Check installation AND authenticated status, noninteractively.

    Returns {name, available, authenticated, reason}. A provider is
    selectable only when both are true. Never runs inference to
    probe, never auto-logs in, never prints credentials. The probe
    shares the stage deadline: an expired deadline is a skip, not
    a reason to extend it.
    """
    remaining = deadline - config.clock()
    if remaining <= 0:
        return {
            "name": provider,
            "available": False,
            "authenticated": False,
            "reason": "deadline expired",
        }
    runner = config.runner or _default_runner
    timeout = min(PROBE_TIMEOUT, remaining)
    try:
        # `command` is a shell builtin, so probe through sh: the same
        # runner seam the CLI spawn uses, and the provider name comes
        # from the validated allowlist (no interpolation).
        found = runner(
            ["sh", "-c", 'command -v "$1"', "probe", provider], timeout=timeout
        )
        binary_ok = getattr(found, "returncode", 1) == 0
    except Exception as exc:  # noqa: BLE001 - probe must never break a run
        return {
            "name": provider,
            "available": False,
            "authenticated": False,
            "reason": f"binary check failed: {type(exc).__name__}",
        }
    if not binary_ok:
        return {
            "name": provider,
            "available": False,
            "authenticated": False,
            "reason": "binary missing",
        }
    probe_argv = AUTH_PROBE_COMMANDS.get(provider)
    if probe_argv is None:
        return {
            "name": provider,
            "available": True,
            "authenticated": False,
            "reason": "no verified non-interactive auth probe",
        }
    remaining = deadline - config.clock()
    if remaining <= 0:
        return {
            "name": provider,
            "available": True,
            "authenticated": False,
            "reason": "deadline expired",
        }
    try:
        result = runner(list(probe_argv), timeout=min(PROBE_TIMEOUT, remaining))
    except Exception as exc:  # noqa: BLE001 - probe must never break a run
        return {
            "name": provider,
            "available": True,
            "authenticated": False,
            "reason": f"auth probe failed: {type(exc).__name__}",
        }
    if getattr(result, "returncode", 1) != 0:
        return {
            "name": provider,
            "available": True,
            "authenticated": False,
            "reason": "auth probe reported unauthenticated",
        }
    return {"name": provider, "available": True, "authenticated": True, "reason": "ok"}


def select_cli(order, *, config, deadline):
    """First provider in configured order that is installed AND
    authenticated. Returns (provider, probes) where probes is the
    per-provider probe dict in order tried. (None, probes) when none
    qualifies, with each skip reason recorded."""
    probes = []
    for provider in order:
        probe = probe_cli(provider, config=config, deadline=deadline)
        probes.append(probe)
        if probe["available"] and probe["authenticated"]:
            if provider not in HEADLESS_PROVIDERS:
                probes[-1] = {
                    **probe,
                    "authenticated": False,
                    "reason": "no verified headless argv",
                }
                continue
            return provider, probes
    return None, probes


# ------------------------------------------------------------------
# Claim-before-dispatch guard
# ------------------------------------------------------------------


class Blocked(Exception):
    """The fallback must not run: precise reason in str(exc)."""


def _ledger_link(repo, number) -> str:
    return f"https://github.com/{repo}/issues/{number}"


def _default_ledger_reader(link):
    """Authoritative Ledger rows for one issue link, latest first."""
    import queue_runner

    rows = [
        r for r in queue_runner.ledger_rows() if (r.get("Link") or "").strip() == link
    ]
    return rows


def _default_ledger_post(link, run_id):
    import queue_runner

    queue_runner.ledger_module("ledger_post").post_claim_row(link, run_id)


def _default_gh_runner(args):
    return subprocess.run(
        ["gh"] + list(args),
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        check=False,
    )


def check_claim(repo, number, *, config, run_id, stale_hours=24):
    """Authoritatively check ownership immediately before spawn.

    Returns the claim run_id to record. Raises Blocked when the
    claim is missing, unreadable, ambiguous, conflicting, or failed,
    when another source holds a fresh Act claim (including a manual
    handoff), or when an open Kiro PR exists for this issue. A claim
    is obtained (posted, then confirmed by re-reading) when none
    exists; a failed post or an unconfirmed re-read blocks.
    """
    link = _ledger_link(repo, number)
    reader = config.ledger_reader or _default_ledger_reader
    poster = config.ledger_post or _default_ledger_post
    try:
        rows = reader(link)
    except Exception as exc:
        raise Blocked(
            f"ledger unreadable ({type(exc).__name__}), failing closed"
        ) from exc
    if rows is None:
        raise Blocked("ledger unreadable (no rows returned), failing closed")
    if not rows:
        try:
            poster(link, run_id)
        except Exception as exc:
            raise Blocked(
                f"claim post failed ({type(exc).__name__}), failing closed"
            ) from exc
        try:
            rows = reader(link)
        except Exception as exc:
            raise Blocked(
                f"claim confirm read failed ({type(exc).__name__}), failing closed"
            ) from exc
        ours = [
            r
            for r in (rows or [])
            if (r.get("Link") or "").strip() == link
            and (r.get("Status") or "").strip() == "Act"
        ]
        if not ours:
            raise Blocked("claim posted but not confirmed on re-read, failing closed")
        return run_id
    latest = rows[-1]
    status = (latest.get("Status") or "").strip()
    source = (latest.get("Agent / Source") or "").strip()
    if status == "Act" and source and source != run_id:
        raise Blocked(f"fresh Act claim by {source[:40]}; not ours")
    if status not in ("Act", "Later", "Needs Okay", "Done", ""):
        raise Blocked(f"ambiguous ledger status {status!r}, failing closed")
    _check_open_kiro_prs(repo, number, config=config)
    return run_id


def _check_open_kiro_prs(repo, number, *, config):
    """Block when an open Kiro PR exists for this issue.

    A Kiro PR means Kiro owns the issue (including Ari's manual
    handoffs). Any gh failure blocks: an unchecked PR list may hide
    one. A PR is linked to the issue when its title or body names
    the issue number or URL.
    """
    gh = config.gh_runner or _default_gh_runner
    try:
        result = gh(
            [
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                "number,title,body,author,headRefName",
                "--limit",
                "100",
            ]
        )
    except Exception as exc:
        raise Blocked(
            f"open-PR check failed ({type(exc).__name__}), failing closed"
        ) from exc
    if getattr(result, "returncode", 1) != 0:
        raise Blocked(
            "open-PR check failed (gh exit "
            f"{getattr(result, 'returncode', '?')}), failing closed"
        )
    try:
        prs = json.loads(getattr(result, "stdout", "") or "[]")
    except Exception as exc:
        raise Blocked(
            f"open-PR check unparseable ({type(exc).__name__}), failing closed"
        ) from exc
    needle_num = f"#{number}"
    needle_url = _ledger_link(repo, number)
    for pr in prs:
        author = (pr.get("author") or {}).get("login") or ""
        head = pr.get("headRefName") or ""
        if author != KIRO_PR_AUTHOR and not head.startswith(KIRO_BRANCH_PREFIX):
            continue
        text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}"
        if needle_num in text or needle_url in text:
            raise Blocked(
                f"open Kiro PR #{pr.get('number')} for this issue; "
                "not starting a second agent"
            )
        if author == KIRO_PR_AUTHOR and not head.startswith(KIRO_BRANCH_PREFIX):
            # A Kiro PR that does not name the issue is not proof of
            # ownership of THIS issue; the claim check above governs.
            continue


# ------------------------------------------------------------------
# Durable attempt journal (no double-spawn across restarts)
# ------------------------------------------------------------------


def _journal_path(config, attempt_id) -> Path:
    directory = config.journal_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / f"{attempt_id}.jsonl"


def _append_jsonl(path, row):
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a") as handle:
        handle.write(json.dumps(row) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def begin_attempt(*, config, attempt_id, issue, run_id, stage, cli, trigger):
    """Persist the audit intent BEFORE spawning. Raises Blocked when
    this attempt was already started (restart/replay: never spawn
    twice for the same issue/stage/attempt)."""
    path = _journal_path(config, attempt_id)
    if path.exists():
        raise Blocked(
            f"attempt {attempt_id} already journaled; refusing a second spawn"
        )
    _append_jsonl(
        path,
        {
            "attempt_id": attempt_id,
            "issue": issue,
            "run_id": run_id,
            "stage": stage,
            "cli": cli,
            "trigger": trigger,
            "state": "started",
            "at": time.time(),
        },
    )
    return path


def finish_attempt(
    *, config, attempt_id, outcome, pr_url=None, cost_usd=None, cost_basis="unknown"
):
    """Append the outcome to the attempt journal (never rewrites)."""
    _append_jsonl(
        _journal_path(config, attempt_id),
        {
            "attempt_id": attempt_id,
            "state": "finished",
            "outcome": outcome,
            "pr_url": pr_url,
            "cost_usd": cost_usd,
            "cost_basis": cost_basis,
            "at": time.time(),
        },
    )


# ------------------------------------------------------------------
# TwT external-execution audit contract
# ------------------------------------------------------------------

# Minimal BoomTick-owned contract for TwT to acknowledge an
# execution that ran outside TwT inference. TwT-side implementation
# is pending; until the endpoint exists, entries stay journaled as
# pending and the pipeline must not report settled. Never fake an
# inference request (no trace_id is minted) and never weaken receipt
# ownership: the acknowledgement is TwT's, recorded here.
EXTERNAL_AUDIT_PATH = "/external-executions"


def external_audit_entry(
    *,
    audit_id,
    issue,
    run_id,
    stage,
    attempt_id,
    cli,
    trigger,
    started_at,
    finished_at=None,
    outcome,
    worktree,
    pr_url=None,
    cost_usd=None,
    cost_basis="unknown",
):
    """One external-execution audit entry. Model/token quantities
    stay explicitly unknown: the CLI does not expose them."""
    return {
        "audit_id": audit_id,
        "kind": "external_execution",
        "issue": issue,
        "run_id": run_id,
        "stage": stage,
        "attempt_id": attempt_id,
        "cli": cli,
        "provider_classification": "subscription",
        "trigger": trigger,
        "started_at": started_at,
        "finished_at": finished_at,
        "outcome": outcome,
        "worktree": worktree,
        "pr_url": pr_url,
        "cost_usd": cost_usd,
        "cost_basis": cost_basis,
        "model": "unknown",
        "tokens": "unknown",
    }


def _default_twt_post(entry):
    """POST one entry to TwT and return its acknowledgement.

    Raises on any failure (including 404: the endpoint is not
    implemented yet). The caller journals the entry as pending.
    """
    import httpx

    from backend.shared import twt_transport

    base = os.environ.get("TWT_BASE_URL", "http://127.0.0.1:8000/v1")
    url = base.rstrip("/") + EXTERNAL_AUDIT_PATH
    with httpx.Client(**twt_transport.client_kwargs(), timeout=30) as client:
        response = client.post(
            url,
            json=entry,
            headers={"Idempotency-Key": entry["audit_id"]},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"TwT external audit refused: HTTP {response.status_code}")
    ack = response.json()
    if not isinstance(ack, dict) or not ack.get("receipt_id"):
        raise RuntimeError("TwT external audit acknowledgement missing receipt_id")
    return ack


def record_external_audit(entry, *, config):
    """Journal the entry durably, then ask TwT to acknowledge it.

    Returns (acknowledged: bool, receipt_id or None). A TwT failure
    leaves the entry journaled as pending for reconciliation; it is
    never reported settled until TwT acknowledges it.
    """
    path = _journal_path(config, entry["audit_id"])
    _append_jsonl(path, {**entry, "state": "pending_audit"})
    poster = config.twt_post or _default_twt_post
    try:
        ack = poster(entry)
    except Exception:  # noqa: BLE001 - TwT down means pending, not a crash
        return False, None
    receipt_id = ack.get("receipt_id") if isinstance(ack, dict) else None
    if not receipt_id:
        return False, None
    _append_jsonl(
        path,
        {
            "audit_id": entry["audit_id"],
            "state": "acknowledged",
            "receipt_id": receipt_id,
            "at": time.time(),
        },
    )
    return True, receipt_id


def reconcile_external_audit(audit_id, entry, *, config):
    """Reconcile one pending entry against TwT, exactly once.

    Returns (acknowledged, receipt_id). Already-acknowledged entries
    are not re-posted: the journal is the idempotency record, and
    the Idempotency-Key header makes a retried POST safe.
    """
    path = _journal_path(config, audit_id)
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:  # noqa: BLE001,S112 - skip a corrupt journal line
                continue
            if row.get("state") == "acknowledged" and row.get("receipt_id"):
                return True, row["receipt_id"]
    return record_external_audit(entry, config=config)


# ------------------------------------------------------------------
# Orchestrator
# ------------------------------------------------------------------


def _cli_argv(provider, *, worktree, session_id, prompt, config):
    """Headless argv for one CLI turn, via the verified agent_turn
    seam. Raises Blocked for a provider with no verified argv."""
    if provider not in HEADLESS_PROVIDERS:
        raise Blocked(f"no verified headless argv for {provider}")
    from backend.exec import agent_turn

    return agent_turn.turn_argv(provider, worktree, session_id, prompt, continued=False)


def run_cli_fallback(
    *,
    config,
    issue,
    run_id,
    stage,
    attempt_id,
    trigger,
    brief,
    worktree,
    deadline,
    repo,
    number,
    verify_fn=None,
    ledger_post_fn=None,
):
    """Run one CLI fallback attempt for a pipeline stage.

    Returns a coder-attempt-shaped tuple (summary, usage, calls,
    started, label, trace, verified, verify_rounds) on success, so
    the normal pipeline gates (deterministic verification, semantic
    audit, done-when, hostile-slop, submit) run on it unchanged.
    Raises Blocked when the fallback must not run, or returns the
    CLI failure as a RuntimeError carrying the precise reason (the
    caller re-raises the original TwT error instead).

    Order: claim check -> CLI selection -> journal intent -> spawn
    (shared deadline, process-group kill) -> deterministic
    verification -> TwT audit -> Ledger result row.
    """
    if not config.enabled:
        raise Blocked("CLI fallback is off (PIPELINE_CLI_FALLBACK unset)")
    if verify_fn is None:
        raise Blocked(
            "no deterministic verification function; refusing "
            "an unverifiable CLI attempt"
        )
    if config.clock() >= deadline:
        raise Blocked("stage deadline already expired; no CLI attempt")
    claim_run_id = check_claim(repo, number, config=config, run_id=run_id)
    provider, probes = select_cli(config.order, config=config, deadline=deadline)
    if provider is None:
        reasons = "; ".join(f"{p['name']}: {p['reason']}" for p in probes)
        raise Blocked(f"no CLI qualifies: {reasons}")
    journal = begin_attempt(
        config=config,
        attempt_id=attempt_id,
        issue=issue,
        run_id=run_id,
        stage=stage,
        cli=provider,
        trigger=trigger,
    )
    started = time.time()
    session_id = str(uuid.uuid4())
    from backend.exec import agent_turn

    prompt = agent_turn.guard_prompt(brief)
    argv = _cli_argv(
        provider, worktree=worktree, session_id=session_id, prompt=prompt, config=config
    )
    remaining = deadline - config.clock()
    runner = config.runner or _default_runner
    outcome = "succeeded"
    summary = ""
    error = None
    try:
        result = runner(argv, timeout=remaining, cwd=worktree)
    except subprocess.TimeoutExpired:
        outcome, error = (
            "timed_out",
            RuntimeError(
                f"CLI fallback timed out after {remaining:.0f}s (shared stage deadline)"
            ),
        )
        result = None
    except Exception as exc:  # noqa: BLE001 - any spawn failure is an audited error
        outcome, error = (
            "error",
            RuntimeError(f"CLI fallback failed to run: {type(exc).__name__}: {exc}"),
        )
        result = None
    if result is not None and getattr(result, "returncode", 1) != 0:
        outcome = "failed"
        err = (getattr(result, "stderr", "") or "")[-500:]
        error = RuntimeError(
            f"CLI fallback {provider} exited "
            f"{getattr(result, 'returncode', '?')}: {err}"
        )
    if result is not None and error is None:
        summary = (getattr(result, "stdout", "") or "").strip()
    verified = True
    if error is None:
        try:
            verified, _failure_class, _reason = verify_fn(worktree)
        except Exception as exc:  # noqa: BLE001 - verification errors fail closed
            verified, outcome = False, "verification_error"
            error = RuntimeError(f"CLI fallback verification error: {exc}")
        if error is None and not verified:
            outcome = "verification_failed"
            error = RuntimeError(
                "CLI fallback verification failed: "
                "worktree did not pass deterministic checks"
            )
    # Every spawned execution gets its TwT audit entry, including
    # failed, timed-out, and verification-failed runs.
    cost_usd, cost_basis = None, "unknown"
    if provider in KNOWN_ZERO_COST:
        cost_usd, cost_basis = 0, "known_zero"
    finish_attempt(
        config=config,
        attempt_id=attempt_id,
        outcome=outcome,
        cost_usd=cost_usd,
        cost_basis=cost_basis,
    )
    entry = external_audit_entry(
        audit_id=attempt_id,
        issue=issue,
        run_id=run_id,
        stage=stage,
        attempt_id=attempt_id,
        cli=provider,
        trigger=trigger,
        started_at=started,
        finished_at=time.time(),
        outcome=outcome,
        worktree=worktree,
        cost_usd=cost_usd,
        cost_basis=cost_basis,
    )
    acknowledged, receipt_id = record_external_audit(entry, config=config)
    if not acknowledged:
        # The entry is journaled as pending; the pipeline must not
        # report settled until TwT acknowledges it. The CLI work is
        # real and the verification above stands, but the audit is
        # outstanding: surface it, do not silently settle.
        raise RuntimeError(
            "CLI fallback ran but its TwT audit entry is unacknowledged; "
            "reconcile it before reporting settled"
        )
    if error is not None:
        raise error
    poster = ledger_post_fn or _default_ledger_result_post
    try:
        poster(
            issue_link=_ledger_link(repo, number),
            note=(
                f"CLI fallback {provider} ({trigger}) ran, "
                f"TwT audit {receipt_id}, "
                f"cost {cost_usd if cost_usd is not None else 'unknown'} "
                f"({cost_basis})"
            ),
            state="Act",
            run_id=claim_run_id,
        )
    except Exception:  # noqa: BLE001,S110 - Ledger post never fails the run
        # A failed Ledger post never fails the run; the TwT audit
        # entry is the authoritative record and it is acknowledged.
        pass
    return (summary, {}, 0, started, f"cli:{provider}", str(journal), verified, 0)


def _default_ledger_result_post(*, issue_link, note, state, run_id):
    import queue_runner

    queue_runner.ledger_write(queue_runner.LEDGER_AGENT, note, state, issue_link)
