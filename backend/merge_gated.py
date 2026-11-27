#!/usr/bin/env python3
"""merge_gated — scheduled gated auto-merge for target repos (#867, #1790).

Merges open PRs oldest-first when ALL machine-verifiable gates hold fresh:
- CI green (statusCheckRollup passing)
- Mergeable (clean or behind default branch)
- Target base in allowed bases (default branch)
- No operator hold / skip_reason (hold labels, held release PRs, draft)
Up to MERGE_CAP per pass.

Usage:
  merge_gated.py --repo NAME --gh OWNER/REPO   # single repo
  merge_gated.py --loop [--interval SECONDS]   # periodic loop over target repos
  merge_gated.py --once                        # single pass over target repos
"""

from __future__ import annotations

import argparse
import re
import sys
import time

from backend.pr_gates import (
    MERGE_CAP,
    default_branch,
    evaluate_merge_gates,
    merge_pr,
    open_prs,
    pr_gate_view,
)

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
GH_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

DEFAULT_TARGET_REPOS = (
    ("boomtick", "arii/boomtick"),
    ("remote-server", "arii/remote-server"),
)

HOLD_LABELS = frozenset(
    {
        "hold",
        "do-not-merge",
        "operator-hold",
        "autorelease: pending",
        "wip",
        "draft",
    }
)

MERGE_VIEW_FIELDS = (
    "number,title,headRefName,baseRefName,isDraft,labels,"
    "statusCheckRollup,mergeStateStatus,reviewDecision"
)


def skip_reason(view_or_pr: dict, gh: str = "") -> str | None:
    """Return a skip reason string if the PR is on hold or should not be auto-merged."""
    if not isinstance(view_or_pr, dict):
        return None
    # Check draft state
    if view_or_pr.get("isDraft") is True or view_or_pr.get("draft") is True:
        return "draft PR"
    # Check labels
    raw_labels = view_or_pr.get("labels") or []
    labels = {
        (lbl.get("name") if isinstance(lbl, dict) else str(lbl)).strip().lower()
        for lbl in raw_labels
    }
    for hold_label in HOLD_LABELS:
        if hold_label in labels:
            return f"held by label: {hold_label}"
    # Specific hold PRs like boomtick #1473 (held release PR)
    pr_number = view_or_pr.get("number")
    repo = (gh or view_or_pr.get("repo") or "").lower()
    if "boomtick" in repo and pr_number == 1473:
        return "held release PR (boomtick #1473)"
    # Check release PR title / branch
    head_ref = (
        view_or_pr.get("headRefName") or view_or_pr.get("head_branch") or ""
    ).lower()
    title = (view_or_pr.get("title") or "").lower()
    if head_ref.startswith("release-please") or "autorelease: pending" in labels:
        return "held release PR"
    if title.startswith("chore: release"):
        return "held release PR"
    return None


def fetch_pr_view(gh: str, pr_number: int, runner=None) -> dict:
    """Fetch PR view with fields needed for gate evaluation and hold detection."""
    try:
        from backend.gh import run_gh_json

        return run_gh_json(
            gh,
            [
                "pr",
                "view",
                str(pr_number),
                "--repo",
                gh,
                "--json",
                MERGE_VIEW_FIELDS,
            ],
            timeout=60,
            runner=runner,
        )
    except Exception:
        return pr_gate_view(gh, pr_number, runner=runner)


