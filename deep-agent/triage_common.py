"""Shared stdlib-only helpers for the triage agents.

triage_fanout.py (Nebius orchestrator + one scout subagent per repo) and
triage_explainer.py (local single-repo explainer) are two phases of the
same triage lifecycle. Both validate owner/name repos, shell out to
`td agent suggest` for deterministic counts, and count tool calls the
same way; that shared logic lives here so the callers stay thin.

Stdlib only (plus s5tools, also stdlib-only): system-python tests
import this without the deep-agent venv. Do NOT import langchain here.
"""

import subprocess

import s5tools

valid_repo = s5tools.valid_repo

REJECTED_REPO = "REJECTED: repo must look like owner/name"
TRIAGE_TIMEOUT = 120
TRIAGE_OUTPUT_LIMIT = 1500


def fetch_triage_counts(repo):
    """Run `td agent suggest` for one repo; capped stdout+stderr text."""
    if not valid_repo(repo):
        return REJECTED_REPO
    proc = subprocess.run(
        ["td", "agent", "suggest", "-r", repo],
        capture_output=True,
        text=True,
        timeout=TRIAGE_TIMEOUT,
    )
    return (proc.stdout + proc.stderr).strip()[:TRIAGE_OUTPUT_LIMIT]


def count_tool_calls(messages, tool_name=None):
    """Count tool calls across agent messages.

    With tool_name, counts only calls to that tool; without, all calls.
    Messages are duck-typed (getattr), so fakes work in tests.
    """
    total = 0
    for message in messages or []:
        for call in getattr(message, "tool_calls", None) or []:
            if tool_name is None or call.get("name") == tool_name:
                total += 1
    return total
