"""Shared deterministic helpers for S5 agent slices (deep-agent/).

Stdlib only: system-python tests import this without the venv. Wraps the
subprocess discipline every slice needs — always a timeout, never a shell,
validation before execution, capped outputs — plus repo sandboxing so tools
cannot escape their checkout.

New slices: import these instead of re-implementing gh/td/file access.
Do NOT import langchain here (keeps system-python tests working).
Do NOT put secrets in here; the model router (`model_router.py`) and the
ledger (`ledger.py`) own their own concerns.
"""

import json
import os
import re
import subprocess

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
CHECKOUTS = {"arii/remote-server": "/srv/remote-server"}

DEFAULT_TIMEOUT = 120
MAX_OUT = 4000


def valid_repo(repo):
    """True for owner/name strings safe to interpolate into commands."""
    return isinstance(repo, str) and bool(REPO_RE.match(repo))


def valid_number(value):
    """Positive int from str digits or int, else None. Rejects bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def checkout_for(repo):
    """Checkout path for a repo; raises ValueError on bad input."""
    if not valid_repo(repo):
        raise ValueError(f"bad repo: {repo!r}")
    if repo in CHECKOUTS:
        return CHECKOUTS[repo]
    return f"/srv/repos/{repo.split('/', 1)[1]}"


def run(argv, cwd=None, timeout=DEFAULT_TIMEOUT):
    """Run argv (no shell). Returns stripped stdout; raises RuntimeError
    with capped stderr on failure or timeout."""
    if not isinstance(argv, list) or not argv or not all(
        isinstance(a, str) for a in argv
    ):
        raise ValueError("argv must be a non-empty list of str")
    try:
        proc = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"timed out after {timeout}s: {' '.join(argv[:3])}")
    except OSError as exc:
        raise RuntimeError(f"exec failed: {exc}")
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()[:200]
        raise RuntimeError(f"rc={proc.returncode}: {' '.join(argv[:3])}: {err}")
    return (proc.stdout or "").strip()


def run_json(argv, cwd=None, timeout=DEFAULT_TIMEOUT):
    """run() plus JSON parsing; raises RuntimeError on bad JSON."""
    out = run(argv, cwd=cwd, timeout=timeout)
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f"bad json from {' '.join(argv[:2])}: {out[:200]}")


def gh_json(args, timeout=DEFAULT_TIMEOUT):
    """gh CLI returning parsed JSON. Caller passes -R/--repo in args."""
    return run_json(["gh", *args], timeout=timeout)


def td(args, cwd, timeout=300):
    """td CLI in a repo checkout. Returns stdout text (parse JSON yourself
    when the command supports --json)."""
    if not isinstance(cwd, str) or not os.path.isdir(cwd):
        raise ValueError(f"bad cwd: {cwd!r}")
    return run(["td", *args], cwd=cwd, timeout=timeout)


def list_dir(checkout, path="", limit=200):
    """List names in a checkout directory. Returns newline-separated
    names or an error string (routine outcomes, not exceptions)."""
    full = os.path.realpath(os.path.join(checkout or "", path or ""))
    if full != checkout and not full.startswith(checkout + os.sep):
        return "REJECTED: path escapes the checkout"
    try:
        names = sorted(os.listdir(full))
    except OSError as exc:
        return f"list failed: {exc}"
    return "\n".join(names[:limit]) or "(empty)"


def read_repo_file(checkout, path, limit=3000):
    """Read a repo-relative file. Returns content or a not-found/REJECTED
    string (routine outcomes, not exceptions)."""
    full = os.path.realpath(os.path.join(checkout or "", path or ""))
    if full != checkout and not full.startswith(checkout + os.sep):
        return "REJECTED: path escapes the checkout"
    if not os.path.isfile(full):
        return "not found: no such file"
    try:
        with open(full, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError as exc:
        return f"read failed: {exc}"


def grep_repo(checkout, pattern, limit=2000):
    """Literal grep over a checkout (skips .git/node_modules/.venv and
    generated/agent dirs). Returns matches or a no-matches/REJECTED
    string."""
    if not pattern or len(pattern) > 120:
        return "REJECTED: bad pattern"
    try:
        proc = subprocess.run(
            ["grep", "-rn", "-m", "20", "--exclude-dir=.git",
             "--exclude-dir=node_modules", "--exclude-dir=.venv",
             "--exclude-dir=__pycache__", "--exclude-dir=.boomtick",
             "--exclude-dir=chroma_db", "--exclude-dir=.claude",
             "--exclude-dir=.muse",
             "--exclude=ledger.jsonl",
             "--", pattern, "."],
            cwd=checkout,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"grep failed: {exc}"
    out = (proc.stdout or "").strip()
    return out[:limit] or "no matches"
