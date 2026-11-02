"""Shared helpers for the deep-agent eval gates (eval_ask.py, eval_chat.py).

Stdlib only: system-python tests import this without the venv.
Covers the three flakiness sources every gate needs handled the same
way: interpreter resolution, ledger correlation (only rows written by
the run under test count), and invalid-vs-failed verdicts (a dead
model or a timeout is an invalid run, not a failed agent).
"""

import json
import os
import re
import subprocess
import sys

DEFAULT_TIMEOUT = 900

# A traceback in child output means the harness/script broke, not that
# the agent answered badly. Always an invalid run, never a fail.
TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")

# Markers that mean "the model was unreachable", not "the agent failed".
# Checked against combined child output when a run exits nonzero/empty.
INVALID_RE = re.compile(
    r"Ollama unreachable|no model is available|no model .* available|"
    r"Connection refused|ConnectError|ConnectTimeout|Failed to connect|"
    r"assistant failed|ledger record failed",
    re.I,
)


def resolve_exe(here):
    """Interpreter for child agent scripts.

    EVAL_PYTHON wins (tests/CI pin this); otherwise the deep-agent venv
    when present; otherwise the current interpreter.
    """
    override = os.environ.get("EVAL_PYTHON", "").strip()
    if override:
        return override
    venv = os.path.join(here, ".venv", "bin", "python")
    if os.path.exists(venv):
        return venv
    return sys.executable


def eval_timeout(default=DEFAULT_TIMEOUT):
    """Per-case child timeout in seconds (EVAL_TIMEOUT override)."""
    try:
        value = int(os.environ.get("EVAL_TIMEOUT", str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def ledger_path(here):
    """Ledger file the gate correlates against (EVAL_LEDGER override)."""
    return os.environ.get("EVAL_LEDGER") or os.path.join(here, "ledger.jsonl")


def ledger_line_count(path):
    """Number of lines currently in the ledger (0 when missing)."""
    try:
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def new_ledger_entries(path, since, script, thread=None):
    """Ledger rows appended after line `since` for one script run.

    Correlating by (line offset, script, thread) instead of "last row
    for this script" keeps parallel runs and older entries from being
    attributed to the case under test. Corrupt lines are skipped.
    """
    entries = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return entries
    for line in lines[since:]:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("script") != script:
            continue
        if thread is not None and row.get("thread_id") != thread:
            continue
        entries.append(row)
    return entries


def entry_tools(entry):
    """Tool names recorded in one ledger entry."""
    return [
        c.get("name", "")
        for c in entry.get("tool_calls", [])
        if isinstance(c, dict)
    ]


def run_child(argv, timeout):
    """Run a child agent script. Returns (code, stdout, stderr, timed_out)."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        if isinstance(err, bytes):
            err = err.decode(errors="replace")
        return None, out, err, True
    return proc.returncode, proc.stdout or "", proc.stderr or "", False


def env_banner(keys):
    """One-line 'k=v' snapshot of the env knobs a gate depends on."""
    return "env: " + " ".join(
        f"{k}={os.environ.get(k, '')!r}" for k in keys
    )


def is_invalid_output(out):
    """True when child output shows an infra/model failure, not an agent one."""
    return bool(TRACEBACK_RE.search(out) or INVALID_RE.search(out))
