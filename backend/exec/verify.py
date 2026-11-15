"""Verify-before-write for chat proposals (Buttons rebuild D3).

Before an approval card renders, the chat router shape-validates each
proposed action (deep_chat --validate-proposal: proposal kinds, enum
checks against the engine allowlists) and then runs THIS content
pass: does the proposal point at real things — the target repo, the
quoted PR number? A proposal that fails verification never shows a
card; the router auto-rejects with the specific reason so the model
can re-propose instead of guessing. The gate is provider-blind: local
and cloud proposals pass through the same checks — verify-before-write
is not a local-only trust downgrade.

Deliberately NOT checked (the over-strict-gate risk from the spec):
branch existence for dispatch/coder — a brand-new branch name is a
legitimate target, the coder creates it. Only git-plausible branch
shape is enforced, and that lives in backend/exec/validate.py behind
shape validation. A failed verify names the failed check first, so
the re-prompt can carry the exact reason.

Stdlib-only, like validate.py/context_budget.py. The gh calls are
module-level functions so tests can inject fakes at the verify_proposal
seam (repo_exists/pr_exists parameters).
"""

from __future__ import annotations

import subprocess

# Every rejection reason starts with this, then names the failed
# check — the shape the re-prompt relies on (tests pin it).
VERIFY_REASON_PREFIX = "VERIFY-FAILED:"

# Bounded: a verify pass must never hold a turn open.
GH_TIMEOUT = 20


def _gh(args: list[str]) -> tuple[bool, str]:
    """One gh call. Returns (ok, output_or_error). Never raises."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except Exception as exc:
        return False, f"gh unavailable: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        return False, detail[:200] if detail else f"gh exited {proc.returncode}"
    return True, proc.stdout


def repo_exists(repo: str) -> tuple[bool, str]:
    """The target repo is real and visible to gh."""
    ok, out = _gh(["repo", "view", repo, "--json", "name"])
    if ok:
        return True, out
    return False, f"repo {repo!r} not found or not visible ({out})"


def pr_exists(repo: str, number: int) -> tuple[bool, str]:
    """The quoted PR number is real in the target repo (any state)."""
    ok, out = _gh(
        ["pr", "view", str(number), "-R", repo, "--json", "number", "--state", "all"]
    )
    if ok:
        return True, out
    return False, f"PR #{number} not found in {repo} ({out})"


def _fail(check: str, detail: str) -> tuple[bool, str]:
    return False, f"{VERIFY_REASON_PREFIX}{check}: {detail}"


def verify_proposal(args: dict, turn_repo: str) -> tuple[bool, str]:
    """Content sanity for one shape-valid proposal.

    args is the raw action_request (already through shape validation);
    turn_repo is the repo the chat turn runs against (issue proposals
    carry no repo of their own). Returns (True, "") or
    (False, "VERIFY-FAILED: <check>: <detail>"). Never raises; a
    checker that cannot run fails closed — an unverified proposal
    never renders a card.
    """
    if not isinstance(args, dict):
        return _fail("shape", "proposal must be an object")
    kind = args.get("kind")
    if kind == "issue":
        # The only content target is the turn's repo; title/body
        # sanity is shape validation's job.
        ok, detail = repo_exists(turn_repo)
        return (True, "") if ok else _fail("repo_exists", detail)
    if kind == "td":
        # The td allowlist (engine.ALLOWED_TD_COMMANDS) IS the content
        # gate, and shape validation already applied it. Nothing left
        # to verify without executing.
        return True, ""
    if kind in ("dispatch", "coder"):
        repo = args.get("repo")
        ok, detail = repo_exists(str(repo))
        return (True, "") if ok else _fail("repo_exists", detail)
    if kind in ("comment", "review"):
        repo = str(args.get("repo"))
        ok, detail = repo_exists(repo)
        if not ok:
            return _fail("repo_exists", detail)
        ok, detail = pr_exists(repo, args.get("pr_number"))
        return (True, "") if ok else _fail("pr_exists", detail)
    # Unknown kinds never reach verify (shape validation rejects
    # them); pass through so the gate stays an addition, never a
    # second validator that can drift from the first.
    return True, ""
