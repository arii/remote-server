"""Single-sourced proposal validation (D4).

One home for the shape checks the chat path (deep_chat.py) and the
execution path (backend/proposals.py) both apply to proposed
actions. The engine allowlists (backend/exec/engine.py:
ALLOWED_ACTIONS, ALLOWED_TD_COMMANDS, validate_td_command) stay
where they are — this module adds the git-shape validators both
runners used to hand-mirror, so the copies can never drift again.

Stdlib-only: deep-agent imports it lazily, same contract as
context_budget.py.
"""

from __future__ import annotations

import re

# Git-plausible branch names: charset, length, no dot-dot, no
# leading slash/dot/dash.
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
BRANCH_MAX_LEN = 120


def valid_branch(branch) -> bool:
    """Git-plausible branch name: charset, length, no dot-dot."""
    return (
        isinstance(branch, str)
        and 1 <= len(branch) <= BRANCH_MAX_LEN
        and bool(BRANCH_RE.match(branch))
        and ".." not in branch
        and branch[0] not in "/-."
    )


def valid_pr_number(value) -> bool:
    """Positive-int PR number (bools are not PRs)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
