"""Execute approved chat proposals server-side (#823).

``propose_action`` (deep-agent/deep_chat.py) only formats a proposal;
the chat approve path validated it via ``--validate-proposal`` and
dead-ended, so every approved proposal needed a human to retype the
action by hand. This module runs the three kinds exactly once after
approval, on the existing resume path (no new endpoints):

- issue:    ``gh issue create`` in the chat repo, returns the URL
- td:       one allowlisted td command via ``s5tools.run``, returns output
- dispatch: ``td worktree create`` + ``td agent run --detach``
  (the BoomTick local worktree session), returns the session id

Validation mirrors ``deep_chat.validate_proposal`` — the
approval-time gate, still authoritative — so the executor never runs
an unvalidated shape even if a pending entry was seeded by another
path. Requests that fail it are *skipped* (ledgered, never surfaced
as executions); every attempted execution returns an outcome dict
the caller ledgers and surfaces. Runners are module attributes so
tests can stub them; ``execute_*`` never raises for bad input or
runner failures (a spent approval must still answer, never 500).
"""

import json
import os
import re
import subprocess
import sys

from backend.core import get_checkout_path, validate_repo_name
from backend.exec.engine import validate_td_command
from backend.gh import run_gh, validate_gh

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "deep-agent"))

import s5tools  # noqa: E402

# Mirrors deep_chat.validate_proposal limits (the approval-time gate).
ISSUE_TITLE_LIMIT = 120
ISSUE_BODY_LIMIT = 4000
TD_COMMAND_LIMIT = 300
DISPATCH_TASK_LIMIT = 4000
# Cap on surfaced/ledgered execution detail (td output can be long).
OUTCOME_DETAIL_LIMIT = 1000
TD_RUN_TIMEOUT = 120
WORKTREE_TIMEOUT = 120
DISPATCH_TIMEOUT = 120

# Mirror of deep_chat._valid_branch (git-plausible branch names).
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _valid_branch(branch) -> bool:
    return (isinstance(branch, str)
            and 1 <= len(branch) <= 120
            and bool(BRANCH_RE.match(branch))
            and ".." not in branch
            and branch[0] not in "/-.")


def _capped(text, limit: int = OUTCOME_DETAIL_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated: showing first {limit} chars]"


def validate_issue(args: dict) -> tuple[str, str]:
    """(title, body) for an issue proposal. Raises ValueError."""
    if not isinstance(args, dict):
        raise ValueError("proposal must be an object")
    title = args.get("title")
    body = args.get("body")
    if (not isinstance(title, str) or not title.strip()
            or len(title) > ISSUE_TITLE_LIMIT):
        raise ValueError("issue needs a title (1-120 chars)")
    if (not isinstance(body, str) or not body.strip()
            or len(body) > ISSUE_BODY_LIMIT):
        raise ValueError("issue needs a body (1-4000 chars)")
    return title.strip(), body.strip()


def validate_td(args: dict) -> list:
    """argv tokens for a td proposal. Raises ValueError.

    Shape authority is the engine allowlist itself (the truth per
    #477), exactly like the approval-time validator derives from it.
    """
    if not isinstance(args, dict):
        raise ValueError("proposal must be an object")
    command = args.get("command")
    if (not isinstance(command, str) or not command.strip()
            or len(command) > TD_COMMAND_LIMIT):
        raise ValueError("td needs a command (1-300 chars)")
    tokens = validate_td_command(" ".join(command.split()))
    if tokens is None:
        raise ValueError("td command is not an allowlisted shape")
    return tokens


