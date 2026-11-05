#!/usr/bin/env python3
"""merge_gated — scheduled gated auto-merge for one repo (#867).

Opt-in scheduled action (engine action "merge-approved-gated"): merges
open PRs oldest-first when ALL machine-verifiable gates hold fresh at
fire time — approved (APPROVED), green (passing), mergeable
(clean/behind), target base in the allowlist (default: the repo's live
default branch) — up to MERGE_CAP per fire. Anything else is skipped
with the reason printed (run output is ledgered by the run path), so a
fire with zero qualifying PRs merges nothing and says why.

Gate semantics are single-sourced in backend/pr_gates.py (shared with
the manual merge path); this script only drives list → gate → merge.
gh itself enforces branch rules on every merge; no approve/review
bypass, no force-merge, same squash + delete-branch argv as manual.

Usage: merge_gated.py --repo NAME --gh OWNER/REPO
Exit 0 unless gh infrastructure fails (auth/network): per-PR skips and
per-PR merge failures are outcomes with reasons, not fire failures.
"""

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from backend.pr_gates import (MERGE_CAP, default_branch,  # noqa: E402
                              evaluate_merge_gates, merge_pr, open_prs,
                              pr_gate_view)

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
GH_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def main(argv):
    if "--repo" not in argv or "--gh" not in argv:
        print("usage: merge_gated.py --repo NAME --gh OWNER/REPO",
              file=sys.stderr)
        return 2
    name = argv[argv.index("--repo") + 1] \
        if argv.index("--repo") + 1 < len(argv) else ""
    if not NAME_RE.match(name):
        print(f"bad repo name: {name}", file=sys.stderr)
        return 2
    gh = argv[argv.index("--gh") + 1] \
        if argv.index("--gh") + 1 < len(argv) else ""
    if not GH_RE.match(gh):
        print(f"bad gh path: {gh}", file=sys.stderr)
        return 2
    try:
        bases = [default_branch(gh)]
    except RuntimeError as exc:
        print(f"cannot resolve default branch for {gh}: {exc}; "
              "merged nothing", file=sys.stderr)
        return 1
    try:
        prs = open_prs(gh)
    except RuntimeError as exc:
        print(f"cannot list open PRs for {gh}: {exc}; merged nothing",
              file=sys.stderr)
        return 1
    if not prs:
        print(f"no open PRs in {gh}; merged nothing")
        return 0
    # Oldest first (open_prs already sorts; enforce here too so the
    # cap always takes the oldest qualifying PRs).
    prs = sorted(prs, key=lambda p: p.get("number") or 0)
    merged = 0
    skipped = 0
    for pos, pr in enumerate(prs):
        number = pr.get("number") or 0
        if merged >= MERGE_CAP:
            print(f"cap reached ({MERGE_CAP} merges); "
                  f"{len(prs) - pos} PR(s) left unattempted")
            break
        try:
            view = pr_gate_view(gh, number)
        except RuntimeError as exc:
            print(f"PR #{number}: skipped (cannot read PR: {exc})")
            skipped += 1
            continue
        ok, fails, _facts = evaluate_merge_gates(
            view, allowed_bases=bases, require_approval=True,
            allow_behind=True)
        if not ok:
            reasons = "; ".join(
                f"{gate} is {actual} (need {want})"
                for gate, actual, want in fails)
            print(f"PR #{number}: skipped ({reasons})")
            skipped += 1
            continue
        try:
            merge_pr(gh, number)
        except RuntimeError as exc:
            print(f"PR #{number}: merge failed ({exc})")
            skipped += 1
            continue
        print(f"PR #{number}: merged")
        merged += 1
    print(f"done: merged={merged} skipped={skipped} "
          f"cap={MERGE_CAP} bases={','.join(bases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
