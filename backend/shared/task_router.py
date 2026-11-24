"""Deterministic task-based model routing (v1).

Picks provider/model pairs from the live TwT catalog at dispatch time
based on properties of the task itself — not on waterfall-by-failure.
Every decision is auditable: the returned record carries the task
features, the rule that fired, the candidates considered, and the pick,
so a later learned router (jev-style decision model) can be trained on
decision -> outcome joins from these records plus summary/ledger cost.

Env contract (stdlib only — system-python tests import this module):
  TWT_CATALOG_DB   catalog sqlite (default ~/twt/catalog.sqlite3)
  TWT_DISABLED_PROVIDERS  comma-separated provider ids to exclude

CLI: python3 -m backend.shared.task_router --issue-file JOB_OR_ISSUE.json
     [--cap-usd 1.0] prints the routing decision as JSON.
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
EST_OUTPUT_TOKENS = 8192
CONTEXT_FLOOR = 32768
# A paid call must cost less than this fraction of the remaining issue
# cap or the model is not spendable for the task.
PAID_BUDGET_SHARE = 0.5
# Task-property rules that mean "start strong" — deterministic, no
# failure history needed. Tuned to the failures observed in the queue:
# multi-file edits and large specs are where free tiers churn.
STRONG_FILE_COUNT = 3
STRONG_INPUT_TOKENS = 12000

# Deterministic tie-break order for same-class picks (latency + proven
# reliability on S5 first).
PROVIDER_ORDER = [
    "google",
    "openrouter",
    "groq",
    "cloudflare",
    "nvidia",
    "sambanova",
    "nebius",
    "ollama-local",
]

_FILE_RE = re.compile(r"[\w./-]+\.(?:py|ts|tsx|js|jsx|md|json|yaml|yml|toml|sh)")


def catalog_path():
    return os.path.expanduser(os.environ.get("TWT_CATALOG_DB") or DEFAULT_CATALOG_DB)


def disabled_providers(raw=None):
    raw = raw if raw is not None else os.environ.get("TWT_DISABLED_PROVIDERS", "")
    return {p.strip().lower() for p in raw.split(",") if p.strip()}


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
    return {
        "est_input_tokens": len(text) // 4 + PROMPT_OVERHEAD_TOKENS,
        "file_count": len(set(files)),
        "needs_tools": True,  # every queue stage binds edit/read tools
    }


def suggested_tier(features):
    """Task-based start tier: hard-looking work starts 'strong' instead
    of burning cheap attempts first. 'default' when nothing fires."""
    if features["file_count"] >= STRONG_FILE_COUNT:
        return "strong", "task:many_files"
    if features["est_input_tokens"] >= STRONG_INPUT_TOKENS:
        return "strong", "task:large_spec"
    return "default", "task:standard"


def _est_call_cost(model, est_input_tokens):
    ip = model.get("input_price")
    op = model.get("output_price")
    if ip is None or op is None:
        return None
    return ip * est_input_tokens + op * EST_OUTPUT_TOKENS


def _eligible(model, features, disabled, free_only, cap_usd):
    if model.get("provider") in disabled:
        return False
    if not model.get("chat"):
        return False
    if features["needs_tools"] and not model.get("tools"):
        return False
    if (model.get("context_length") or 0) < max(
        CONTEXT_FLOOR, features["est_input_tokens"] * 2
    ):
        return False
    if free_only:
        return bool(model.get("free"))
    if model.get("free"):
        return True
    cost = _est_call_cost(model, features["est_input_tokens"])
    if cost is None:
        return False  # unrated spend fails closed, same as the spend gate
    return cost <= cap_usd * PAID_BUDGET_SHARE


def choose(tier, features, catalog, disabled=None, cap_usd=1.0):
    """Pick one model for one tier. Returns a decision record.

    'fast' ranks free tool-capable models by context window (the trim
    budget is the binding constraint on long sessions). 'strong' ranks
    every affordable model by price descending — the strongest model
    that fits the issue budget — so the full paid catalog is in play,
    not one env-pinned endpoint.
    """
    disabled = disabled_providers() if disabled is None else disabled
    free_only = tier != "strong"
    cands = [m for m in catalog if _eligible(m, features, disabled, free_only, cap_usd)]
    order = {p: i for i, p in enumerate(PROVIDER_ORDER)}
    if free_only:

        def key(m):
            return (
                -(m.get("context_length") or 0),
                order.get(m.get("provider"), len(order)),
                m.get("id") or "",
            )

    else:

        def key(m):
            return (
                -(m.get("input_price") or 0.0),
                -(m.get("context_length") or 0),
                m.get("id") or "",
            )

    cands.sort(key=key)
    pick = cands[0] if cands else None
    return {
        "tier": tier,
        "model": f"{pick['provider']}/{pick['id']}" if pick else None,
        "free_only": free_only,
        "rule": (
            "rank:free_context_desc" if free_only else "rank:affordable_price_desc"
        ),
        "candidates": len(cands),
        "top3": [f"{m['provider']}/{m['id']}" for m in cands[:3]],
        "est_call_cost_usd": (
            _est_call_cost(pick, features["est_input_tokens"]) if pick else None
        ),
    }


def route_issue(issue, cap_usd, catalog=None, disabled=None):
    """Full dispatch decision for one issue: start tier + a concrete
    model pick per tier. ``issue`` is the gh-shaped {title, body} dict."""
    text = f"{issue.get('title') or ''}\n{issue.get('body') or ''}"
    features = task_features(text)
    tier, tier_rule = suggested_tier(features)
    if catalog is None:
        catalog = load_catalog()
    return {
        "version": 1,
        "ts": time.time(),
        "features": features,
        "tier": tier,
        "tier_rule": tier_rule,
        "picks": {
            name: choose(name, features, catalog, disabled=disabled, cap_usd=cap_usd)
            for name in ("fast", "strong")
        },
    }


def log_decision(decision, issue_key="", ledger_record=None):
    """File the decision on the cost ledger (llm_call=False audit row)
    so routing choices join with spend and outcomes later."""
    if ledger_record is None:
        from backend.shared import ledger

        ledger_record = ledger.record
    picks = decision.get("picks", {})
    strong = picks.get("strong") or {}
    return ledger_record(
        script="task_router",
        model=strong.get("model") or (picks.get("fast") or {}).get("model") or "none",
        thread_id=issue_key,
        messages=[],
        started=decision.get("ts") or time.time(),
        extra={
            "event": "routing-decision",
            "llm_call": False,
            "issue": issue_key,
            "features": decision.get("features"),
            "tier": decision.get("tier"),
            "tier_rule": decision.get("tier_rule"),
            "picks": picks,
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
