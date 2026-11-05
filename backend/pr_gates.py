"""Single-sourced PR merge gating (stdlib-only).

Both the manual merge path (backend/api/routers/pulls.py::_pr_action)
and the scheduled gated-merge action (backend/merge_gated.py)
evaluate through here, so the gate semantics can never drift (the
#477 lesson: never a copy). Stdlib-only: the scheduled step runs
under system python3 without backend deps.
"""

from backend.gh import run_gh, run_gh_json

# Max merges per scheduled fire (oldest first).
MERGE_CAP = 5

FAILING_CONCLUSIONS = {"FAILURE", "CANCELLED", "TIMED_OUT",
                         "ACTION_REQUIRED"}


def ci_label(rollup) -> str:
    """Port of backend/briefing_summary.py::ci_summary label."""
    if rollup is None:
        return "unknown"
    if not rollup:
        return "no checks"
    if any(isinstance(c, dict) and c.get("conclusion")
           in FAILING_CONCLUSIONS for c in rollup):
        return "failing"
    if any(isinstance(c, dict) and c.get("status") != "COMPLETED"
           for c in rollup):
        return "pending"
    return "passing"


def merge_argv(gh: str, pr_number: int) -> list:
    """The one merge command (squash + delete-branch)."""
    return ["pr", "merge", str(pr_number), "--repo", gh,
            "--squash", "--delete-branch"]


def evaluate_merge_gates(view: dict, *, allowed_bases=None,
                         require_approval=False,
                         allow_behind=False) -> tuple:
    """(ok, fails, facts) for a pr-view dict.

    fails = [(gate, actual, want)] in ci/merge/review/base check
    order; facts = {"ci", "merge", "review", "base"}. Callers
    format their own messages: the manual path keeps its exact
    legacy strings, the scheduled path ledgers per-gate reasons.
    """
    view = view if isinstance(view, dict) else {}
    ci = ci_label(view.get("statusCheckRollup"))
    merge = str(view.get("mergeStateStatus") or "?").lower()
    review = str(view.get("reviewDecision") or "")
    base = str(view.get("baseRefName") or "")
    fails = []
    if ci != "passing":
        fails.append(("ci", ci, "passing"))
    ok_states = {"clean", "behind"} if allow_behind else {"clean"}
    if merge not in ok_states:
        want = "clean/behind" if allow_behind else "clean"
        fails.append(("merge", merge, want))
    if require_approval:
        if review != "APPROVED":
            fails.append(("review", review or "(none)", "APPROVED"))
    else:
        if review == "REVIEW_REQUIRED":
            fails.append(("review", review, "approved-or-unrequired"))
        elif review == "CHANGES_REQUESTED":
            fails.append(("review", review, "no requested changes"))
    if allowed_bases is not None and base not in allowed_bases:
        fails.append(("base", base or "(none)",
                      "|".join(allowed_bases)))
    facts = {"ci": ci, "merge": merge, "review": review, "base": base}
    return (not fails), fails, facts


def default_branch(gh: str, runner=None) -> str:
    """Live default branch (main/leader) via gh. Raises RuntimeError."""
    # `gh repo view` takes no --repo flag (positional only), which
    # violates run_gh's argv-scoping rule; the repos/ api URL carries
    # the same data and satisfies it.
    ref = run_gh_json(gh, ["api", f"repos/{gh}"], runner=runner)
    name = ref.get("default_branch")
    if not name:
        raise RuntimeError("gh returned no default branch")
    return name


PR_LIST_MIN_FIELDS = "number,baseRefName,updatedAt"


def open_prs(gh: str, limit: int = 50, runner=None) -> list:
    """Open PRs oldest-first. Raises RuntimeError on gh failure."""
    prs = run_gh_json(gh, ["pr", "list", "--state", "open",
                           "--repo", gh, "--json", PR_LIST_MIN_FIELDS,
                           "--limit", str(limit)],
                      runner=runner)
    if not isinstance(prs, list):
        raise RuntimeError("gh returned no PR list")
    return sorted(prs, key=lambda p: p.get("number") or 0)


GATE_VIEW_FIELDS = ("statusCheckRollup,mergeStateStatus,"
                    "reviewDecision,baseRefName")


def pr_gate_view(gh: str, pr_number: int, runner=None) -> dict:
    """Fresh gate fields for one PR. Raises RuntimeError."""
    return run_gh_json(gh, ["pr", "view", str(pr_number), "--repo", gh,
                            "--json", GATE_VIEW_FIELDS],
                       timeout=60, runner=runner)


def merge_pr(gh: str, pr_number: int, runner=None) -> str:
    """Run the merge argv; return gh output. Raises RuntimeError."""
    return run_gh(gh, merge_argv(gh, pr_number), runner=runner)
