"""Execution engine ported from buttons/server.py.

Faithful port of the allowlisted command execution surface:
allowed td commands/shapes, run-plan allowlist, action steps,
timeouts, and output truncation. Every command run goes through
the same allowlists as the original. Jules actions are cut (Jules
is dead); the Pi transcript step of logs is cut (Pi is dead).
"""

from __future__ import annotations

import os
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

# Action names ported from buttons/server.py::ACTIONS (Jules actions
# cut: sync-agents/nudge-agents are dead with Jules).
ALLOWED_ACTIONS = (
    "health",
    "status",
    "deploy-buttons",
    "recreate",
    "recreate-code",
    "logs",
    "briefing-td",
    "warm-context",
    "preflight",
    "create-plan",
    "run-td",
    "merge-approved",
)

# Actions whose original has confirm: True. The router requires
# {intent: "jobs-run", confirmed: True} for these.
DESTRUCTIVE_ACTIONS = (
    "deploy-buttons",
    "recreate",
    "recreate-code",
    "create-plan",
    "merge-approved",
)

# Actions scoped to the picked repo (original REPO_SCOPED_ACTIONS
# plus run-td, minus the dead Jules pair).
REPO_SCOPED_ACTIONS = (
    "briefing-td",
    "warm-context",
    "preflight",
    "create-plan",
    "merge-approved",
    "run-td",
)

ENGINE_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def stack_dir() -> str:
    return os.environ.get("BUTTONS_STACK", ENGINE_ROOT)


def compose_file() -> str:
    return os.environ.get("BUTTONS_COMPOSE_FILE",
                           os.path.join(stack_dir(), "docker-compose.yml"))


def compose_argv(*args: str) -> list:
    return ["docker", "compose", "-f", compose_file(), *args]


def exec_argv(shell: str) -> list:
    return compose_argv("exec", "-T", "terminal", "bash", "-c", shell)


def td_prefix(cwd: str) -> str:
    """Shell prefix ported from buttons/server.py::_td_prefix."""
    return ("set -a && [ -f /srv/remote-server/.env ] && "
            f". /srv/remote-server/.env; set +a && cd {cwd} && ")


def git_argv(*args: str) -> list:
    stack = stack_dir()
    return ["git", "-c", f"safe.directory={stack}", "-C", stack, *args]

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


