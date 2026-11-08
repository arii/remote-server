"""Execution engine ported from buttons/server.py.

Faithful port of the allowlisted command execution surface:
allowed td commands/shapes, run-plan allowlist, action steps,
timeouts, and output truncation. Every command run goes through
the same allowlists as the original. The dead-agent actions
(sync-agents/nudge-agents) and the fix-ci dispatch are cut (dead
backends, #102/#428); the Pi transcript step of logs is cut (Pi
is dead).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid

# Exact port of buttons/server.py::MAX_OUTPUT: keep the tail, model
# pulls are verbose.
MAX_OUTPUT = 200_000

# Exact port of buttons/server.py ALLOWED_TD_COMMANDS, minus the
# dead dispatches: fix-ci shelled to `td agent fix-ci --execute`,
# whose backend is dead (#428); list-sessions and
# run-feedback-check shelled to the retired agent backend (#102).
ALLOWED_TD_COMMANDS = [
    "td gh read-pr-reviews --pr N",
    "td gh read-pr-comments --pr N",
    "td gh resolve-conflicts --pr N",
    "td context-warm",
    "td --json preflight",
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
    "ux-audit": 1200,
    "impact-pass": 1200,
    "review-pr": 1200,
    "supervise": 1200,
}


def step_timeout(action) -> int:
    """Watchdog budget for one step of ``action`` (STEP_TIMEOUT
    unless ACTION_STEP_TIMEOUTS overrides it)."""
    return ACTION_STEP_TIMEOUTS.get(action, STEP_TIMEOUT)

# Action names ported from buttons/server.py::ACTIONS (dead-agent
# actions cut: sync-agents/nudge-agents, #102). "recreate"
# (terminal restart) is cut too: it drops live agent sessions with
# no resume path, so the console must not offer it (#444).
ALLOWED_ACTIONS = (
    "health",
    "status",
    "deploy-buttons",
    "recreate-code",
    "logs",
    "briefing-td",
    "warm-context",
    "preflight",
    "create-plan",
    "run-td",
    "run-plan",
    "merge-approved",
    "merge-approved-gated",
    "triage-fanout",
    "check-notifications",
    "autofile-scan",
    "ux-audit",
    "impact-pass",
    "review-pr",
    "supervise",
)

# Actions whose original has confirm: True. The router requires
# {intent: "jobs-run", confirmed: True} for these.
DESTRUCTIVE_ACTIONS = (
    "deploy-buttons",
    "recreate-code",
    "create-plan",
    "merge-approved",
)

# Actions scoped to the picked repo (original REPO_SCOPED_ACTIONS
# plus run-td, minus the dead sync/nudge pair). triage-fanout runs
# the sweep for the run's repo only (one repo per run, #670).
REPO_SCOPED_ACTIONS = (
    "briefing-td",
    "warm-context",
    "preflight",
    "create-plan",
    "merge-approved",
    "merge-approved-gated",
    "run-td",
    "triage-fanout",
    "check-notifications",
    "autofile-scan",
    "review-pr",
    "supervise",
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


def validate_plan_argv(tokens):
    """True when tokens is exactly a build_plan_argv-shaped plan
    command. Structural mirror of the builder: the run-plan action
    step execs the attached argv directly, so anything the builder
    could not have produced is rejected here."""
    if not isinstance(tokens, (list, tuple)) or len(tokens) < 4:
        return False
    tokens = list(tokens)
    if tokens[0:3] != ["td", "--json", "agent"]:
        return False
    if not all(isinstance(t, str) for t in tokens):
        return False
    sub, rest = tokens[3], tokens[4:]
    if sub == "plan-issue-audit":
        return rest == ["--all-open"]
    if sub == "plan-workflow-audit":
        return rest == []
    if sub == "plan-review":
        return (len(rest) == 2 and rest[0] == "--pr"
                and rest[1].isdigit() and int(rest[1]) > 0)
    if sub == "plan-aggregation":
        # ["--pr", N] pairs (1..10) + ["--target", T].
        if len(rest) < 4 or len(rest) % 2 != 0:
            return False
        if rest[-2] != "--target" or not TARGET_RE.fullmatch(rest[-1]):
            return False
        pairs = rest[:-2]
        if not pairs or len(pairs) > 20:
            return False
        for i in range(0, len(pairs), 2):
            if pairs[i] != "--pr":
                return False
            if not pairs[i + 1].isdigit() or int(pairs[i + 1]) <= 0:
                return False
        return True
    return False


UX_AUDIT_SITE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,62}$")


def validate_ux_audit_argv(tokens):
    """True for builder-shaped ux-audit argv: ["ux-audit", site]
    plus an optional ["--max-pages", N] pair (1..200).

    Structural only (hostname-shaped site, numeric cap): the
    runner owns the site registry and fails closed on unknown
    sites, so the engine never drifts from it.
    """
    if not isinstance(tokens, (list, tuple)):
        return False
    tokens = list(tokens)
    if len(tokens) not in (2, 4):
        return False
    if not all(isinstance(t, str) for t in tokens):
        return False
    if tokens[0] != "ux-audit":
        return False
    if not UX_AUDIT_SITE_RE.match(tokens[1]):
        return False
    if len(tokens) == 2:
        return True
    return (tokens[2] == "--max-pages" and tokens[3].isdigit()
            and 1 <= int(tokens[3]) <= 200)


def validate_review_pr_argv(tokens):
    """True for builder-shaped review argv: ["review-pr", N] plus an
    optional "--post" flag. Structural only (positive PR number,
    exact flag spelling): the step builder maps the bare form to
    --dry-run, so anything else could never post anyway.
    """
    if not isinstance(tokens, (list, tuple)):
        return False
    tokens = list(tokens)
    if len(tokens) not in (2, 3):
        return False
    if not all(isinstance(t, str) for t in tokens):
        return False
    if tokens[0] != "review-pr":
        return False
    if not tokens[1].isdigit() or int(tokens[1]) <= 0:
        return False
    if len(tokens) == 2:
        return True
    return tokens[2] == "--post"


def _td_failure_message(stderr, stdout):
    """Failure text for a nonzero td/gh call: last stderr line, else
    the handled-error payload td prints as JSON on stdout (td exits
    nonzero with empty stderr for those, which used to surface as the
    opaque "command failed" and made CI failures undebuggable)."""
    text = (stderr or "").strip() if isinstance(stderr, str) else ""
    if text:
        return text.splitlines()[-1]
    raw = (stdout or "").strip() if isinstance(stdout, str) else ""
    try:
        payload = json.loads(raw) if raw else None
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("message"):
        return str(payload["message"])[:200]
    return "command failed"


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
        raise RuntimeError(_td_failure_message(proc.stderr, proc.stdout))
    return proc.stdout


async def td_run_async(cmd, timeout=TD_RUN_TIMEOUT):
    """Async td_run: same command shape and error contract, but the
    wait happens on the event loop instead of blocking a sync worker
    thread, so concurrent plan runs overlap instead of starving
    FastAPI's sync pool (#294). Runs in its own session so a
    timeout kills the whole tree, not just the bash child (#445)."""
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=stack_dir(),
        start_new_session=True,
    )
    assert proc.stdout is not None and proc.stderr is not None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        _kill_tree(proc)
        await proc.wait()
        raise RuntimeError(f"command timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(_td_failure_message(
            err.decode("utf-8", "replace"), out.decode("utf-8", "replace")))
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
    if action == "recreate-code":
        return [
            compose_argv("up", "-d", "--force-recreate", "code-server"),
        ], None
    prefix = td_prefix(td_cwd)
    if action == "briefing-td":
        return [
            exec_argv(prefix +
                      "python3 /srv/remote-server/backend/briefing_summary.py "
                      f"--briefing --repo {repo} --gh {gh}"),
        ], None
    if action == "warm-context":
        return [exec_argv(prefix + "td context-warm")], None
    if action == "preflight":
        return [
            exec_argv(prefix + "set -o pipefail; td --json preflight | "
                      "python3 /srv/remote-server/backend/preflight_digest.py"),
        ], None
    if action == "create-plan":
        return [
            exec_argv(prefix + "mkdir -p .agents/plans && "
                      "python3 /srv/remote-server/backend/briefing_summary.py "
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
    if action == "run-plan":
        if not td_argv or not validate_plan_argv(td_argv):
            return None, "run-plan needs a validated plan command attached"
        # Direct exec of the builder-shaped argv: the same command the
        # old inline handler ran via bash -c, minus the shell. The
        # step inherits the default 300s watchdog, matching
        # PLAN_TIMEOUT, plus single-flight, history, cancel, and
        # ledgering through the standard run path (#452).
        return [list(td_argv)], None
    if action == "merge-approved":
        return [
            exec_argv(prefix + "td --json agent merge-approved -r "
                      f"{gh} --execute"),
        ], None
    if action == "merge-approved-gated":
        # Scheduled-safe by construction (#867): every gate is
        # re-checked at fire time and only approved-green PRs to the
        # repo's default branch merge, capped per fire. Deliberately
        # NOT in DESTRUCTIVE_ACTIONS (schedulable); the gates stand in
        # for the human confirm. Manual merge-approved keeps confirm.
        # Local -m exec (check-notifications precedent): step_env /
        # step_cwd resolve the module from this backend's tree.
        if not gh:
            return None, "merge-approved-gated needs a repo"
        return [[sys.executable, "-m", "backend.merge_gated",
                 "--repo", repo, "--gh", gh]], None
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
    if action == "check-notifications":
        # One-repo notify scan (#822): local exec (like run-plan, not
        # exec_argv into terminal) so the check reads/writes the same
        # notifier state file the API serves. step_env() points -m at
        # this backend's tree: stack_dir() lags the image.
        if not gh:
            return None, "check-notifications needs a repo"
        return [[sys.executable, "-m", "backend.notify_check",
                 "--repo", repo, "--gh", gh]], None
    if action == "autofile-scan":
        # One-repo repeated-failure scan (#825): local exec (like
        # check-notifications) so the scan reads the same ledger
        # file the API serves. Files at most one tracking issue per
        # crossed signature (marker-deduped, filing only).
        if not gh:
            return None, "autofile-scan needs a repo"
        return [[sys.executable, "-m", "backend.autofile_scan",
                 "--repo", repo, "--gh", gh]], None
    if action == "ux-audit":
        # Dispatched multi-page UX audit (#850): local exec of the
        # deep-agent runner (like triage-fanout) so the report lands
        # in the state dir this backend serves. Site-scoped, not
        # repo-scoped: the site rides the attached argv. Audit-only
        # (crawl + file one findings issue); --model stays off so
        # the job is deterministic (runbook covers model reruns).
        if not td_argv or not validate_ux_audit_argv(td_argv):
            return None, "ux-audit needs attached [ux-audit, site]"
        python = deep_agent_python()
        script = deep_agent_script("ux_audit.py")
        if not os.path.isfile(python):
            return (None, "deep-agent venv python not found at "
                    f"{python} (set DEEP_AGENT_VENV)")
        if not os.path.isfile(script):
            return (None, "ux audit script not found at "
                    f"{script}")
        argv = [python, script, "audit", td_argv[1]]
        if len(td_argv) == 4:
            argv += ["--max-pages", td_argv[3]]
        argv.append("--file")
        return [argv], None
    if action == "impact-pass":
        # Scheduled console UX self-regression (#869): local exec
        # (like check-notifications) so the pass reads/writes the
        # same snapshot + ledger state the API serves. Console-wide,
        # not repo-scoped (like ux-audit); findings are data (exit
        # 0, ledgered), capture errors fail the run (job_fail push).
        return [[sys.executable, "-m", "backend.impact_pass"]], None
    if action == "review-pr":
        # Local-model PR review (#886): local -m exec (the
        # merge-approved-gated shape) of backend.review. Read-only
        # by default: the bare argv maps to --dry-run (plan only,
        # prints, never posts); --post rides only when the attached
        # argv explicitly carries it. review.py rejects --dry-run +
        # --post together, so exactly one flag is ever attached.
        if not gh:
            return None, "review-pr needs a repo"
        if not td_argv or not validate_review_pr_argv(td_argv):
            return None, "review-pr needs attached [review-pr, N]"
        argv = [sys.executable, "-m", "backend.review",
               "--repo", gh, "--pr", td_argv[1]]
        argv.append("--post" if len(td_argv) == 3 else "--dry-run")
        return [argv], None
    if action == "supervise":
        # Propose-only maintainer sweep (#886): deep-agent
        # --supervise mode for the run's repo (triage-fanout
        # precedent: venv python + the repo's gh name). Findings
        # surface as approval interrupts (exit 3, like a chat
        # proposal awaiting verdict); all-clean exits 0.
        if not gh:
            return None, "supervise needs a repo"
        python = deep_agent_python()
        script = deep_agent_script("deep_chat.py")
        if not os.path.isfile(python):
            return (None, "deep-agent venv python not found at "
                    f"{python} (set DEEP_AGENT_VENV)")
        if not os.path.isfile(script):
            return (None, "deep chat script not found at "
                    f"{script}")
        return [[python, script, gh, "--supervise"]], None
    return None, "unknown action"


def _is_python_m_step(step):
    """True for steps invoking this interpreter with ``-m``.

    Only matches the running interpreter (``free -m`` and friends
    are untouched).
    """
    return step[:1] == [sys.executable] and "-m" in step


def step_env(steps):
    """Extra child env for steps that run ``python -m ...``.

    Belt-and-braces beside step_cwd(): -m modules resolve from this
    backend's own tree (ENGINE_ROOT) even if the cwd override is
    ever bypassed.
    """
    for step in steps:
        if _is_python_m_step(step):
            base = os.environ.get("PYTHONPATH", "")
            path = ENGINE_ROOT + (os.pathsep + base if base else "")
            return {"PYTHONPATH": path}
    return None


def step_cwd(steps):
    """Child cwd override for steps that run ``python -m ...``.

    Steps default to cwd=stack_dir(), a host bind-mount that lags
    the deployed image — and cwd SHADOWS PYTHONPATH for the
    top-level package, so a stale stack_dir()/backend (missing
    notify_check.py) wins over /app and the import dies with No
    module named (live 2026-11-04). -m steps therefore execute FROM
    the deployed tree; their state paths are absolute, so no step
    depends on the stack cwd. Returns None (keep stack_dir) for
    everything else.
    """
    for step in steps:
        if _is_python_m_step(step):
            return ENGINE_ROOT
    return None


def run_step_pipe(step, append, runner=None, timeout=None,
                  on_proc=None, env=None, cwd=None):
    """Run one step, streaming output through append(chunk).

    Pipes (not a PTY: no ANSI in the UI). ``runner`` injects tests:
    runner(argv) -> (exit_code, output).

    Output drains on a reader thread while the caller waits with a
    watchdog: a step running past ``timeout`` (default STEP_TIMEOUT)
    has its whole process group killed and returns 124 with a note,
    so a hung step fails its run instead of wedging the
    single-flight engine (#288) and no orphaned grandchild survives
    to fight a retry (#445).

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
        text=True, cwd=cwd or stack_dir(), start_new_session=True,
        env={**os.environ, "TERM": "xterm-256color",
             **(env or {})})
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
        _kill_tree(proc)
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


def _kill_tree(proc):
    """SIGKILL a step's whole process group (watchdog path).

    Steps run in their own session (start_new_session), so the
    group is exactly the step's tree: killing it takes pipeline
    grandchildren down with the child instead of orphaning them
    to fight a later retry (#445). The budget is already spent,
    so there is no SIGTERM grace (unlike the cancel path in
    _terminate_tree). Falls back to killing the child alone when
    the group is already gone. Works for Popen and asyncio
    subprocess handles alike (both expose pid + kill).
    """
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except Exception:
            pass
    try:
        proc.kill()
    except Exception:
        pass


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

    # Queue gate (issue #1025): runs beyond the slot pool wait in
    # acquire until promotion. A False return means the run was
    # cancelled, cleared, or finished before its turn — exit
    # without running and without finishing (already done).
    if not registry.acquire(run_id):
        return

    try:
        steps, err = action_steps(action, repo, gh, td_cwd, td_argv)
        if err:
            registry.append(run_id, f"error: {err}\n")
            _done(1)
            return
        child_env = step_env(steps)
        child_cwd = step_cwd(steps)
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
                                     on_proc=_track, env=child_env,
                                     cwd=child_cwd)
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
                    # Never record a bare exit code (#1125): a silent
                    # step failure still names its exit and repeats
                    # the command line (echoed above), so the record
                    # alone can diagnose without re-running.
                    registry.append(
                        run_id, f"note: the step exited {code} with no "
                        "output — re-run the $ command above on the "
                        "host to diagnose.\n")
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

# Run-queue knobs (issue #1025): how many td runs may hold a slot
# at once, and how long an active run may go without output before
# it reads as wedged and gets reaped. Module-attr lookups at call
# time so tests can override them without env surgery.
MAX_CONCURRENT_RUNS = int(os.environ.get("BUTTONS_JOBS_MAX_CONCURRENT",
                                         "2"))
RUN_STALE_TTL = int(os.environ.get("BUTTONS_JOBS_STALE_TTL",
                                   "1800"))

# Suffix for runs that were active when the previous process died:
# the tail above it ends at the last persist, and the outcome is
# honestly unknown (exit stays None).
RESTART_NOTE = ("note: console restarted while this run was active; "
                "output ends at the last persist.\n")

# Completed-run TTL (#451): done runs older than this are evicted
# opportunistically at store sites (start/finish), by the public
# evict_completed sweep, and at boot rehydrate — mirroring the
# agent_turn RESULT_TTL sweep, with no background thread.
# Unfinished runs are never evicted, however old. Each completed
# run pins up to MAX_OUTPUT of output, so without eviction the
# registry grows for the life of the process.
COMPLETED_RUN_TTL = 3600


def runs_store_path() -> str:
    """Current run-store file path (env override read dynamically
    so tests can point it at tmp)."""
    return os.environ.get("BUTTONS_JOBS_RUNS_PATH", DEFAULT_RUNS_PATH)


# Error-line cap for job rows: attention shows one line, never a log.
JOB_ERROR_MAX = 300


def _run_to_job(run, queue_position=0):
    """Job-shaped view of one run record (#451). Pure: the caller
    snapshots under the registry lock; the row is a fresh dict.
    Queued runs carry their 1-based FIFO position (#1025) so the
    UI can show place-in-line instead of a bare waiting flag."""
    job = {
        "id": run.get("id", ""),
        "name": run.get("action", ""),
        "kind": run.get("action", ""),
        "repo": run.get("repo", ""),
        "done": bool(run.get("done")),
        "exit_code": run.get("exit_code"),
        "created_at": run.get("started_at", 0),
        "finished_at": run.get("finished_at"),
        "td_argv": list(run.get("td_argv", [])),
        "error": "",
        "queued": bool(run.get("queued")) and not bool(run.get("done")),
        "queue_position": queue_position,
    }
    if run.get("awaiting_approval") is True:
        job["awaiting_approval"] = True
    if job["done"] and job["exit_code"] not in (None, 0):
        out = run.get("output")
        tail = out.strip().splitlines() if isinstance(out, str) else []
        if tail:
            job["error"] = tail[-1][-JOB_ERROR_MAX:]
        else:
            job["error"] = f"exit {job['exit_code']}"
    return job


class RunRegistry:
    """Run records + FIFO queue + job list (issues #451, #1025).

    Ports the runs dict, locks, and clear semantics of
    buttons/server.py. Runs are the one collection (#451): the
    job list, clear, and the attention seams all read runs — no
    second jobs dict. Concurrency is a bounded slot pool, not
    single-flight: dispatches beyond MAX_CONCURRENT_RUNS wait in
    FIFO order instead of failing with 409s, and a stale lease
    (no output for RUN_STALE_TTL) reaps wedged runs so one stuck
    run cannot jam the queue forever."""

    def __init__(self, store_path=None):
        self._lock = threading.Lock()
        self.runs = {}
        self.active_ids = set()
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
        self.active_ids = set()
        # A restart must not resurrect long-dead runs: drop stale
        # completeds now, and persist so the next boot agrees (#451).
        if self._evict_completed_locked(now):
            self._persist_locked()

    def start_run(self, action, repo="remote-server", td_argv=None):
        """Create a run for an allowlisted action. Validation mirrors
        buttons/server.py::_start_run; capacity is a FIFO queue, not
        single-flight (issue #1025): beyond MAX_CONCURRENT_RUNS the
        run waits queued instead of failing, so schedules pend
        instead of going red."""
        if action not in ALLOWED_ACTIONS:
            return None, "unknown action"
        # Reap outside the create lock: cancel_run takes _lock itself.
        self.reap_stale()
        with self._lock:
            # Store-site sweep (#451): stale completeds go before
            # the new record lands. The bump below covers both.
            self._evict_completed_locked()
            run_id = uuid.uuid4().hex[:12]
            now = time.time()
            queued = len(self.active_ids) >= MAX_CONCURRENT_RUNS
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
                "queued": queued,
            }
            if td_argv is not None:
                run["td_argv"] = td_argv
            self._seq += 1
            self.runs[run_id] = run
            if not queued:
                self.active_ids.add(run_id)
            self._bump()
            self._persist_locked()
        return run_id, None

    def queue_position(self, run_id):
        """1-based FIFO position among waiting runs; 0 when the run
        holds a slot, is done, or is unknown. Pure read for the
        dispatch response and the job rows."""
        with self._lock:
            run = self.runs.get(run_id)
            if (run is None or run.get("done")
                    or run_id in self.active_ids):
                return 0
            ahead = [r for r in self.runs.values()
                     if not r.get("done") and r.get("queued")
                     and r.get("seq", 0) < run.get("seq", 0)]
            return len(ahead) + 1

    def acquire(self, run_id):
        """Worker gate: block until the run holds a slot.

        Returns True when the run is active and may execute, False
        when it was cancelled, cleared, or finished before its turn
        (the worker must exit without running or finishing). FIFO
        promotion wakes waiters via the push condition, and cancel
        bumps wake them too — no polling, no timeout: promotion on
        every finish/cancel guarantees progress.
        """
        with self._lock:
            while True:
                run = self.runs.get(run_id)
                if run is None or run.get("done"):
                    return False
                if run_id in self.active_ids:
                    return True
                self._cond.wait()

    def _promote_locked(self):
        """Fill free slots with the oldest queued runs, FIFO. Caller
        must hold _lock; bumps + notifies when anything moves."""
        while len(self.active_ids) < MAX_CONCURRENT_RUNS:
            nxt = [r for r in self.runs.values()
                   if not r.get("done") and r.get("queued")
                   and r.get("id") not in self.active_ids]
            if not nxt:
                return
            nxt.sort(key=lambda r: r.get("seq", 0))
            nxt[0]["queued"] = False
            self.active_ids.add(nxt[0]["id"])
            self._bump()
        self._cond.notify_all()

    def reap_stale(self):
        """Cancel active runs silent longer than RUN_STALE_TTL and
        finish the ones whose worker is already gone, then promote.

        A wedged run must not jam the queue forever (issue #1025);
        runs awaiting approval are exempt — a human, not output, is
        what they wait for. Locking is staged (scan, cancel outside
        the lock, finish under it) because cancel_run takes _lock.
        """
        now = time.time()
        with self._lock:
            stale = [rid for rid in self.active_ids
                     if (lambda r: r is not None and not r.get("done")
                        and not r.get("awaiting_approval")
                        and now - r.get("last_output_at", now)
                        > RUN_STALE_TTL)(self.runs.get(rid))]
        for rid in stale:
            run = self.runs.get(rid)
            if run is not None and not run.get("done"):
                self.cancel_run(rid)
        with self._lock:
            for rid in list(self.active_ids):
                run = self.runs.get(rid)
                if run is None or run.get("done"):
                    self.active_ids.discard(rid)
                    continue
                if not run.get("cancel_requested"):
                    continue
                proc = run.get("_proc")
                try:
                    exited = proc is None or proc.poll() is not None
                except Exception:
                    exited = True
                if exited:
                    run["done"] = True
                    if run.get("exit_code") is None:
                        run["exit_code"] = 130
                    run["finished_at"] = time.time()
                    run["output"] += (
                        "note: reaped after silence exceeded "
                        "the stale lease; slot released.\n")
                    self.active_ids.discard(rid)
            self._promote_locked()
            self._persist_locked()

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
            self.active_ids.discard(run_id)
            run = self.runs.get(run_id)
            if run is None:
                return
            run["exit_code"] = exit_code
            run["done"] = True
            run["queued"] = False
            run["finished_at"] = time.time()
            # Store-site sweep (#451): the just-finished run is
            # fresh, so only older completeds can go. The bump
            # below covers both; promotion fills the freed slot.
            self._evict_completed_locked()
            self._promote_locked()
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
            # A queued run never started: finish it immediately so
            # its worker (parked in acquire) exits and the queue
            # advances, instead of running to completion later.
            if run.get("queued") and run_id not in self.active_ids:
                run["done"] = True
                run["queued"] = False
                run["exit_code"] = 130
                run["finished_at"] = time.time()
                run["output"] += "note: cancelled while queued\n"
                self._promote_locked()
                self._bump()
                self._persist_locked()
                return run_id, None
            run["cancel_requested"] = True
            proc = run.get("_proc")
            self._bump()
            self._persist_locked()
        if proc is not None:
            _terminate_tree(proc, grace)
        return run_id, None

    def clear_run(self, run_id=None):
        """Clear stored output only. Exact port of the /api/clear
        handler: with no id, clears the oldest active run, else the
        newest run (several may be active now that single-flight is
        a queue)."""
        with self._lock:
            run = self.runs.get(run_id) if run_id else None
            if run is None:
                actives = [self.runs[rid] for rid in self.active_ids
                           if rid in self.runs]
                if actives:
                    run = min(actives, key=lambda r: r.get("seq", 0))
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
        lists). The all-path refuses with (None, err) while any run
        is active (#512): deleting an active record would orphan
        its procs. Cancel the active runs first, then clear-all.
        Refusals bump nothing, like any other no-op.
        """
        with self._lock:
            if all_jobs:
                if self.active_ids:
                    oldest = min(self.active_ids)
                    return (None,
                            "a run is still active: "
                            f"{oldest}")
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

    def _evict_completed_locked(self, now=None):
        """Drop done runs finished over COMPLETED_RUN_TTL ago.

        Returns the evicted count. Caller must hold _lock; the
        caller bumps + persists (start/finish already do;
        evict_completed does when the count is nonzero).
        Unfinished runs never go, however old; done runs with no
        usable timestamp stay — without an age, eviction would be
        a guess.
        """
        at = time.time() if now is None else now
        stale = []
        for rid, run in self.runs.items():
            if not run.get("done"):
                continue
            stamp = run.get("finished_at")
            if stamp is None:
                stamp = run.get("started_at")
            if isinstance(stamp, bool) or \
                    not isinstance(stamp, (int, float)):
                continue
            if at - stamp > COMPLETED_RUN_TTL:
                stale.append(rid)
        for rid in stale:
            del self.runs[rid]
        return len(stale)

    def evict_completed(self, now=None):
        """Sweep stale completeds; returns the evicted count (#451).

        The store sites sweep on their own; this is the explicit
        seam for tests and future scheduled callers. No-ops bump
        nothing, like any other no-op.
        """
        with self._lock:
            count = self._evict_completed_locked(now)
            if count:
                self._bump()
                self._persist_locked()
        return count

    def list_jobs(self):
        """Job-shaped rows over runs, newest first (#451).

        Runs are the one collection: each run maps to the job
        shape the attention seams read (id/name/repo/done/
        exit_code/created_at/error). Failed runs carry their last
        output line as error so the attention item says what
        broke, not just which run. Waiting runs carry their FIFO
        place-in-line (#1025).
        """
        with self._lock:
            waiting = sorted(
                (r for r in self.runs.values()
                 if not r.get("done") and r.get("queued")
                 and r.get("id") not in self.active_ids),
                key=lambda r: r.get("seq", 0))
            positions = {r.get("id"): i + 1
                         for i, r in enumerate(waiting)}
            ordered = sorted(self.runs.values(),
                             key=lambda r: r.get("seq", 0), reverse=True)
            return [_run_to_job(r, positions.get(r.get("id"), 0))
                    for r in ordered]
