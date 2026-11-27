"""Deterministic task-based model routing (v1).

Determines task tier (default vs strong) based on properties of the task itself
at dispatch time. The TwT gateway owns candidate selection and cooldown failover.
Every decision is auditable: the returned record carries the task features
and the rule that fired.

Env contract (stdlib only — system-python tests import this module):
  TWT_CATALOG_DB   catalog sqlite (default ~/twt/catalog.sqlite3)
  TWT_DISABLED_PROVIDERS  comma-separated provider ids to exclude
  TWT_PAID_PROVIDERS  comma-separated provider ids allowed to spend
                      (union with DEFAULT_PAID_PROVIDERS)

CLI: python3 -m backend.shared.task_router --issue-file JOB_OR_ISSUE.json
     prints the routing decision as JSON.
"""

import json
import os
import re
import sqlite3
import sys
import time

DEFAULT_CATALOG_DB = "~/twt/catalog.sqlite3"
# Rough dispatch-time estimate of the system + tool overhead the coder
# stack adds to the brief (attempt traces show ~4-7k tokens/call).
PROMPT_OVERHEAD_TOKENS = 6000
# Providers whose configured key can actually pay. Catalog prices alone
# do not imply spend — a free-tier key 402s on paid models (OpenRouter
# /key on S5: is_free_tier=true, ~$0.001 remaining). TWT_PAID_PROVIDERS
# adds providers as accounts get funded.
DEFAULT_PAID_PROVIDERS = {"nebius"}
# Recent gateway attempts returning payment-required also block paid
# picks for that provider; the trace lookback auto-clears when billing
# is fixed (older rows fall out of the window).
PAID_BLOCK_TRACE_ROWS = 5000
# Task-property rules that mean "start strong" — deterministic, no
# failure history needed. Tuned to the failures observed in the queue:
# multi-file edits and large specs are where free tiers churn.
STRONG_FILE_COUNT = 3
STRONG_INPUT_TOKENS = 12000

_FILE_RE = re.compile(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|md|json|yaml|yml|toml|sh)")
_ARCHITECTURAL_KEYWORDS = re.compile(
    r"\b(refactor|refactoring|rewrite|architecture|architectural|redesign|migrate|migration|concurrency|deadlock|race\s+condition|distributed|breaking\s+change)\b",
    re.IGNORECASE,
)


def catalog_path():
    env = os.environ.get("TWT_CATALOG_DB")
    if env:
        return os.path.expanduser(env)
    for candidate in (
        os.path.expanduser(DEFAULT_CATALOG_DB),
        os.path.expanduser("~/.local/share/twt/catalog.sqlite3"),
        "/root/.local/share/twt/catalog.sqlite3",
        "/var/lib/buttons/twt/catalog.sqlite3",
    ):
        if os.path.exists(candidate):
            return candidate
    return os.path.expanduser(DEFAULT_CATALOG_DB)


