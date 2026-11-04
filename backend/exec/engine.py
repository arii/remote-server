"""Execution engine ported from buttons/server.py.

Faithful port of the allowlisted command execution surface:
allowed td commands/shapes, run-plan allowlist, action steps,
timeouts, and output truncation. Every command run goes through
the same allowlists as the original. Jules actions are cut (Jules
is dead); the Pi transcript step of logs is cut (Pi is dead).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
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
    # One-tap phone diagnostics (#393): read-only shapes only. audit-pr
    # pins the read-only stages; --submit is never allowlisted, and the
    # bare no-flag form is a server-side no-op so it stays rejected.
    "td doctor",
    "td preflight",
    "td gh audit-pr --pr N --fetch --audit",
    "td ai review N",
]
ALLOWED_TD_SHAPES = [cmd.split() for cmd in ALLOWED_TD_COMMANDS]

# Exact port of the run-plan allowlist in buttons/server.py
# (handler for /buttons/api/run-plan).
ALLOWED_PLANS = ("issue-audit", "workflow-audit", "review", "aggregation")

# Exact port of the timeouts used by buttons/server.py:
# _td_run default timeout, and the run-plan call timeout.
TD_RUN_TIMEOUT = 120
PLAN_TIMEOUT = 300

# Watchdog: no single engine step may run longer than this (#288).
# A hung step is killed and fails the run instead of wedging the
# single-flight engine forever.
STEP_TIMEOUT = 300

# Per-action step budgets (#670). CPU-local triage fanout runs take
# 10+ min unbounded (cutoff audit observation), so the default 300s
# watchdog would kill healthy runs; fanout gets a 20-minute budget.
# Actions not listed here inherit STEP_TIMEOUT.
ACTION_STEP_TIMEOUTS = {
    "triage-fanout": 1200,
}


def step_timeout(action) -> int:
    """Watchdog budget for one step of ``action`` (STEP_TIMEOUT
    unless ACTION_STEP_TIMEOUTS overrides it)."""
    return ACTION_STEP_TIMEOUTS.get(action, STEP_TIMEOUT)

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
    "triage-fanout",
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
# plus run-td, minus the dead Jules pair). triage-fanout runs the
# sweep for the run's repo only (one repo per run, #670).
REPO_SCOPED_ACTIONS = (
    "briefing-td",
    "warm-context",
    "preflight",
    "create-plan",
    "merge-approved",
    "run-td",
    "triage-fanout",
)

ENGINE_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def stack_dir() -> str:
    return os.environ.get("BUTTONS_STACK", ENGINE_ROOT)


def deep_agent_dir() -> str:
    """deep-agent checkout dir. Same DEEP_AGENT_DIR override the
    chat router uses, so jobs and chat resolve one venv."""
    return os.environ.get("DEEP_AGENT_DIR",
                           os.path.join(ENGINE_ROOT, "deep-agent"))


def deep_agent_python() -> str:
    """Venv python for deep-agent scripts. Same DEEP_AGENT_VENV
    override the chat router uses; never a hardcoded home path."""
    return os.environ.get(
        "DEEP_AGENT_VENV",
        os.path.join(deep_agent_dir(), ".venv", "bin", "python"))


def deep_agent_script(name: str) -> str:
    return os.path.join(deep_agent_dir(), name)


def compose_file() -> str:
    return os.environ.get("BUTTONS_COMPOSE_FILE",
                           os.path.join(stack_dir(), "docker-compose.yml"))


def compose_env_file() -> str | None:
    """Compose --env-file: explicit override, else the stack .env,
    else the primary checkout .env (dev worktrees have no .env)."""
    override = os.environ.get("BUTTONS_ENV_FILE")
    if override:
        return override
    for cand in (os.path.join(stack_dir(), ".env"),
                 "/srv/remote-server/.env"):
        if os.path.isfile(cand):
            return cand
    return None


def compose_argv(*args: str) -> list:
    argv = ["docker", "compose", "-f", compose_file()]
    env_file = compose_env_file()
    if env_file:
        argv += ["--env-file", env_file]
    return argv + list(args)


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
    # cwd = live checkout: td discovers project_config.json upward from
    # cwd, and the image workdir (/app) has none.
    proc = run.run(
        ["bash", "-c", cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        text=True,
        cwd=stack_dir(),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or "").strip().splitlines()[-1]
            if (proc.stderr or "").strip()
            else "command failed"
        )
    return proc.stdout


async def td_run_async(cmd, timeout=TD_RUN_TIMEOUT):
    """Async td_run: same command shape and error contract, but the
    wait happens on the event loop instead of blocking a sync worker
    thread, so concurrent plan runs overlap instead of starving
    FastAPI's sync pool (#294)."""
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=stack_dir(),
    )
    assert proc.stdout is not None and proc.stderr is not None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise RuntimeError(f"command timed out after {timeout}s")
    if proc.returncode != 0:
        text = err.decode("utf-8", "replace")
        raise RuntimeError(
            text.strip().splitlines()[-1] if text.strip()
            else "command failed"
        )
    return out.decode("utf-8", "replace")


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
                      "http://buttons:8000/api/status >/dev/null; "
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
                      "python3 /srv/remote-server/core_cli/briefing_summary.py "
                      f"--briefing --repo {repo} --gh {gh}"),
        ], None
    if action == "warm-context":
        return [exec_argv(prefix + "td context-warm")], None
    if action == "preflight":
        return [
            exec_argv(prefix + "set -o pipefail; td --json preflight | "
                      "python3 /srv/remote-server/core_cli/preflight_digest.py"),
        ], None
    if action == "create-plan":
        return [
            exec_argv(prefix + "mkdir -p .agents/plans && "
                      "python3 /srv/remote-server/core_cli/briefing_summary.py "
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
    if action == "triage-fanout":
        # One-repo sweep for the run's repo (#670). --prefer is
        # omitted so the script's auto ladder applies (Nebius when
        # keyed, else the local model). Progress lines + FINAL
        # stream as text through the normal output path (#648); the
        # step inherits single-flight, history, re-run, cancel, and
        # the #655 schedule surface like every other action.
        python = deep_agent_python()
        script = deep_agent_script("triage_fanout.py")
        if not os.path.isfile(python):
            return (None, "deep-agent venv python not found at "
                    f"{python} (set DEEP_AGENT_VENV)")
        if not os.path.isfile(script):
            return (None, "triage fanout script not found at "
                    f"{script}")
        return [[python, script, gh]], None
    return None, "unknown action"


def run_step_pipe(step, append, runner=None, timeout=None,
                  on_proc=None):
    """Run one step, streaming output through append(chunk).

    Pipes (not a PTY: no ANSI in the UI). ``runner`` injects tests:
    runner(argv) -> (exit_code, output).

    Output drains on a reader thread while the caller waits with a
    watchdog: a step running past ``timeout`` (default STEP_TIMEOUT)
    is killed and returns 124 with a note, so a hung step fails its
    run instead of wedging the single-flight engine (#288).

    ``on_proc`` reports the live child for cancellation (#589):
    called with the Popen right after spawn and with None once the
    step ends. Each step runs in its own session
    (start_new_session) so cancel can kill the whole tree as one
    process group, including pipeline grandchildren.
    """
    if runner is not None:
        code, out = runner(step)
        if out:
            append(out)
        return code
    limit = STEP_TIMEOUT if timeout is None else timeout
    proc = subprocess.Popen(
        step, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, cwd=stack_dir(), start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color"})
    assert proc.stdout is not None
    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:
            pass
    drained = threading.Event()
    errors: list = []

    def _drain():
        try:
            for line in proc.stdout:
                append(line)
        except Exception as exc:
            errors.append(exc)
        finally:
            drained.set()

    worker = threading.Thread(target=_drain, daemon=True)
    worker.start()
    try:
        code = proc.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        proc.kill()
        code = 124
        append(f"\nnote: step killed after {limit}s without finishing "
               f"(watchdog; exit 124)\n")
    finally:
        drained.wait(timeout=5)
        try:
            proc.stdout.close()
        except Exception:
            pass
        if on_proc is not None:
            try:
                on_proc(None)
            except Exception:
                pass
    if errors:
        raise errors[0]
    return code


def _terminate_tree(proc, grace=3.0):
    """SIGTERM a step's process group, SIGKILL after ``grace`` seconds.

    Signals only: the step owner (run_step_pipe's wait) keeps the
    reap, so concurrent wait() calls can never double-reap. Already
    exited procs and vanished groups return quietly.
    """
    try:
        if proc.poll() is not None:
            return
        pgid = os.getpgid(proc.pid)
    except Exception:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except Exception:
            return
        wait = grace if sig == signal.SIGTERM else 2.0
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            try:
                if proc.poll() is not None:
                    return
            except Exception:
                return
            time.sleep(0.05)


def execute_run(registry, run_id, action, repo, gh, td_cwd,
                td_argv=None, runner=None):
    """Worker body: run an action's steps to completion. Never raises."""
    from backend.ledger import append as ledger_append

    started = time.time()

    def _done(code):
        registry.finish_run(run_id, code)
        entry = {"kind": "run", "action": action, "repo": repo,
                 "exit": code,
                 "elapsed_s": round(time.time() - started, 2)}
        if td_argv:
            entry["command"] = " ".join(td_argv)
        ledger_append(entry)

    def _cancelled():
        """Cancel path: note + exit 130 when cancel was requested."""
        if not registry.cancel_requested(run_id):
            return False
        registry.append(run_id, "note: cancelled by operator\n")
        _done(130)
        return True

    def _track(proc):
        registry.set_proc(run_id, proc)

    try:
        steps, err = action_steps(action, repo, gh, td_cwd, td_argv)
        if err:
            registry.append(run_id, f"error: {err}\n")
            _done(1)
            return
        for step in steps:
            if _cancelled():
                return
            registry.append(run_id, "$ " + " ".join(step) + "\n")
            chunks: list = []

            def _cb(text, chunks=chunks):
                chunks.append(text)
                registry.append(run_id, text)

            try:
                code = run_step_pipe(step, _cb, runner=runner,
                                     timeout=step_timeout(action),
                                     on_proc=_track)
            except Exception as exc:
                registry.append(run_id, f"error: {exc}\n")
                chunks.append("error")
                code = 1
            # After-step check first: a cancel that killed this step
            # mid-flight surfaces here (not as a step failure).
            if _cancelled():
                return
            if code != 0:
                if not "".join(chunks).strip():
                    registry.append(
                        run_id, "note: the step printed nothing before "
                        "failing — re-run this command on the host to "
                        "diagnose.\n")
                _done(code)
                return
        _done(0)
    except Exception as exc:  # never leave a run stuck active
        try:
            registry.append(run_id, f"error: {exc}\n")
        except Exception:
            pass
        try:
            _done(1)
        except Exception:
            pass


# Durable run store (#461). The registry used to be pure in-memory,
# so every console restart wiped the Jobs view. Run records persist
# to a JSON snapshot on the buttons-data volume; a fresh registry
# rehydrates from it on boot. Single-writer: one console owns the
# file (cross-replica sharing is an explicit non-goal).
DEFAULT_RUNS_PATH = "/var/lib/buttons/jobs_runs.json"

# Snapshot keeps the newest runs only: outputs run to MAX_OUTPUT
# each, so an unbounded file would balloon.
RUN_STORE_MAX_RUNS = 200

# Minimum seconds between output-append persists. Appends arrive
# per output line; the throttle bounds snapshot rewrites while a
# run chats. start/finish/clear always persist immediately.
RUN_STORE_SAVE_INTERVAL = 2.0

# Suffix for runs that were active when the previous process died:
# the tail above it ends at the last persist, and the outcome is
# honestly unknown (exit stays None).
RESTART_NOTE = ("note: console restarted while this run was active; "
                "output ends at the last persist.\n")


def runs_store_path() -> str:
    """Current run-store file path (env override read dynamically
    so tests can point it at tmp)."""
    return os.environ.get("BUTTONS_JOBS_RUNS_PATH", DEFAULT_RUNS_PATH)


class RunRegistry:
    """Single-flight run records + job list. Ports the runs/jobs
    dicts, locks, and clear semantics of buttons/server.py."""

    def __init__(self, store_path=None):
        self._lock = threading.Lock()
        self.runs = {}
        self.active_id = None
        self.jobs = {}
        self._seq = 0
        # Push channel state: every real mutation bumps _version and
        # notifies _cond (which shares _lock, so waiters never miss
        # a bump). The jobs SSE endpoints block on this instead of
        # polling. No-ops (busy start, missing run, empty clear)
        # bump nothing, so wakeups always mean new state.
        self._cond = threading.Condition(self._lock)
        self._version = 0
        # Durable snapshot path (#461), or None for pure in-memory
        # (unit tests). The jobs router passes runs_store_path();
        # loading here means every boot rehydrates.
        self._store_path = store_path
        self._last_save = 0.0
        if store_path is not None:
            with self._lock:
                self._load_locked()

    def _bump(self):
        """Bump the push version. Caller must hold _lock."""
        self._version += 1
        self._cond.notify_all()

    def _persist_locked(self):
        """Write the run snapshot atomically. Never raises: a store
        failure must not fail the run it records. Caller must hold
        _lock."""
        if self._store_path is None:
            return
        try:
            ordered = sorted(self.runs.values(),
                             key=lambda r: r.get("seq", 0))
            snap = [{k: v for k, v in r.items()
                     if not k.startswith("_")}
                    for r in ordered[-RUN_STORE_MAX_RUNS:]]
            d = os.path.dirname(self._store_path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self._store_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "runs": snap}, f)
            os.replace(tmp, self._store_path)
            self._last_save = time.monotonic()
        except Exception as exc:
            print(f"run store persist failed: {exc}", flush=True)

    def _load_locked(self):
        """Rehydrate the snapshot left by the previous process. Runs
        still marked active died with it: finish them with the
        restart note and an unknown outcome instead of wedging
        single-flight. Missing/corrupt stores load as empty. Caller
        must hold _lock."""
        try:
            with open(self._store_path, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            return
        rows = state.get("runs") if isinstance(state, dict) else None
        if not isinstance(rows, list):
            return
        now = time.time()
        seq = 0
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            rid = rec.get("id")
            if not rid or not isinstance(rid, str):
                continue
            run = {k: v for k, v in rec.items()
                   if not k.startswith("_")}
            run.pop("cancel_requested", None)
            run["id"] = rid
            keep = run.get("seq")
            run["seq"] = keep if isinstance(keep, int) \
                and not isinstance(keep, bool) else seq
            seq = max(seq, run["seq"]) + 1
            out = run.get("output", "")
            run["output"] = out if isinstance(out, str) else ""
            argv = run.get("td_argv", [])
            run["td_argv"] = list(argv) if isinstance(argv, list) \
                else []
            run.setdefault("action", "")
            run.setdefault("repo", "")
            run.setdefault("exit_code", None)
            run.setdefault("started_at", now)
            run.setdefault("finished_at", None)
            run.setdefault("last_output_at", now)
            if not run.get("done"):
                run["done"] = True
                run["finished_at"] = now
                run["output"] += RESTART_NOTE
            else:
                run["done"] = True
            self.runs[rid] = run
        self._seq = seq
        self.active_id = None

    def start_run(self, action, repo="remote-server", td_argv=None):
        """Create a run for an allowlisted action. Exact port of
        buttons/server.py::_start_run validation + single-flight."""
        if action not in ALLOWED_ACTIONS:
            return None, "unknown action"
        with self._lock:
            if self.active_id is not None:
                return (None, "a run is already in progress: "
                              f"{self.active_id}")
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
            self._bump()
            self._persist_locked()
        return run_id, None

    def append(self, run_id, text):
        with self._lock:
            run = self.runs.get(run_id)
            if run is not None:
                append_output(run, text)
                self._bump()
                if time.monotonic() - self._last_save >= \
                        RUN_STORE_SAVE_INTERVAL:
                    self._persist_locked()

    def finish_run(self, run_id, exit_code):
        with self._lock:
            if self.active_id == run_id:
                self.active_id = None
            run = self.runs.get(run_id)
            if run is None:
                return
            run["exit_code"] = exit_code
            run["done"] = True
            run["finished_at"] = time.time()
            self._bump()
            self._persist_locked()

    def set_proc(self, run_id, proc):
        """Track the live step child for cancellation (#589). The
        worker reports the Popen after spawn and None once the step
        ends; a cleared run simply drops the report."""
        with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                return
            run["_proc"] = proc

    def cancel_requested(self, run_id):
        """True once cancel_run flags the run. Missing runs read as
        not cancelled, so clear keeps its existing semantics."""
        with self._lock:
            run = self.runs.get(run_id)
            return bool(run and run.get("cancel_requested"))

    def cancel_run(self, run_id, grace=3.0):
        """Flag a run cancelled and SIGTERM its step tree (#589).

        Returns (run_id, None) on success, (None, "unknown run")
        for a missing record, (None, "run is not active") for a
        finished run. The tree kill runs outside the lock; the
        worker picks up the flag before/after each step and
        finishes with the note + exit 130.
        """
        with self._lock:
            run = self.runs.get(run_id)
            if run is None:
                return None, "unknown run"
            if run.get("done"):
                return None, "run is not active"
            run["cancel_requested"] = True
            proc = run.get("_proc")
            self._bump()
            self._persist_locked()
        if proc is not None:
            _terminate_tree(proc, grace)
        return run_id, None

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
            self._bump()
            self._persist_locked()
            return run["id"], None

    def clear_jobs(self, all_jobs=False):
        """Clear finished runs (or all). Returns (count, None).

        Operates on the runs store (the same store GET /api/jobs
        lists), not the legacy jobs dict. The all-path refuses
        with (None, err) while a run is active (#512): deleting
        the active record would orphan its procs and haunt
        single-flight with 409s naming an invisible run. Cancel
        the active run first, then clear-all. Refusals bump
        nothing, like any other no-op.
        """
        with self._lock:
            if all_jobs:
                if self.active_id is not None:
                    return (None,
                            "a run is still active: "
                            f"{self.active_id}")
                unfinished = [rid for rid, run in self.runs.items()
                              if not run.get("done")]
                if unfinished:
                    return (None,
                            "a run is still active: "
                            f"{unfinished[0]}")
            to_del = [
                rid
                for rid, run in self.runs.items()
                if all_jobs or run.get("done")
            ]
            for rid in to_del:
                del self.runs[rid]
            if to_del:
                self._bump()
                self._persist_locked()
        return len(to_del), None

    def list_jobs(self):
        with self._lock:
            out = list(self.jobs.values())
        out.sort(key=lambda j: j.get("created_at", 0), reverse=True)
        return out