def action_steps(action, repo, gh, td_cwd, td_argv=None):
    """argv steps for an action. Returns (steps, error).

    Port of buttons/server.py::ACTIONS steps plus the run_action
    step building (repo prefix substitution, run-td/merge-approved
    dynamic steps). df paths use the stack dir + / instead of the
    container-only /home/ari so health works on the host too.
    """
    if action == "health":
        return [
            ["df", "-h", stack_dir(), "/"],
            ["free", "-m"],
            compose_argv("ps", "--format",
                         "table {{.Name}}\t{{.State}}\t{{.Status}}"),
        ], None
    if action == "status":
        return [
            git_argv("status", "--short", "--branch"),
            git_argv("log", "--oneline", "-3"),
            compose_argv("ps"),
            ["docker", "images",
             "ghcr.io/arii/remote-server-terminal",
             "--format", "{{.Repository}}:{{.Tag}}  {{.Size}}  "
                        "{{.CreatedSince}}"],
        ], None
    if action == "logs":
        return [
            compose_argv("logs", "--tail=100", "terminal", "ollama"),
        ], None
    if action == "deploy-buttons":
        return [
            exec_argv("git -c safe.directory=/srv/remote-server "
                      "-c core.hooksPath=/dev/null -C /srv/remote-server "
                      "pull --ff-only && chmod +x "
                      "/srv/remote-server/scripts/*.sh 2>/dev/null || true"),
            exec_argv("HOST_HOME=/home/ari HOME=/home/ari docker compose "
                      "-f /srv/remote-server/docker-compose.yml up -d "
                      "--build buttons"),
            exec_argv("for i in $(seq 1 30); do if curl -sf "
                      "http://buttons:8000/buttons/api/status >/dev/null; "
                      "then echo buttons up; exit 0; fi; sleep 2; done; "
                      "HOST_HOME=/home/ari HOME=/home/ari docker compose "
                      "-f /srv/remote-server/docker-compose.yml ps buttons; "
                      "exit 1"),
        ], None
    if action == "recreate":
        return [
            compose_argv("up", "-d", "--force-recreate", "terminal"),
        ], None
    if action == "recreate-code":
        return [
            compose_argv("up", "-d", "--force-recreate", "code-server"),
        ], None
    prefix = td_prefix(td_cwd)
    if action == "briefing-td":
        return [
            exec_argv(prefix +
                      "python3 /srv/remote-server/buttons/briefing_summary.py "
                      f"--briefing --repo {repo} --gh {gh}"),
        ], None
    if action == "warm-context":
        return [exec_argv(prefix + "td context-warm")], None
    if action == "preflight":
        return [
            exec_argv(prefix + "set -o pipefail; td --json preflight | "
                      "python3 /srv/remote-server/buttons/preflight_digest.py"),
        ], None
    if action == "create-plan":
        return [
            exec_argv(prefix + "mkdir -p .agents/plans && "
                      "python3 /srv/remote-server/buttons/briefing_summary.py "
                      "--plan-dir .agents/plans "
                      f"--repo {repo} --gh {gh} && "
                      "git status --short .agents/plans/"),
        ], None
    if action == "run-td":
        if not td_argv or validate_td_command(" ".join(td_argv)) != td_argv:
            return None, "run-td needs a validated td command attached"
        return [
            exec_argv(prefix + " ".join(shlex.quote(t) for t in td_argv)),
        ], None
    if action == "merge-approved":
        return [
            exec_argv(prefix + "td --json agent merge-approved -r "
                      f"{gh} --execute"),
        ], None
    return None, "unknown action"


def run_step_pipe(step, append, runner=None):
    """Run one step, streaming output through append(chunk).

    Pipes (not a PTY: no ANSI in the UI). ``runner`` injects tests:
    runner(argv) -> (exit_code, output).
    """
    if runner is not None:
        code, out = runner(step)
        if out:
            append(out)
        return code
    proc = subprocess.Popen(
        step, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=stack_dir(),
        env={**os.environ, "TERM": "xterm-256color"})
    assert proc.stdout is not None
    for line in proc.stdout:
        append(line)
    return proc.wait()


def execute_run(registry, run_id, action, repo, gh, td_cwd,
                td_argv=None, runner=None):
    """Worker body: run an action's steps to completion. Never raises."""
    try:
        steps, err = action_steps(action, repo, gh, td_cwd, td_argv)
        if err:
            registry.append(run_id, f"error: {err}\n")
            registry.finish_run(run_id, 1)
            return
        for step in steps:
            registry.append(run_id, "$ " + " ".join(step) + "\n")
            chunks: list = []

            def _cb(text, chunks=chunks):
                chunks.append(text)
                registry.append(run_id, text)

            try:
                code = run_step_pipe(step, _cb, runner=runner)
            except Exception as exc:
                registry.append(run_id, f"error: {exc}\n")
                chunks.append("error")
                code = 1
            if code != 0:
                if not "".join(chunks).strip():
                    registry.append(
                        run_id, "note: the step printed nothing before "
                        "failing — re-run this command on the host to "
                        "diagnose.\n")
                registry.finish_run(run_id, code)
                return
        registry.finish_run(run_id, 0)
    except Exception as exc:  # never leave a run stuck active
        try:
            registry.append(run_id, f"error: {exc}\n")
            registry.finish_run(run_id, 1)
        except Exception:
            pass


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

    def append(self, run_id, text):
        with self._lock:
            run = self.runs.get(run_id)
            if run is not None:
                append_output(run, text)

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
