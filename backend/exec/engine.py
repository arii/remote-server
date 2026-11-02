"""Execution engine ported from buttons/server.py.

Faithful port of the allowlisted command execution surface:
allowed td commands/shapes, run-plan allowlist, timeouts, and
output truncation. No new execution surface is added here: every
command run goes through the same allowlists as the original.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import threading
import time
import uuid

# Exact port of buttons/server.py::MAX_OUTPUT: keep the tail, model
# pulls are verbose.
MAX_OUTPUT = 200_000

# Exact port of buttons/server.py ALLOWED_TD_COMMANDS.
ALLOWED_TD_COMMANDS = [
    "td --json agent fix-ci --pr N --execute",
    "td gh read-pr-reviews --pr N",
    "td gh read-pr-comments --pr N",
    "td gh resolve-conflicts --pr N",
    "td context-warm",
    "td --json preflight",
    "td --json agent list-sessions",
    "td --json agent run-feedback-check",
]
ALLOWED_TD_SHAPES = [cmd.split() for cmd in ALLOWED_TD_COMMANDS]

# Exact port of the run-plan allowlist in buttons/server.py
# (handler for /buttons/api/run-plan).
ALLOWED_PLANS = ("issue-audit", "workflow-audit", "review", "aggregation")

# Exact port of the timeouts used by buttons/server.py:
# _td_run default timeout, and the run-plan call timeout.
TD_RUN_TIMEOUT = 120
PLAN_TIMEOUT = 300

# Action names ported from buttons/server.py::ACTIONS (keys only; the
# backend owns no docker transport, so steps are resolved by the
# injected runner in tests and by _td_run in production).
ALLOWED_ACTIONS = (
    "health",
    "status",
    "deploy-buttons",
    "recreate",
    "recreate-code",
    "logs",
    "briefing-td",
    "sync-agents",
    "nudge-agents",
    "warm-context",
    "preflight",
    "create-plan",
    "run-td",
    "merge-approved",
)

TARGET_RE = re.compile(r"[A-Za-z0-9._/-]{1,64}\Z")


def validate_td_command(text):
    """argv tokens for an allowlisted td command, or None.

    Exact port of buttons/server.py::_validate_td_command: exact
    shapes only, PR numbers digits. Anything else is rejected.
    """
    try:
        tokens = shlex.split(text or "")
    except Exception:
        return None
    if not tokens or tokens[0] != "td":
        return None
    for shape in ALLOWED_TD_SHAPES:
        if len(tokens) != len(shape):
            continue
        ok = True
        for want, got in zip(shape, tokens):
            if want == "N":
                if not got.isdigit() or int(got) <= 0:
                    ok = False
                    break
            elif want != got:
                ok = False
                break
        if ok:
            return tokens
    return None


def truncate_tail(text):
    """Keep the tail of oversized output. Exact port of the
    buttons/server.py::_append truncation (output[-MAX_OUTPUT:])."""
    if len(text) > MAX_OUTPUT:
        return text[-MAX_OUTPUT:]
    return text


def append_output(run, text):
    """Append chunk to a run record with tail truncation. Exact port
    of buttons/server.py::_append."""
    run["output"] += text
    run["last_output_at"] = time.time()
    if len(run["output"]) > MAX_OUTPUT:
        run["output"] = run["output"][-MAX_OUTPUT:]


def build_plan_argv(plan, pr="", prs=None, target=""):
    """Build the td plan argv. Exact port of the argv construction in
    buttons/server.py run-plan handler. Returns (argv, error)."""
    if plan not in ALLOWED_PLANS:
        return None, "bad repo or plan"
    argv = ["td", "--json", "agent"]
    if plan == "issue-audit":
        argv += ["plan-issue-audit", "--all-open"]
    elif plan == "workflow-audit":
        argv += ["plan-workflow-audit"]
    elif plan == "review":
        pr = str(pr)
        if not pr.isdigit() or int(pr) <= 0:
            return None, "bad pr"
        argv += ["plan-review", "--pr", pr]
    else:  # aggregation
        prs = prs or []
        target = str(target).strip()
        if (
            not prs
            or len(prs) > 10
            or not all(str(p).isdigit() and int(p) > 0 for p in prs)
            or not TARGET_RE.fullmatch(target)
        ):
            return None, "bad prs or target"
        argv += ["plan-aggregation"]
        for p in prs:
            argv += ["--pr", str(int(p))]
        argv += ["--target", target]
    return argv, None


def td_run(cmd, timeout=TD_RUN_TIMEOUT, runner=None):
    """Run one fixed td/gh command string; return stdout.

    Exact port of buttons/server.py::_td_run semantics: raises
    RuntimeError with the last stderr line on failure. The command
    string is built from validated values only. ``runner`` is an
    injectable subprocess-like module (for tests); defaults to the
    real subprocess.
    """
    run = runner or subprocess
    proc = run.run(
        ["bash", "-c", cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or "").strip().splitlines()[-1]
            if (proc.stderr or "").strip()
            else "command failed"
        )
    return proc.stdout


class RunRegistry:
    """Single-flight run records + job list. Ports the runs/jobs
    dicts, locks, and clear semantics of buttons/server.py."""

    def __init__(self):
        self._lock = threading.Lock()
        self.runs = {}
        self.active_id = None
        self.jobs = {}
        self._seq = 0

    def start_run(self, action, repo="remote-server", td_argv=None):
        """Create a run for an allowlisted action. Exact port of
        buttons/server.py::_start_run validation + single-flight."""
        if action not in ALLOWED_ACTIONS:
            return None, "unknown action"
        with self._lock:
            if self.active_id is not None:
                return None, "a run is already in progress"
            run_id = uuid.uuid4().hex[:12]
            now = time.time()
            run = {
                "id": run_id,
                "action": action,
                "repo": repo,
                "done": False,
                "exit_code": None,
                "output": "",
                "seq": self._seq,
                "started_at": now,
                "finished_at": None,
                "last_output_at": now,
            }
            if td_argv is not None:
                run["td_argv"] = td_argv
            self._seq += 1
            self.runs[run_id] = run
            self.active_id = run_id
        return run_id, None

    def finish_run(self, run_id, exit_code):
        with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                return
            run["exit_code"] = exit_code
            run["done"] = True
            run["finished_at"] = time.time()
            if self.active_id == run_id:
                self.active_id = None

    def clear_run(self, run_id=None):
        """Clear stored output only. Exact port of the /api/clear
        handler: with no id, clears the active or newest run."""
        with self._lock:
            run = self.runs.get(run_id) if run_id else None
            if run is None:
                if self.active_id in self.runs:
                    run = self.runs[self.active_id]
                elif self.runs:
                    run = max(self.runs.values(), key=lambda r: r["seq"])
            if run is None:
                return None, "no run to clear"
            run["output"] = ""
            run["last_output_at"] = time.time()
            return run["id"], None

    def clear_jobs(self, all_jobs=False):
        """Clear terminal jobs (or all). Exact port of
        buttons/server.py::_jobs_clear. Returns count removed."""
        with self._lock:
            to_del = [
                jid
                for jid, job in self.jobs.items()
                if all_jobs or job.get("state") == "terminal"
            ]
            for jid in to_del:
                del self.jobs[jid]
        return len(to_del)

    def list_jobs(self):
        with self._lock:
            out = list(self.jobs.values())
        out.sort(key=lambda j: j.get("created_at", 0), reverse=True)
        return out