def validate_dispatch(args: dict) -> tuple[str, str, str]:
    """(gh, branch, task) for a dispatch proposal. Raises ValueError.

    Stricter than the approval-time gate in one way: the repo must
    be under arii/, the only org with server-side checkouts. Anything
    else could never resolve a worktree root, so it fails closed here
    instead of misresolving to an unrelated same-named checkout.
    """
    if not isinstance(args, dict):
        raise ValueError("proposal must be an object")
    repo = args.get("repo")
    try:
        gh = validate_gh(repo)
    except (ValueError, AttributeError):
        raise ValueError("dispatch needs a repo (owner/name)")
    if not gh.startswith("arii/"):
        raise ValueError("dispatch repo must be under arii/")
    try:
        validate_repo_name(gh.partition("/")[2])
    except ValueError:
        raise ValueError("dispatch needs a repo (owner/name)")
    branch = args.get("branch")
    branch = branch.strip() if isinstance(branch, str) else branch
    if not _valid_branch(branch):
        raise ValueError("dispatch needs a branch (1-120 chars, "
                         "[A-Za-z0-9._/-], no ..)")
    task = args.get("task")
    if (not isinstance(task, str) or not task.strip()
            or len(task) > DISPATCH_TASK_LIMIT):
        raise ValueError("dispatch needs a task (1-4000 chars)")
    return gh, branch, task.strip()


def run_gh_create_issue(gh: str, title: str, body: str) -> str:
    """File an issue; return its URL. Injectable for tests.

    Same argv shape as the /api/create-issue endpoint minus labels
    (proposals carry none, so the label-validation step is vacuous).
    """
    out = run_gh(gh, ["issue", "create", "--repo", gh,
                      "--title", title, "--body", body],
                 timeout=120)
    lines = out.strip().splitlines()
    return (lines[-1] if lines else "")[:500]


def run_td_command(tokens: list, repo: str) -> str:
    """Run validated td argv in the repo checkout. Injectable."""
    checkout = get_checkout_path(repo)
    return s5tools.run(list(tokens), cwd=str(checkout),
                       timeout=TD_RUN_TIMEOUT)


def _run_td_json(argv: list, timeout: int) -> dict:
    """Run td --json; return the payload. Raises RuntimeError."""
    try:
        proc = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, text=True)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"timed out after {timeout}s: {' '.join(argv[:3])}")
    except OSError as exc:
        raise RuntimeError(f"exec failed: {exc}")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(err[-1][:200] if err else "td command failed")
    try:
        payload = json.loads((proc.stdout or "").strip())
    except ValueError:
        raise RuntimeError("td returned unreadable JSON")
    if not isinstance(payload, dict) or payload.get("status") != "success":
        msg = payload.get("message", "td command failed") \
            if isinstance(payload, dict) else "td command failed"
        raise RuntimeError(str(msg)[:200])
    return payload


def create_managed_worktree(checkout_root: str,
                            base_branch: str) -> dict:
    """td worktree create; return {id, path, branch}. Injectable."""
    payload = _run_td_json(
        ["td", "--json", "worktree", "create",
         "--base", base_branch, "--path", str(checkout_root)],
        WORKTREE_TIMEOUT)
    wid, path, branch = (payload.get("worktreeId"),
                         payload.get("path"), payload.get("branch"))
    if not wid or not path or not branch:
        raise RuntimeError("td worktree create returned no worktree")
    return {"id": wid, "path": path, "branch": branch}


def start_worktree_session(worktree_path: str, brief: str,
                           repo: str) -> dict:
    """td agent run --detach; return {session_id, state}. Injectable."""
    payload = _run_td_json(
        ["td", "--json", "agent", "run", "--worktree", worktree_path,
         "--brief", brief, "-r", repo, "--detach"],
        DISPATCH_TIMEOUT)
    session = payload.get("session") or {}
    sid = session.get("sessionId")
    if not sid:
        raise RuntimeError("td agent run returned no session")
    return {"session_id": sid, "state": session.get("state", "")}


def run_dispatch(gh: str, branch: str, task: str) -> dict:
    """Create a worktree, dispatch a session. Returns
    {session_id, state, worktree, branch}. Injectable for tests."""
    checkout = get_checkout_path(gh.partition("/")[2])
    wt = create_managed_worktree(str(checkout), branch)
    started = start_worktree_session(wt["path"], task, gh)
    return {"session_id": started["session_id"],
            "state": started["state"],
            "worktree": wt["path"], "branch": wt["branch"]}