def evaluate_repo(
    name: str,
    gh: str,
    *,
    require_approval: bool = False,
    cap: int = MERGE_CAP,
    default_branch_fn=None,
    open_prs_fn=None,
    pr_gate_view_fn=None,
    merge_pr_fn=None,
    runner=None,
    log_fn=print,
) -> dict:
    """Evaluate open PRs for a repo and merge qualifying green ones."""
    get_default_branch = default_branch_fn or (
        lambda r: default_branch(r, runner=runner)
    )
    get_open_prs = open_prs_fn or (lambda r: open_prs(r, runner=runner))
    get_pr_view = pr_gate_view_fn or (
        lambda r, num: fetch_pr_view(r, num, runner=runner)
    )
    do_merge = merge_pr_fn or (lambda r, num: merge_pr(r, num, runner=runner))

    try:
        bases = [get_default_branch(gh)]
    except Exception as exc:
        log_fn(f"cannot resolve default branch for {gh}: {exc}; merged nothing")
        return {"repo": name, "gh": gh, "merged": 0, "skipped": 0, "error": str(exc)}

    try:
        prs = get_open_prs(gh)
    except Exception as exc:
        log_fn(f"cannot list open PRs for {gh}: {exc}; merged nothing")
        return {"repo": name, "gh": gh, "merged": 0, "skipped": 0, "error": str(exc)}

    if not prs:
        log_fn(f"no open PRs in {gh}; merged nothing")
        return {"repo": name, "gh": gh, "merged": 0, "skipped": 0, "error": None}

    prs = sorted(prs, key=lambda p: p.get("number") or 0)
    merged = 0
    skipped = 0
    results = []

    for pos, pr in enumerate(prs):
        number = pr.get("number") or 0
        if merged >= cap:
            log_fn(
                f"cap reached ({cap} merges); {len(prs) - pos} PR(s) left unattempted"
            )
            break

        hold = skip_reason(pr, gh=gh)
        if hold:
            log_fn(f"PR #{number}: skipped ({hold})")
            skipped += 1
            results.append({"number": number, "action": "skip", "reason": hold})
            continue

        try:
            view = get_pr_view(gh, number)
        except Exception as exc:
            log_fn(f"PR #{number}: skipped (cannot read PR: {exc})")
            skipped += 1
            results.append(
                {"number": number, "action": "skip", "reason": f"cannot read PR: {exc}"}
            )
            continue

        hold = skip_reason(view, gh=gh)
        if hold:
            log_fn(f"PR #{number}: skipped ({hold})")
            skipped += 1
            results.append({"number": number, "action": "skip", "reason": hold})
            continue

        ok, fails, facts = evaluate_merge_gates(
            view,
            allowed_bases=bases,
            require_approval=require_approval,
            allow_behind=True,
        )

        log_fn(
            f"PR #{number} gate facts: ci={facts.get('ci')} "
            f"merge={facts.get('merge')} review={facts.get('review')} "
            f"base={facts.get('base')}"
        )

        if not ok:
            reasons = "; ".join(
                f"{gate} is {actual} (need {want})" for gate, actual, want in fails
            )
            log_fn(f"PR #{number}: skipped ({reasons})")
            skipped += 1
            results.append(
                {"number": number, "action": "skip", "reason": reasons, "facts": facts}
            )
            continue

        try:
            do_merge(gh, number)
            log_fn(f"PR #{number}: merged")
            merged += 1
            results.append({"number": number, "action": "merge", "facts": facts})
        except Exception as exc:
            log_fn(f"PR #{number}: merge failed ({exc})")
            skipped += 1
            results.append(
                {
                    "number": number,
                    "action": "failed",
                    "error": str(exc),
                    "facts": facts,
                }
            )

    log_fn(
        f"done {gh}: merged={merged} skipped={skipped} "
        f"cap={cap} bases={','.join(bases)}"
    )
    return {
        "repo": name,
        "gh": gh,
        "merged": merged,
        "skipped": skipped,
        "results": results,
        "error": None,
    }


def evaluate_all(
    repos=DEFAULT_TARGET_REPOS,
    *,
    require_approval: bool = False,
    cap: int = MERGE_CAP,
    **kwargs,
) -> list[dict]:
    """Run merge evaluation across all specified target repositories."""
    outcomes = []
    for name, gh in repos:
        outcomes.append(
            evaluate_repo(
                name,
                gh,
                require_approval=require_approval,
                cap=cap,
                **kwargs,
            )
        )
    return outcomes


def periodic_loop(
    interval_seconds: int = 60,
    once: bool = False,
    repos=DEFAULT_TARGET_REPOS,
    stop_event=None,
    **kwargs,
) -> None:
    """Run periodic merge evaluation loop on a timer."""
    while True:
        evaluate_all(repos=repos, **kwargs)
        if once:
            break
        if stop_event is not None and stop_event.is_set():
            break
        time.sleep(interval_seconds)


def main(argv):
    parser = argparse.ArgumentParser(
        description="merge_gated — scheduled/periodic gated auto-merge"
    )
    parser.add_argument("--repo", help="Single repo short name")
    parser.add_argument("--gh", help="Single repo owner/name path")
    parser.add_argument("--loop", action="store_true", help="Run as periodic loop")
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Poll interval in seconds (default 60)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once across all target repos",
    )
    parser.add_argument(
        "--require-approval",
        action="store_true",
        default=False,
        help="Require explicit APPROVED review decision",
    )
    parser.add_argument(
        "--cap",
        type=int,
        default=MERGE_CAP,
        help=f"Max merges per pass (default {MERGE_CAP})",
    )

    args = parser.parse_args(argv[1:])

    if args.repo or args.gh:
        if not args.repo or not args.gh:
            print(
                "usage: merge_gated.py --repo NAME --gh OWNER/REPO",
                file=sys.stderr,
            )
            return 2
        if not NAME_RE.match(args.repo):
            print(f"bad repo name: {args.repo}", file=sys.stderr)
            return 2
        if not GH_RE.match(args.gh):
            print(f"bad gh path: {args.gh}", file=sys.stderr)
            return 2
        res = evaluate_repo(
            args.repo,
            args.gh,
            require_approval=args.require_approval,
            cap=args.cap,
        )
        return 1 if res.get("error") else 0

    if args.loop:
        periodic_loop(
            interval_seconds=args.interval,
            once=False,
            require_approval=args.require_approval,
            cap=args.cap,
        )
        return 0

    # Default: evaluate all target repos once
    evaluate_all(
        require_approval=args.require_approval,
        cap=args.cap,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