def disabled_providers(raw=None):
    raw = raw if raw is not None else os.environ.get("TWT_DISABLED_PROVIDERS", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


def paid_providers(raw=None):
    raw = raw if raw is not None else os.environ.get("TWT_PAID_PROVIDERS", "")
    extra = {p.strip().lower() for p in raw.split(",") if p.strip()}
    return DEFAULT_PAID_PROVIDERS | extra


def paid_blockers(path=None):
    """Providers whose recent gateway attempts hit payment-required
    (HTTP 402) — live spend truth from the TwT trace table. Auto-clears
    once those rows age out of the lookback window."""
    db_path = path or catalog_path()
    if not os.path.exists(db_path):
        return set()
    blocked = set()
    try:
        db = sqlite3.connect(db_path)
        try:
            rows = db.execute(
                "select data from traces order by timestamp desc limit ?",
                (PAID_BLOCK_TRACE_ROWS,),
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return set()
    for (data,) in rows:
        try:
            trace = json.loads(data)
        except ValueError:
            continue
        for attempt in trace.get("attempts") or []:
            reason = str(attempt.get("reason") or "")
            if "payment required" in reason or "402" in reason:
                provider = attempt.get("provider")
                if provider:
                    blocked.add(provider)
    return blocked


def load_catalog(path=None):
    """Rows from the TwT catalog sqlite, excluding retired models."""
    db = sqlite3.connect(path or catalog_path())
    try:
        retired = set()
        try:
            retired = {
                r[0] for r in db.execute("select id from retired_models").fetchall()
            }
        except sqlite3.Error:
            pass
        rows = []
        for provider, mid, data in db.execute(
            "select provider, id, data from models"
        ).fetchall():
            if f"{provider}/{mid}" in retired or mid in retired:
                continue
            try:
                row = json.loads(data)
            except ValueError:
                row = {}
            row.setdefault("provider", provider)
            row.setdefault("id", mid)
            rows.append(row)
        return rows
    finally:
        db.close()


def task_features(text="", files=None):
    """Deterministic task profile used by both the tier rule and the
    model pick. ``files`` is an explicit path list when a caller has one
    (a spec); otherwise paths are counted from the text."""
    files = list(files or []) or _FILE_RE.findall(text)
    is_architectural = bool(_ARCHITECTURAL_KEYWORDS.search(text))
    return {
        "est_input_tokens": len(text) // 4 + PROMPT_OVERHEAD_TOKENS,
        "file_count": len(set(files)),
        "needs_tools": True,  # every queue stage binds edit/read tools
        "is_architectural": is_architectural,
    }


def suggested_tier(features):
    """Task-based start tier: hard-looking work starts 'strong' instead
    of burning cheap attempts first. 'default' when nothing fires."""
    if features.get("is_architectural"):
        return "strong", "task:architectural"
    if features["file_count"] >= STRONG_FILE_COUNT:
        return "strong", "task:many_files"
    if features["est_input_tokens"] >= STRONG_INPUT_TOKENS:
        return "strong", "task:large_spec"
    return "default", "task:standard"


def route_issue(issue, cap_usd=1.0, catalog=None, disabled=None):
    """Full dispatch decision for one issue: start tier based on task features.
    ``issue`` is the gh-shaped {title, body} dict.
    Candidate selection and cooldown failover are handled by the TwT gateway."""
    text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
    features = task_features(text)
    tier, tier_rule = suggested_tier(features)
    return {
        "version": 1,
        "ts": time.time(),
        "features": features,
        "tier": tier,
        "tier_rule": tier_rule,
    }


def log_decision(
    decision, issue_key="", ledger_record=None, requested_tier=None, served_tier=None
):
    """File the decision on the cost ledger (llm_call=False audit row)
    so routing choices join with spend and outcomes later."""
    if ledger_record is None:
        from backend.shared import ledger

        ledger_record = ledger.record
    tier = decision.get("tier")
    model = "route:strong" if tier == "strong" else "default"
    return ledger_record(
        script="task_router",
        model=model,
        thread_id=issue_key,
        messages=[],
        started=decision.get("ts") or time.time(),
        extra={
            "event": "routing-decision",
            "llm_call": False,
            "issue": issue_key,
            "features": decision.get("features"),
            "tier": tier,
            "tier_rule": decision.get("tier_rule"),
            "requested_tier": requested_tier,
            "served_tier": served_tier,
            "tier_substituted": bool(requested_tier and requested_tier != served_tier),
        },
        usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        calls=[],
    )


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--issue-file",
        required=True,
        help="JSON file with {title, body} (issue or job.json)",
    )
    parser.add_argument("--cap-usd", type=float, default=1.0)
    parser.add_argument("--catalog", default=None)
    args = parser.parse_args(argv)
    with open(args.issue_file, encoding="utf-8") as handle:
        issue = json.load(handle)
    if "issue" in issue:  # job.json wraps the issue
        issue = issue["issue"]
    decision = route_issue(
        issue,
        cap_usd=args.cap_usd,
        catalog=None if args.catalog is None else load_catalog(args.catalog),
    )
    json.dump(decision, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