def execute_proposal(args: dict, session_gh: str, session_repo: str,
                     proposal_id: str) -> dict:
    """Run one approved request. Never raises.

    Returns an outcome {"proposal_id", "kind", "target", "ok",
    "detail", "skipped"}: skipped=True when the shape fails the
    approval-time mirror (ledger-only, never surfaced as an
    execution); otherwise ok/detail carry the attempt's result.
    """
    kind = args.get("kind") if isinstance(args, dict) else None
    if kind == "issue":
        try:
            title, body = validate_issue(args)
        except ValueError as exc:
            return {"proposal_id": proposal_id, "kind": "issue",
                    "target": "", "ok": False, "detail": f"skipped: {exc}",
                    "skipped": True}
        try:
            url = run_gh_create_issue(session_gh, title, body)
        except Exception as exc:
            return {"proposal_id": proposal_id, "kind": "issue",
                    "target": title[:100], "ok": False,
                    "detail": _capped(f"issue create failed: {exc}"),
                    "skipped": False}
        return {"proposal_id": proposal_id, "kind": "issue",
                "target": title[:100], "ok": True,
                "detail": url or "(no url returned)", "skipped": False}
    if kind == "td":
        try:
            tokens = validate_td(args)
        except ValueError as exc:
            return {"proposal_id": proposal_id, "kind": "td",
                    "target": "", "ok": False, "detail": f"skipped: {exc}",
                    "skipped": True}
        command = " ".join(tokens)
        try:
            out = run_td_command(tokens, session_repo)
        except Exception as exc:
            return {"proposal_id": proposal_id, "kind": "td",
                    "target": command, "ok": False,
                    "detail": _capped(f"td run failed: {exc}"),
                    "skipped": False}
        return {"proposal_id": proposal_id, "kind": "td",
                "target": command, "ok": True,
                "detail": _capped(out.strip() or "(no output)"),
                "skipped": False}
    if kind == "dispatch":
        try:
            gh, branch, task = validate_dispatch(args)
        except ValueError as exc:
            return {"proposal_id": proposal_id, "kind": "dispatch",
                    "target": "", "ok": False, "detail": f"skipped: {exc}",
                    "skipped": True}
        target = f"{gh}@{branch}"
        try:
            started = run_dispatch(gh, branch, task)
        except Exception as exc:
            return {"proposal_id": proposal_id, "kind": "dispatch",
                    "target": target, "ok": False,
                    "detail": _capped(f"dispatch failed: {exc}"),
                    "skipped": False}
        return {"proposal_id": proposal_id, "kind": "dispatch",
                "target": target, "ok": True,
                "detail": (f"session {started['session_id']} "
                           f"on {started['branch']} "
                           f"({started['worktree']})"),
                "skipped": False}
    return {"proposal_id": proposal_id, "kind": str(kind),
            "target": "", "ok": False,
            "detail": "skipped: kind must be 'issue', 'td', or 'dispatch'",
            "skipped": True}


def execute_proposals(items: list, session_id: str, session_gh: str,
                      session_repo: str) -> list:
    """Run approved (index, args) items. Returns one outcome per item,
    in order (attempted executions plus skipped shapes; the caller
    surfaces the attempted ones and marks every index terminal so a
    retry never re-runs).
    """
    return [execute_proposal(args, session_gh, session_repo,
                             f"{session_id}:{index}")
            for index, args in items]


def format_suffix(outcomes: list) -> str:
    """Chat-text suffix surfacing attempted executions ("" when none).

    Skipped shapes stay out of the reply: they never ran, so there is
    nothing to show (the ledger keeps the audit trail).
    """
    attempted = [o for o in outcomes if not o.get("skipped")]
    if not attempted:
        return ""
    lines = ["", "", "---"]
    for o in attempted:
        if o["kind"] == "issue":
            lines.append(f"✓ issue filed: {o['detail']}" if o["ok"]
                         else f"✗ issue failed: {o['detail']}")
        elif o["kind"] == "td":
            lines.append(f"✓ td `{o['target']}`:\n{o['detail']}"
                         if o["ok"]
                         else f"✗ td `{o['target']}` failed: {o['detail']}")
        else:
            lines.append(f"✓ dispatch started: {o['detail']}" if o["ok"]
                         else f"✗ dispatch failed: {o['detail']}")
    return "\n".join(lines)
