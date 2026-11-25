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
  TWT_PAID_PROVIDERS  comma-separated provider ids allowed to spend
                      (union with DEFAULT_PAID_PROVIDERS)

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
# Minimum explicit parameter size (in billions) for a model to qualify
# for coding tasks. Tiny models (e.g. lfm-3-1.7b) burn inspection calls
# without making edits.
MIN_CODING_PARAMS_B = 14.0
# A paid call must cost less than this fraction of the remaining issue
# cap or the model is not spendable for the task.
PAID_BUDGET_SHARE = 0.5
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
_PARAM_SIZE_RE = re.compile(
    r"(?<![a-z0-9])(?:(\d+(?:\.\d+)?)b|(\d{2,3})m)(?![a-z0-9])", re.IGNORECASE
)
_WEAK_MODEL_RE = re.compile(
    r"(?<![a-z0-9])(?:nano|micro|tiny|mini|small|lite)(?![a-z0-9])", re.IGNORECASE
)
_META_ROUTER_IDS = frozenset({"openrouter/free", "openrouter/auto", "auto", "free"})


def _param_size_billions(model_id):
    """Extract the largest explicit parameter count (in billions) from a model id."""
    sizes = []
    for match in _PARAM_SIZE_RE.finditer(model_id or ""):
        b_val, m_val = match.groups()
        if b_val is not None:
            sizes.append(float(b_val))
        elif m_val is not None:
            sizes.append(float(m_val) / 1000.0)
    return max(sizes) if sizes else None


def coding_floor_rejection(model):
    """Return a short rejection reason if ``model`` fails the coding
    capability floor, or None when it qualifies."""
    model_id = str(model.get("id") or "").strip()
    lowered = model_id.lower()
    if lowered in _META_ROUTER_IDS or lowered.endswith(("/auto", "/free")):
        return "meta_router"
    params_b = _param_size_billions(model_id)
    if params_b is not None and params_b < MIN_CODING_PARAMS_B:
        return "below_min_params"
    if _WEAK_MODEL_RE.search(model_id):
        return "small_tier_name"
    max_out = model.get("max_output_tokens")
    if max_out is not None and max_out < EST_OUTPUT_TOKENS:
        return "low_max_output"
    return None


def catalog_path():
    return os.path.expanduser(os.environ.get("TWT_CATALOG_DB") or DEFAULT_CATALOG_DB)


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
    if model.get("free"):
        return 0.0
    ip = model.get("input_price")
    op = model.get("output_price")
    if ip is None or op is None:
        return None
    return ip * est_input_tokens + op * EST_OUTPUT_TOKENS


def _passes_base_checks(model, features, disabled, free_only, cap_usd, paid):
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
    if model.get("provider") not in paid:
        return False  # priced in the catalog but no funded key
    cost = _est_call_cost(model, features["est_input_tokens"])
    if cost is None:
        return False  # unrated spend fails closed, same as the spend gate
    return cost <= cap_usd * PAID_BUDGET_SHARE


def _eligible(model, features, disabled, free_only, cap_usd, paid):
    if not _passes_base_checks(model, features, disabled, free_only, cap_usd, paid):
        return False
    return coding_floor_rejection(model) is None


def _format_exclusions(counts):
    return ", ".join(f"{reason}={count}" for reason, count in sorted(counts.items()))


def choose(tier, features, catalog, disabled=None, cap_usd=1.0, blocked=None):
    """Pick one model for one tier. Returns a decision record.

    Both tiers enforce the coding capability floor (chat, tools, context
    window, minimum parameter size, non-lite/micro/nano/meta-router, and
    output budget). 'fast' ranks qualifying free models by context window;
    'strong' tries qualifying free models first, then picks the cheapest
    qualifying paid model within spend rules, logging why a paid fallback
    was needed in ``pick_reason``. With no qualifying model the pick is
    None and ``no_pick_reason`` explains why.
    """
    disabled = disabled_providers() if disabled is None else disabled
    blocked = paid_blockers() if blocked is None else blocked
    paid = paid_providers() - blocked
    free_only = tier != "strong"
    cands = []
    floor_excluded = {}
    free_floor_excluded = {}
    for model in catalog:
        if not _passes_base_checks(model, features, disabled, free_only, cap_usd, paid):
            continue
        rejection = coding_floor_rejection(model)
        if rejection is not None:
            floor_excluded[rejection] = floor_excluded.get(rejection, 0) + 1
            if model.get("free"):
                free_floor_excluded[rejection] = (
                    free_floor_excluded.get(rejection, 0) + 1
                )
            continue
        cands.append(model)
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
                0 if m.get("free") else 1,
                _est_call_cost(m, features["est_input_tokens"]),
                -(m.get("context_length") or 0),
                order.get(m.get("provider"), len(order)),
                m.get("id") or "",
            )

    cands.sort(key=key)
    pick = cands[0] if cands else None
    free_candidates = sum(1 for m in cands if m.get("free"))
    if pick is None:
        pick_reason = None
    elif pick.get("free"):
        pick_reason = "free candidate meeting coding capability floor"
    elif free_floor_excluded:
        pick_reason = (
            "paid fallback: no free candidate met coding capability floor "
            f"({_format_exclusions(free_floor_excluded)})"
        )
    else:
        pick_reason = "paid fallback: no eligible free candidate in catalog"
    min_ctx = max(CONTEXT_FLOOR, features["est_input_tokens"] * 2)
    no_pick_reason = None
    if pick is None:
        no_pick_reason = (
            f"no {tier} candidate meets the coding capability floor "
            f"(context>={min_ctx}, tools={features['needs_tools']}, "
            f"min_params_b={MIN_CODING_PARAMS_B:g}) within spend rules"
        )
        if floor_excluded:
            no_pick_reason += f" (floor excluded: {_format_exclusions(floor_excluded)})"
    return {
        "tier": tier,
        "model": f"{pick['provider']}/{pick['id']}" if pick else None,
        "free_only": free_only,
        "rule": (
            "rank:free_context_desc" if free_only else "rank:cheapest_meeting_floor"
        ),
        "pick_reason": pick_reason,
        "no_pick_reason": no_pick_reason,
        "candidates": len(cands),
        "free_candidates": free_candidates,
        "floor_excluded": dict(sorted(floor_excluded.items())),
        "paid_providers": sorted(paid),
        "paid_blocked": sorted(blocked),
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
    blocked = paid_blockers()
    return {
        "version": 1,
        "ts": time.time(),
        "features": features,
        "tier": tier,
        "tier_rule": tier_rule,
        "picks": {
            name: choose(
                name,
                features,
                catalog,
                disabled=disabled,
                cap_usd=cap_usd,
                blocked=blocked,
            )
            for name in ("fast", "strong")
        },
    }


def log_decision(
    decision, issue_key="", ledger_record=None, requested_tier=None, served_tier=None
):
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
            "requested_tier": requested_tier,
            "served_tier": served_tier,
            "tier_substituted": bool(requested_tier and requested_tier != served_tier),
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
