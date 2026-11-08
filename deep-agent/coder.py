"""Session coder: implement a task brief inside a worktree (disk writes).

Spawner-compatible executor CLI (replaces pi): the session supervisor
runs ``coder -p --system-prompt <ctx> [--model M] <brief>`` with
cwd=worktree. The agent gets real file tools rooted at the worktree
(FilesystemBackend, virtual_mode) plus a scoped run_check tool for
tests/lint; no shell otherwise. It never commits, pushes, or opens
PRs — the pipeline (submit-pr) does that after the scope gate.

Plan-then-implement (#654): briefs naming 2+ files (or
CODER_PLAN=always) pause after a planning step with the standard
``INTERRUPT:`` blob and exit 3; ``coder --resume approve|reject
--thread T`` continues or aborts the same thread. Single-file
briefs skip planning (CODER_PLAN=never forces the skip).

Safety layers: worktree root enforced by virtual_mode; run_check runs
an allowlisted command set with cwd pinned and output capped; the
planning agent denies all file writes, so zero writes land before
plan approval; scope is enforced at submit, not mid-run.

Stdout contract: progress goes to stderr; stdout carries the final
summary only (or the INTERRUPT blob on a plan pause). Exit 0 on
success, 1 with a Sorry line on failure, 3 on plan pause.
Every run is traced + ledgered (recorder totals, like deep_chat).

Env: CODER_TIMEOUT (wall seconds, default 1800; also the plan
freshness cap on resume), CODER_MODEL (override), CODER_THINK=1
(Ollama thinking traces), CODER_VERIFY_ROUNDS (verify re-prompts,
default 2, 0 disables), CODER_PLAN (always|never|unset auto),
plus the model_router knobs (ASK_MODEL, OLLAMA_URL, NEBIUS_*).
"""

import argparse
import json
import os
import re
import shlex
import sys
import threading
import time

import cli

cli.bootstrap()

# run_check allowlist lives in s5tools now, shared with the chat/ask
# run_repo_command tool (#633); names re-exported here so existing
# imports (coder.check_allowed, tests) keep working unchanged.
from s5tools import (  # noqa: E402,F401
    CHECK_OUTPUT_LIMIT,
    CHECK_PREFIXES,
    CHECK_TIMEOUT,
    TEST_FILE_RE,
    check_allowed,
    check_env,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CODER_DB = os.path.join(HERE, ".coder_threads", "coder.db")

# Coding needs more graph steps than chat turns (read/plan/edit/verify).
RECURSION_LIMIT = 100
DEFAULT_TIMEOUT = 1800
DEFAULT_VERIFY_ROUNDS = 2

# Dithering guard (#1160 gap 3): this many consecutive identical
# failing run_check calls means the agent is spinning, not
# progressing — the tool tells it to stop, and the verify loop
# refuses further re-prompts.
DITHER_THRESHOLD = 5

DITHER_NOTICE = (
    "DITHERING: this exact command has failed {streak} times in a "
    "row. Stop retrying it unchanged — fix the code, or run a "
    "different check. Further identical retries will keep failing."
)

# Re-prompt sent when a coder turn finishes with no passing run_check
# in the trace. Same thread, so the agent sees its own prior work.
VERIFY_NUDGE = (
    "No passing run_check was recorded for your edits. Run the "
    "checks for your edits with run_check and fix any failures; "
    "do not finish without a passing run."
)

CODER_RULES = """You are implementing the task below in the checkout at {worktree}. Rules:
- File paths are relative to the worktree root: /x means {worktree}/x.
- Only change files the task needs; when a scope list is given, stay inside it.
- Read before editing; keep diffs minimal and match surrounding style.
- Verify with run_check (the touched area's tests/lint) before finishing.
- Do NOT commit, push, or open PRs; leave changes in the working tree.
- End with a short summary: files changed + verification run."""

# Plan-then-implement (#654): deterministic multi-file bar, no LLM
# judge. CODER_PLAN=always forces planning, =never skips it.
PLAN_ENV = "CODER_PLAN"
PLAN_LIMIT = 8000

# File-like tokens: a path with a slash, or a name with a
# letter-led extension (so v1.2 never counts). URLs are stripped
# first; e.g./i.e. are stoplisted. A heuristic: prose with two
# dotted abbreviations still plans (harmless — one approval step),
# and CODER_PLAN=never escapes it.
FILE_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+"
    r"|[A-Za-z0-9_-]+\.[A-Za-z][A-Za-z0-9]{0,4})\b")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
PROSE_ABBREVS = frozenset({"e.g", "i.e"})

PLAN_PROMPT = """Before writing any code, produce an implementation plan for the task below. Call the propose_plan tool exactly once with your plan, then stop. Do not call any other tool. Do not write or edit any file.

Plan shape — numbered steps; each step names its files and its checks:
PLAN:
1. <step> [files: <paths>] [checks: <commands>]
2. ...

Task:
{brief}"""

# Replan/runbook skill for the plan + validated_replanner loop. The
# registry tolerates a missing skills dir (worktrees), yielding PLAN_PROMPT
# unchanged.
try:
    import skills_registry as _skills_registry

    PLAN_PROMPT = _skills_registry.SkillsRegistry().inject_skills_for_node(
        "validated_replanner_node", PLAN_PROMPT)
except Exception:
    pass

PLAN_NUDGE = ("Call the propose_plan tool now with your numbered plan "
              "(steps + files + checks each). Do not write code.")


def files_named(*texts):
    """Distinct file-like tokens across texts (URLs stripped). Pure."""
    found = set()
    for text in texts:
        if not isinstance(text, str):
            continue
        scrubbed = URL_RE.sub(" ", text)
        for match in FILE_TOKEN_RE.findall(scrubbed):
            if match.lower() not in PROSE_ABBREVS:
                found.add(match)
    return sorted(found)


def needs_plan(brief, context=""):
    """True when the brief clears the planning bar. Pure.

    CODER_PLAN=always forces planning, =never skips it; otherwise
    2+ distinct file-like tokens in brief+context trigger a plan.
    """
    mode = os.environ.get(PLAN_ENV, "").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    return len(files_named(brief, context)) >= 2


def validate_plan(plan):
    """Validate a propose_plan payload. Returns (ok, text). Pure."""
    if not isinstance(plan, str) or not plan.strip():
        return False, "REJECTED: plan must be non-empty text"
    if len(plan) > PLAN_LIMIT:
        return False, ("REJECTED: plan is too long "
                       f"(max {PLAN_LIMIT} chars)")
    return True, "PLAN recorded:\n" + plan.strip()


def parse_args(argv):
    """Spawner-shaped argv: -p --system-prompt CTX [--model M] BRIEF.

    --resume approve|reject continues a paused plan thread (brief
    then becomes an optional override for the ledgered brief).
    """
    parser = argparse.ArgumentParser(prog="coder")
    parser.add_argument("-p", action="store_true")
    parser.add_argument("--system-prompt", default="")
    parser.add_argument("--model", default=None)
    parser.add_argument("--worktree", default=None)
    parser.add_argument("--thread", default=None)
    parser.add_argument("--resume", default=None,
                        choices=["approve", "reject"])
    parser.add_argument("brief", nargs="?")
    args = parser.parse_args(argv)
    if not args.brief and not args.resume:
        parser.error("BRIEF is required")
    return args


def build_system(worktree, context):
    """Spawner context plus coder rules (context may be empty direct)."""
    rules = CODER_RULES.format(worktree=worktree)
    if context and context.strip():
        return context.strip() + "\n\n---\n\n" + rules
    return rules


def build_model(model_override=None):
    """LLM via model_router (Ollama no-think default). Venv-only."""
    import model_router  # noqa: E402

    spec = model_router.resolve_llm()
    if model_override:
        spec = dict(spec, model=model_override)
    think = os.environ.get("CODER_THINK", "") == "1"
    return model_router.build_chat_model(spec=spec, think=think)


def _default_plan_fn(brief, worktree_root, context):
    """Out-of-the-box planner: model proposes plan + files, parsed
    deterministically. Injectable seam is build_model (monkeypatch in
    tests); the graph signature stays (brief, root, context)."""
    model, _spec = build_model()
    context = dict(context or {})
    system = (context.get("skills") or "")
    if system:
        system = system.strip() + "\n\n---\n\n"
    prompt = (
        f"{system}Propose an implementation plan for this brief. "
        "Reply with a numbered step list, one step per line, each "
        "naming its files like [files: path1, path2]. "
        "Keep it to the brief; no questions.\n\n"
        f"Brief:\n{brief}\n\nErrors so far:\n{context.get('errors', [])}"
    )
    text = _model_text(model.invoke(prompt))
    return {
        "plan": text.strip(),
        "allowed_files": files_named(text),
        "verification_commands": [],
    }


def _default_implement_fn(brief, plan, worktree_root, errors):
    """Out-of-the-box implementer: model returns notes text."""
    model, _spec = build_model()
    prompt = (
        "Implement this plan in the worktree using the repo tools. "
        "Reply with short implementation notes when done.\n\n"
        f"Brief:\n{brief}\n\nPlan:\n{plan}\n\n"
        f"Worktree: {worktree_root}\nErrors so far: {errors}"
    )
    return _model_text(model.invoke(prompt)).strip()


def _model_text(reply):
    """Text out of a model reply (content str or message object)."""
    if isinstance(reply, str):
        return reply
    return str(getattr(reply, "content", reply) or "")


def _inject_replanner_skills():
    """Node-targeted skills for validated_replanner_node at replan
    time (import-time PLAN_PROMPT injection is static; the replan
    context must re-query the registry). Returns "" when the registry
    is unavailable or has nothing for the node."""
    try:
        import skills_registry as _skills_registry
        injected = _skills_registry.SkillsRegistry(
        ).inject_skills_for_node(
            "validated_replanner_node", PLAN_PROMPT)
        return injected if injected != PLAN_PROMPT else ""
    except Exception:
        return ""


def build_check_tool(worktree):
    """run_check tool: allowlisted verification commands in worktree."""
    from langchain_core.tools import tool  # noqa: E402

    allowed = ", ".join(" ".join(p) for p in CHECK_PREFIXES)
    # Per-tool dither streak: consecutive identical failing
    # commands. Any different command (or a pass) resets it.
    spin = {"last": None, "streak": 0}

    @tool
    def run_check(command: str) -> str:
        """Run a verification command (tests/lint/git-inspect) in the worktree.

        Args:
            command: Full command line; must start with one of:
                {allowed}.
        """
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return f"REJECTED: bad command line: {exc}"
        if not argv or not check_allowed(argv):
            return f"REJECTED: command must start with one of: {allowed}"
        import subprocess  # noqa: E402

        try:
            proc = subprocess.run(
                argv, cwd=worktree, capture_output=True, text=True,
                timeout=CHECK_TIMEOUT,
                env={**os.environ, **check_env(worktree)})
        except subprocess.TimeoutExpired:
            return f"TIMEOUT after {CHECK_TIMEOUT}s: {command[:200]}"
        except OSError as exc:
            return f"exec failed: {exc}"
        out = ((proc.stdout or "") + (proc.stderr or ""))[-CHECK_OUTPUT_LIMIT:]
        result = f"exit={proc.returncode}\n{out}"
        if proc.returncode != 0 and command == spin["last"]:
            spin["streak"] += 1
        else:
            spin["streak"] = 0 if proc.returncode == 0 else 1
        spin["last"] = command
        if spin["streak"] >= DITHER_THRESHOLD:
            result += "\n" + DITHER_NOTICE.format(streak=spin["streak"])
        return result

    run_check.description = run_check.description.format(allowed=allowed)
    return run_check


def build_agent(worktree, system, model=None, recorder=None,
                checkpointer=None, tools=None, interrupt_on=None,
                permissions=None, spec=None):
    """create_deep_agent coder: disk backend rooted at worktree. Venv-only.

    tools defaults to [run_check]; interrupt_on/permissions pass
    through to create_deep_agent (the plan agent denies writes and
    pauses on propose_plan; the implement agent leaves all three
    defaulted, exactly as before). spec is the model_router spec the
    model was built from: when given, the default general-purpose
    subagent is disabled for it, so the read-only-until-submit
    contract holds for the whole harness (no task hop with ungated
    file tools around the submit scope gate). (#539)
    """
    from deepagents import create_deep_agent  # noqa: E402
    from deepagents.backends import FilesystemBackend  # noqa: E402

    import model_router  # noqa: E402

    if model is None:
        model, _ = build_model()
    if spec is not None:
        model_router.disable_general_purpose(spec)
    kwargs = {}
    if recorder is not None:
        import observe  # noqa: E402

        kwargs["middleware"] = observe.trace_middleware(recorder)
    return create_deep_agent(
        model=model,
        tools=(list(tools) if tools is not None
               else [build_check_tool(worktree)]),
        system_prompt=system,
        backend=FilesystemBackend(root_dir=worktree, virtual_mode=True),
        interrupt_on=interrupt_on,
        permissions=permissions,
        checkpointer=checkpointer,
        **kwargs,
    )


# deep_chat HITL rails, coder-side: propose_plan pauses for
# approve/reject exactly like propose_action; the pause blob,
# resume payload, and verdict mapping reuse deep_chat's helpers
# (imported, not forked — deep_chat itself is untouched).
CODER_INTERRUPT_ON = {
    "propose_plan": {
        "allowed_decisions": ["approve", "reject"],
        "description": "Review the implementation plan before it runs.",
    }
}


def build_plan_tool():
    """propose_plan tool: formats one implementation plan. Venv-only."""
    from langchain_core.tools import tool  # noqa: E402

    @tool
    def propose_plan(plan: str) -> str:
        """Propose the implementation plan for this task (human-approved).

        Call exactly once with the numbered plan (steps + files +
        checks each), then STOP and wait for the human's
        approve/reject decision. Never writes anything.

        Args:
            plan: The full plan text, from PLAN: onward.
        """
        _ok, text = validate_plan(plan)
        return text

    return propose_plan


def build_plan_agent(worktree, system, model=None, recorder=None,
                     checkpointer=None, spec=None):
    """Planning agent: propose_plan only, writes denied, pauses on it.

    Same model/backend/threading as the implement agent; the write
    deny (FilesystemPermission, like deep_chat's harness gate) is
    what guarantees zero file writes before approval. spec passes
    through to build_agent for the general-purpose disable. Venv-only.
    """
    from deepagents.middleware.filesystem import (  # noqa: E402
        FilesystemPermission)

    return build_agent(
        worktree, system, model=model, recorder=recorder,
        checkpointer=checkpointer, tools=[build_plan_tool()],
        interrupt_on=dict(CODER_INTERRUPT_ON),
        permissions=[FilesystemPermission(operations=["write"],
                                          paths=["/**"], mode="deny")],
        spec=spec)


def _timeout():
    try:
        return max(60, int(os.environ.get("CODER_TIMEOUT", DEFAULT_TIMEOUT)))
    except ValueError:
        return DEFAULT_TIMEOUT


def verify_rounds():
    """Verify re-prompt bound from CODER_VERIFY_ROUNDS.

    Default 2; 0 disables the gate (legacy single-invoke behavior).
    """
    try:
        return max(0, int(os.environ.get("CODER_VERIFY_ROUNDS",
                                         DEFAULT_VERIFY_ROUNDS)))
    except ValueError:
        return DEFAULT_VERIFY_ROUNDS


def trace_has_passing_check(trace_path):
    """True when the trace records a passing run_check verdict.

    run_check returns exit=<n> TEXT and never raises, so a failing
    suite still records ok=True: call presence proves nothing and the
    gate matches the exit=0 prefix in result_preview instead. Never
    raises: unreadable traces read as unverified (fail closed into a
    verify re-prompt).
    """
    try:
        with open(trace_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") != "tool_call":
                    continue
                if event.get("name") != "run_check":
                    continue
                preview = event.get("result_preview", "")
                if preview == "exit=0" or preview.startswith("exit=0\n"):
                    return True
    except (OSError, TypeError):
        return False
    return False


def trace_check_history(trace_path):
    """[(command, exit_code)] for run_check calls in a trace, in order.

    Only strictly consecutive tool_call events count toward a
    streak: any other tool call between two checks (an edit, a
    read) means the agent changed approach, not dithered. Never
    raises: unreadable traces read as empty (no dither).
    """
    history = []
    try:
        with open(trace_path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") != "tool_call":
                    continue
                if event.get("name") != "run_check":
                    history = []
                    continue
                preview = event.get("result_preview", "")
                match = re.match(r"exit=(\d+)",
                                 preview or "")
                code = int(match.group(1)) if match else -1
                history.append((event.get("args_preview", ""), code))
    except (OSError, TypeError):
        return []
    return history


def find_dithered_command(history, threshold=DITHER_THRESHOLD):
    """Dithering command in a check history, or None. Pure.

    Returns the command when the last `threshold` entries are the
    same command, all failing (nonzero exit); otherwise None. A
    pass, a different command, or too few entries read as no
    dither. Never raises.
    """
    try:
        tail = list(history or [])[-threshold:]
        if len(tail) < threshold:
            return None
        command = tail[0][0]
        if all(entry[0] == command and entry[1] != 0 for entry in tail):
            return command
        return None
    except (TypeError, ValueError, IndexError):
        return None


def trace_has_dither(trace_path, threshold=DITHER_THRESHOLD):
    """True when a trace ends in a dithering check streak. Never raises."""
    try:
        return find_dithered_command(
            trace_check_history(trace_path), threshold) is not None
    except Exception:  # noqa: BLE001 - dither check never breaks a turn
        return False


def run_bounded(fn, timeout):
    """Run fn() on a daemon thread with a wall-clock timeout.

    Replaces ThreadPoolExecutor + future.result(timeout) inside a
    `with` block: on timeout that pattern's __exit__ calls
    shutdown(wait=True), which blocks until the stuck worker
    finishes — the TimeoutError handler never runs and the cap is
    unenforceable exactly when it matters (#514). The daemon worker
    here never blocks the caller or interpreter exit: on timeout
    this raises TimeoutError immediately, the caller closes the
    recorder/ledgers the error/Sorry-exits, and the process exits
    even with the worker still stuck. A stuck worker may still race
    the recorder close or the sqlite handle on its way out; those
    errors die with the worker thread, never with the timeout path.
    """
    box = {}

    def _target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in caller
            box["error"] = exc

    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"timed out after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def run_with_verify(initial_invoke, reinvoke, has_passing_check,
                    max_rounds, dithered=None):
    """One invoke plus bounded verify re-invokes.

    initial_invoke() runs first; while has_passing_check() is false,
    reinvoke() runs up to max_rounds times. Returns
    (result, verify_rounds, verified). When dithered() reports a
    repeated-identical-failure streak, re-invokes stop early
    (#1160 gap 3: fail fast instead of burning verify rounds on a
    spinning agent).
    """
    result = initial_invoke()
    rounds = 0
    verified = has_passing_check()
    while not verified and rounds < max_rounds:
        if dithered is not None and dithered():
            break
        rounds += 1
        result = reinvoke()
        verified = has_passing_check()
    return result, rounds, verified


def extract_reply(result):
    """Best-effort final-message text from an agent result. Never raises."""
    try:
        out_messages = result.get("messages", []) if result else []
        if out_messages:
            last = out_messages[-1]
            content = getattr(last, "content", last)
            text = (content if isinstance(content, str)
                    else json.dumps(content)[:2000])
            return text.strip()
    except Exception:  # noqa: BLE001 - summary best effort
        pass
    return ""


def mark_unverified(summary):
    """Prefix a summary that exhausted the verify bound without a pass."""
    if summary:
        return "[unverified] " + summary
    return "[unverified]"


def run_coder(worktree, brief, system, thread, timeout):
    """Invoke the coder with a verify gate.

    Returns (summary, usage, calls, started, label, trace, verified,
    verify_rounds). Unless CODER_VERIFY_ROUNDS=0, a turn with no
    passing run_check in the trace re-invokes (same thread) with the
    verify nudge, up to the round bound; an exhausted bound prefixes
    the summary with [unverified] and still exits clean. Venv-only.
    """
    import observe  # noqa: E402
    from langchain_core.messages import HumanMessage  # noqa: E402

    import deep_chat  # noqa: E402

    started = time.time()
    recorder = observe.TraceRecorder(
        observe.default_trace_path("coder", thread))
    model, spec = build_model(os.environ.get("CODER_MODEL") or None)
    label = f"{spec['provider']}:{spec['model']}"
    print(f"coder via {label}", file=sys.stderr, flush=True)
    recorder.run_start("coder.py", thread, label)
    saver, _persistent, holder = deep_chat.open_checkpointer(CODER_DB)
    agent = build_agent(worktree, system, model=model, recorder=recorder,
                        checkpointer=saver, spec=spec)
    config = {"configurable": {"thread_id": thread}}

    def _invoke(msgs):
        return agent.invoke(
            {"messages": msgs},
            config={**config, "recursion_limit": RECURSION_LIMIT})

    max_rounds = verify_rounds()
    try:
        result, rounds, verified = run_with_verify(
            lambda: run_bounded(
                lambda: _invoke([HumanMessage(content=brief)]), timeout),
            lambda: run_bounded(
                lambda: _invoke([HumanMessage(content=VERIFY_NUDGE)]),
                timeout),
            lambda: trace_has_passing_check(recorder.path),
            max_rounds,
            dithered=lambda: trace_has_dither(recorder.path),
        )
    except TimeoutError:
        recorder.run_end(False)
        recorder.close()
        raise RuntimeError(f"coder timed out after {timeout}s")
    except Exception:
        recorder.run_end(False)
        recorder.close()
        raise
    # holder stays referenced (with saver + agent) until return, so the
    # DB connection outlives the run.
    _ = holder
    reply = extract_reply(result)
    if not verified and max_rounds > 0:
        reply = mark_unverified(reply)
    recorder.run_end(True)
    recorder.close()
    usage, calls = recorder.totals()
    print(f"trace: {recorder.path}", file=sys.stderr)
    return reply, usage, calls, started, label, recorder.path, verified, rounds


def latest_plan(thread):
    """Newest plan_pending ledger row for thread, or None. Never raises."""
    import ledger  # noqa: E402

    try:
        with open(ledger.LEDGER, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return None
    best = None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("script") != "coder.py":
            continue
        if entry.get("thread_id") != thread:
            continue
        if (entry.get("extra") or {}).get("coder_phase") != "plan_pending":
            continue
        best = entry
    if best is None:
        return None
    extra = best.get("extra") or {}
    return {"ts": best.get("ts"), "model": best.get("model") or "unknown",
            "brief": extra.get("brief") or "",
            "worktree": extra.get("worktree") or "",
            "system": extra.get("system") or "",
            "plan": extra.get("plan") or "",
            "trace": extra.get("trace") or ""}


def plan_is_stale(ts, now, cap):
    """True when a plan timestamp is older than cap seconds. Pure.

    Unparseable timestamps read as stale (fail closed: never
    implement from an undated plan).
    """
    try:
        age = float(now) - float(ts)
    except (TypeError, ValueError):
        return True
    return age > cap


def _plan_requests(pending):
    """action_requests of the first pending interrupt (defensive)."""
    try:
        value = pending[0].get("value") or {}
    except (AttributeError, IndexError, TypeError):
        return []
    if not isinstance(value, dict):
        return []
    requests = value.get("action_requests", []) or []
    return requests if isinstance(requests, list) else []


def plan_and_pause(worktree, brief, system, thread, timeout):
    """Planning phase: gated PLAN, then the standard INTERRUPT pause.

    Invokes the plan agent (writes denied); when propose_plan
    pauses, the plan is traced + ledgered as plan_pending and this
    returns deep_chat's exit-3 with the INTERRUPT blob on stdout.
    A model that never calls propose_plan (initial + one nudge)
    fails closed: plan_failed ledgered, Sorry, exit 1. Venv-only.
    """
    import observe  # noqa: E402
    from langchain_core.messages import (  # noqa: E402
        AIMessage, HumanMessage)

    import deep_chat  # noqa: E402
    import ledger  # noqa: E402

    started = time.time()
    recorder = observe.TraceRecorder(
        observe.default_trace_path("coder_plan", thread))
    model, spec = build_model(os.environ.get("CODER_MODEL") or None)
    label = f"{spec['provider']}:{spec['model']}"
    print(f"coder plan via {label}", file=sys.stderr, flush=True)
    recorder.run_start("coder.py", thread, label)
    saver, _persistent, holder = deep_chat.open_checkpointer(CODER_DB)
    agent = build_plan_agent(worktree, system, model=model,
                             recorder=recorder, checkpointer=saver,
                             spec=spec)
    config = {"configurable": {"thread_id": thread}}

    def _invoke(msgs):
        return agent.invoke(
            {"messages": msgs},
            config={**config, "recursion_limit": RECURSION_LIMIT})

    try:
        run_bounded(lambda: _invoke(
            [HumanMessage(
                content=PLAN_PROMPT.format(brief=brief))]), timeout)
        pending = deep_chat._pending_interrupts(agent, config)
        if not pending:
            run_bounded(lambda: _invoke(
                [HumanMessage(content=PLAN_NUDGE)]), timeout)
            pending = deep_chat._pending_interrupts(agent, config)
    except TimeoutError:
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py", label, thread,
            [HumanMessage(content=brief)], started,
            extra={"worktree": worktree, "coder_phase": "plan_failed",
                   "trace": recorder.path,
                   "error": f"plan timed out after {timeout}s"},
        )
        return cli.sorry(
            f"Sorry, the coder plan step timed out after {timeout}s.")
    except Exception as exc:  # noqa: BLE001 - sorry contract
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py", label, thread,
            [HumanMessage(content=brief)], started,
            extra={"worktree": worktree, "coder_phase": "plan_failed",
                   "trace": recorder.path, "error": str(exc)[:200]},
        )
        return cli.sorry(f"Sorry, the coder plan step failed: {exc}")
    _ = holder
    if not pending:
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py", label, thread,
            [HumanMessage(content=brief)], started,
            extra={"worktree": worktree, "coder_phase": "plan_failed",
                   "trace": recorder.path,
                   "error": "plan step did not call propose_plan"},
        )
        return cli.sorry("Sorry, the coder plan step produced no plan "
                         "for approval.")
    plan_text = ""
    requests = _plan_requests(pending)
    if requests and isinstance(requests[0], dict):
        plan_text = (requests[0].get("args") or {}).get("plan") or ""
    recorder.event("plan", plan_preview=str(plan_text)[:2000],
                   interrupt_id=pending[0].get("id"))
    recorder.run_end(True)
    recorder.close()
    usage, calls = recorder.totals()
    print(f"trace: {recorder.path}", file=sys.stderr)
    ledger.record(
        "coder.py", label, thread,
        [HumanMessage(content=brief),
         AIMessage(content=plan_text or "(empty plan)")],
        started,
        extra={"worktree": worktree, "brief": brief, "system": system,
               "plan": str(plan_text)[:PLAN_LIMIT],
               "coder_phase": "plan_pending", "trace": recorder.path},
        usage=usage, calls=calls,
    )
    return deep_chat._emit_interrupt(thread, pending)


def _implement_and_finish(worktree, brief, system, thread, timeout,
                          extra=None):
    """run_coder plus the ledger row + stdout summary. Returns exit code."""
    import ledger  # noqa: E402
    from langchain_core.messages import (  # noqa: E402
        AIMessage, HumanMessage)

    label = "unknown"
    try:
        (summary, usage, calls, started, label, trace, verified,
         rounds) = run_coder(
            worktree, brief, system, thread, timeout)
    except Exception as exc:  # noqa: BLE001 - sorry contract
        ledger.record(
            "coder.py", label,
            thread, [{"role": "user", "content": brief}],
            time.time(),
            extra={"worktree": worktree, "error": str(exc)[:200],
                   **(extra or {})},
        )
        return cli.sorry(f"Sorry, the coder failed: {exc}")
    ledger.record(
        "coder.py", label, thread,
        [HumanMessage(content=brief),
         AIMessage(content=summary)],
        started,
        extra={"worktree": worktree, "trace": trace,
               "verified": verified, "verify_rounds": rounds,
               **(extra or {})},
        usage=usage, calls=calls,
    )
    print(summary or "(empty summary)")
    return 0


def _cmd_resume(worktree_arg, thread, verdict, brief_override=None,
                system_prompt_override=None):
    """Resume a paused coder plan. Returns an exit code.

    reject: ledger plan_rejected, no agent touched, no edits,
    exit 0 (a human decision, like deep_chat's resume-reject).
    approve: stale plans (older than CODER_TIMEOUT) abort as
    plan_stale with Sorry exit 1; fresh plans resume the SAME
    thread into the normal implement + verify path.
    """
    import observe  # noqa: E402
    from langchain_core.messages import (  # noqa: E402
        AIMessage, HumanMessage)

    import deep_chat  # noqa: E402
    import ledger  # noqa: E402

    if verdict not in ("approve", "reject"):
        return cli.usage_error(
            f"bad verdict {verdict!r}: want 'approve'/'reject'")
    started = time.time()
    plan = latest_plan(thread)
    if plan is None:
        return cli.sorry(f"Sorry, no pending plan for thread {thread!r}.")
    worktree = os.path.abspath(
        worktree_arg or plan["worktree"] or os.getcwd())
    if not os.path.isdir(worktree):
        return cli.sorry(f"Sorry, no worktree at {worktree}.")
    if verdict == "reject":
        recorder = observe.TraceRecorder(
            observe.default_trace_path("coder_resume", thread))
        recorder.run_start("coder.py", thread, plan["model"])
        recorder.event("plan_decision", verdict="reject")
        recorder.run_end(True)
        recorder.close()
        ledger.record(
            "coder.py", plan["model"], thread,
            [HumanMessage(content=plan["brief"] or "(brief unavailable)"),
             AIMessage(content="plan rejected")],
            started,
            extra={"worktree": worktree, "coder_phase": "plan_rejected",
                   "trace": recorder.path},
        )
        print("plan rejected, no changes made")
        return 0
    timeout = _timeout()
    if plan_is_stale(plan["ts"], time.time(), timeout):
        ledger.record(
            "coder.py", plan["model"], thread,
            [HumanMessage(content=plan["brief"] or "(brief unavailable)")],
            started,
            extra={"worktree": worktree, "coder_phase": "plan_stale",
                   "error": f"plan older than {timeout}s"},
        )
        return cli.sorry(
            f"Sorry, the plan for thread {thread!r} expired "
            f"(older than {timeout}s); re-dispatch the task.")
    brief = brief_override or plan["brief"]
    if not brief:
        return cli.sorry(
            f"Sorry, the plan for thread {thread!r} has no brief; "
            "re-dispatch the task.")
    if system_prompt_override and system_prompt_override.strip():
        system = build_system(worktree, system_prompt_override)
    elif plan["system"]:
        system = plan["system"]
    else:
        system = build_system(worktree, "")
    # Complete the paused planning turn on its own agent first, so
    # the thread carries plan→approve before implementation starts.
    recorder = observe.TraceRecorder(
        observe.default_trace_path("coder_resume", thread))
    model, spec = build_model(os.environ.get("CODER_MODEL") or None)
    label = f"{spec['provider']}:{spec['model']}"
    print(f"coder resume via {label}", file=sys.stderr, flush=True)
    recorder.run_start("coder.py", thread, label)
    saver, persistent, holder = deep_chat.open_checkpointer(CODER_DB)
    agent = build_plan_agent(worktree, system, model=model,
                             recorder=recorder, checkpointer=saver,
                             spec=spec)
    config = {"configurable": {"thread_id": thread}}
    pending = deep_chat._pending_interrupts(agent, config)
    if not pending and not persistent:
        # Cross-process resume with a memory-only checkpointer: the
        # pause lives in another process, so no interrupt is visible
        # here — but the human-approved plan text is ledgered, and
        # the explicit approve verdict carries the approval. Continue
        # from the ledger plan instead of stranding it (#1160
        # gap 2). A persistent checkpointer with no pending approval
        # still fails closed below (nothing was ever paused).
        _ = holder
        recorder.event("plan_decision", verdict="approve",
                       resume="ledger-plan-cross-process")
        recorder.run_end(True)
        recorder.close()
        resume_usage, resume_calls = recorder.totals()
        ledger.record(
            "coder.py", label, thread,
            [HumanMessage(content=brief),
             AIMessage(content=plan["plan"] or "(plan approved)")],
            started,
            extra={"worktree": worktree, "coder_phase": "plan_approved",
                   "trace": recorder.path,
                   "ledger_resume": True},
            usage=resume_usage, calls=resume_calls,
        )
        return _implement_and_finish(
            worktree, brief, system, thread, timeout,
            extra={"coder_phase": "implement", "plan_approved": True,
                   "ledger_resume": True})
    if not pending:
        recorder.run_end(False)
        recorder.close()
        return cli.sorry(
            f"Sorry, no pending approval for thread {thread!r}.")
    from langgraph.types import Command  # noqa: E402

    count = len(_plan_requests(pending)) or 1

    def _invoke(payload):
        return agent.invoke(
            payload,
            config={**config, "recursion_limit": RECURSION_LIMIT})

    try:
        run_bounded(lambda: _invoke(Command(resume=deep_chat.resume_payload(
            [deep_chat.decision_for_verdict("approve")] * count))), timeout)
    except TimeoutError:
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py", label, thread,
            [HumanMessage(content=brief)], started,
            extra={"worktree": worktree, "coder_phase": "resume_failed",
                   "trace": recorder.path,
                   "error": f"resume timed out after {timeout}s"},
        )
        return cli.sorry(
            f"Sorry, the coder resume timed out after {timeout}s.")
    except Exception as exc:  # noqa: BLE001 - sorry contract
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py", label, thread,
            [HumanMessage(content=brief)], started,
            extra={"worktree": worktree, "coder_phase": "resume_failed",
                   "trace": recorder.path, "error": str(exc)[:200]},
        )
        return cli.sorry(f"Sorry, the coder resume failed: {exc}")
    _ = holder
    recorder.event("plan_decision", verdict="approve")
    recorder.run_end(True)
    recorder.close()
    resume_usage, resume_calls = recorder.totals()
    ledger.record(
        "coder.py", label, thread,
        [HumanMessage(content=brief),
         AIMessage(content=plan["plan"] or "(plan approved)")],
        started,
        extra={"worktree": worktree, "coder_phase": "plan_approved",
               "trace": recorder.path},
        usage=resume_usage, calls=resume_calls,
    )
    return _implement_and_finish(
        worktree, brief, system, thread, timeout,
        extra={"coder_phase": "implement", "plan_approved": True})


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.resume:
        if not args.thread:
            return cli.usage_error("--thread is required with --resume")
        return _cmd_resume(
            args.worktree, args.thread, args.resume,
            brief_override=args.brief,
            system_prompt_override=args.system_prompt or None)
    worktree = os.path.abspath(args.worktree or os.getcwd())
    if not os.path.isdir(worktree):
        return cli.sorry(f"Sorry, no worktree at {worktree}.")
    thread = args.thread or f"coder-{os.getpid()}-{int(time.time())}"
    system = build_system(worktree, args.system_prompt)
    timeout = _timeout()
    print(f"thread: {thread} worktree: {worktree} timeout: {timeout}s",
          file=sys.stderr, flush=True)
    if needs_plan(args.brief, args.system_prompt):
        print("brief clears the plan bar: planning first",
              file=sys.stderr, flush=True)
        return plan_and_pause(worktree, args.brief, system, thread,
                              timeout)
    return _implement_and_finish(worktree, args.brief, system, thread,
                                 timeout)


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# InitCoderGraph: worktree-isolated coding loop (deterministic gates).
#
# init -> plan -> plan_review (HITL) -> implement -> deterministic_gate -+
#   gate pass -> persist -> END                                          |
#   gate fail -> fix (implement with errors) or validated_replanner -----+
#   replan -> plan_review (re-validate) -> implement ...
#
# Only stdlib at module top (langgraph lazy). File access outside the
# provisioned worktree is rejected by assert_inside(); provisioning and
# every gate run through `td` (never raw git), injectable for tests.
# ---------------------------------------------------------------------------


def assert_inside(worktree_root, path):
    """Real path inside worktree_root, else ValueError (escape blocked)."""
    root = os.path.realpath(worktree_root)
    full = os.path.realpath(os.path.join(root, path))
    if full != root and not full.startswith(root + os.sep):
        raise ValueError(f"path escapes worktree: {path!r}")
    return full


def read_worktree_file(worktree_root, path, limit=20000):
    """Read a file inside the worktree; escapes raise ValueError."""
    full = assert_inside(worktree_root, path)
    with open(full, encoding="utf-8", errors="ignore") as handle:
        return handle.read(limit)


def gate_spec_markdown(brief, allowed_files):
    """Minimal verify-scope spec from a brief + allowed file list."""
    lines = ["# Task", "", "## Problem Statement", brief.strip(), "",
             "## Goal", brief.strip(), "", "## Scope", "", "## Definition of Done",
             "done.", "", "## Scope & File Boundaries", "### Allowed Files"]
    for path in allowed_files or []:
        lines.append(f"- `{path}`")
    lines += ["### Forbidden Files", "- (none declared)", "",
              "## Acceptance Criteria", "- [ ] Gates pass", ""]
    return "\n".join(lines)


class InitCoderGraph:
    """Worktree-isolated implement loop. Inject fakes for tests.

    plan_fn(brief, worktree_root, context) -> {"plan": str,
        "allowed_files": [...], "verification_commands": [...]}
    implement_fn(brief, plan, worktree_root, errors) -> notes str
    td_runner(args, cwd) -> (returncode, stdout-json)
    cmd_runner(checkout, command) -> s5tools.run_repo_command-shaped str
    """

    def __init__(self, repo, issue_id, brief, plan_fn=None,
                 implement_fn=None, td_runner=None, cmd_runner=None,
                 max_fix_loops=3, max_replans=1, thread_id=None):
        import s5tools  # noqa: E402  (stdlib-only module)
        self.repo = repo
        self.issue_id = issue_id
        self.brief = brief
        self.plan_fn = plan_fn or _default_plan_fn
        self.implement_fn = implement_fn or _default_implement_fn
        self.td_runner = (td_runner if td_runner is not None
                          else self._default_td_runner)
        self.cmd_runner = (cmd_runner if cmd_runner is not None
                           else s5tools.run_repo_command)
        self.max_fix_loops = max_fix_loops
        self.max_replans = max_replans
        self.thread_id = thread_id or f"coder-{issue_id}-{int(time.time())}"

    @staticmethod
    def _default_td_runner(args, cwd):
        import s5tools
        out = s5tools.td(["--json"] + list(args), cwd)
        return 0, out

    # -- deterministic pieces -------------------------------------------

    def provision_worktree(self):
        """td worktree create --issue; returns (root, error-or-None)."""
        try:
            code, out = self.td_runner(
                ["worktree", "create", "--issue", str(self.issue_id)],
                self.repo)
        except Exception as exc:
            return None, f"td worktree create failed: {exc}"
        try:
            payload = json.loads(out) if out else {}
        except ValueError:
            return None, f"td returned non-JSON: {out[:200]}"
        if code != 0 or not isinstance(payload, dict):
            return None, (f"td worktree create exited {code}: "
                           f"{out[:200]}")
        root = payload.get("path")
        if not root or not os.path.isdir(root):
            return None, f"td returned no worktree path: {out[:200]}"
        return root, None

    def run_gate(self, worktree_root, allowed_files, verification_commands):
        """td verify-scope + td audit-code + verify commands in root.

        Returns {"passed": bool, "scope": {...}, "audit": {...},
        "commands": [{command, exit}], "errors": [...]}.
        """
        import tempfile
        errors = []
        spec_text = gate_spec_markdown(self.brief, allowed_files)
        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8")
        commands_out = []
        try:
            handle.write(spec_text)
            handle.close()
            try:
                code, out = self.td_runner(
                    ["verify-scope", "--spec-file", handle.name,
                     "--worktree", worktree_root], worktree_root)
            except Exception as exc:
                return {"passed": False, "scope": {}, "audit": {},
                        "commands": [],
                        "errors": [f"verify-scope failed: {exc}"]}
            try:
                scope = json.loads(out) if out else {}
            except ValueError:
                scope = {}
            if code != 0 or not scope.get("passed", False):
                for violation in scope.get("violations", []):
                    errors.append(f"scope violation: {violation}")
                if not errors:
                    errors.append(f"verify-scope exited {code}")
            try:
                code, out = self.td_runner(
                    ["audit-code", "--worktree", worktree_root],
                    worktree_root)
            except Exception as exc:
                return {"passed": False, "scope": scope, "audit": {},
                        "commands": [],
                        "errors": errors + [f"audit-code failed: {exc}"]}
            try:
                audit = json.loads(out) if out else {}
            except ValueError:
                audit = {}
            if code != 0 or not audit.get("passed", False):
                for item in audit.get("ast_violations", []):
                    errors.append(
                        f"audit: {item.get('file')}:{item.get('line')}: "
                        f"{item.get('rule')}: {item.get('message')}")
                for item in audit.get("slop_findings", []):
                    errors.append(
                        f"slop: {item.get('script')}: "
                        f"{str(item.get('summary', ''))[:200]}")
                if code != 0 and not errors:
                    errors.append(f"audit-code exited {code}")
            for command in verification_commands or []:
                result = self.cmd_runner(worktree_root, command)
                match = re.match(r"exit=(\d+)", result or "")
                exit_code = int(match.group(1)) if match else -1
                commands_out.append({"command": command,
                                     "exit": exit_code})
                if exit_code != 0:
                    errors.append(
                        f"command failed (exit={exit_code}): {command} "
                        f"{result[-300:]}")
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        return {"passed": not errors, "scope": scope, "audit": audit,
                "commands": commands_out, "errors": errors}

    # -- graph ------------------------------------------------------------

    def build_graph(self):
        """Compile the coding loop (needs langgraph)."""
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import Command, interrupt
        from typing_extensions import TypedDict

        if getattr(self, "_checkpointer", None) is None:
            self._checkpointer = InMemorySaver()
        outer = self

        class CoderState(TypedDict, total=False):
            worktree_root: str
            plan: str
            allowed_files: list
            verification_commands: list
            implementation_notes: str
            gate: dict
            errors: list
            fix_attempts: int
            replans: int
            plan_attempts: int
            approved: bool
            valid: bool
            persisted: bool
            fatal: str

        def init_node(state):
            if state.get("worktree_root"):
                return {}
            root, error = outer.provision_worktree()
            if error:
                return {"fatal": error}
            return {"worktree_root": root, "fix_attempts": 0,
                    "replans": 0, "plan_attempts": 0, "errors": [],
                    "valid": False}

        def plan_node(state):
            if state.get("plan") and not state.get("errors"):
                return {}
            if outer.plan_fn is None:
                return {"fatal": "plan_fn is required"}
            result = outer.plan_fn(
                outer.brief, state.get("worktree_root", ""),
                {"errors": state.get("errors", [])})
            allowed = list(result.get("allowed_files", []))
            commands = list(result.get("verification_commands", []))
            return {"plan": result.get("plan", ""),
                    "allowed_files": allowed,
                    "verification_commands": commands,
                    "approved": False}

        def plan_review_node(state):
            if state.get("approved"):
                return {}
            if state.get("plan_attempts", 0) >= 3:
                return {"fatal": "plan rejected too many times"}
            reply = interrupt({"plan": state.get("plan", ""),
                               "issue_id": outer.issue_id})
            decision = (reply or {}).get("decision", "approve")
            if decision == "approve":
                return {"approved": True,
                        "plan_attempts": state.get("plan_attempts", 0) + 1}
            notes = (reply or {}).get("notes", "")
            errors = list(state.get("errors", []))
            if notes:
                errors.append(f"plan revision requested: {notes}")
            return {"approved": False, "errors": errors,
                    "plan_attempts": state.get("plan_attempts", 0) + 1}

        def implement_node(state):
            if (state.get("implementation_notes")
                    and not state.get("errors")):
                return {}
            if outer.implement_fn is None:
                return {"fatal": "implement_fn is required"}
            notes = outer.implement_fn(
                outer.brief, state.get("plan", ""),
                state.get("worktree_root", ""),
                state.get("errors", []))
            return {"implementation_notes": notes}

        def gate_node(state):
            gate = outer.run_gate(
                state.get("worktree_root", ""),
                state.get("allowed_files", []),
                state.get("verification_commands", []))
            update = {"gate": gate, "errors": gate["errors"],
                      "valid": gate["passed"]}
            if gate["passed"]:
                return update
            return update

        def route_after_gate(state):
            if state.get("fatal"):
                return END
            if state.get("valid"):
                return "persist"
            if state.get("fix_attempts", 0) < outer.max_fix_loops:
                return "implement"
            if state.get("replans", 0) < outer.max_replans:
                return "replan"
            return END

        def fix_bookkeeping(state):
            return {"fix_attempts": state.get("fix_attempts", 0) + 1}

        def replan_node(state):
            if outer.plan_fn is None:
                return {"fatal": "plan_fn is required"}
            context = {"errors": state.get("errors", []),
                       "gate": state.get("gate", {}),
                       "replan": True}
            skills_text = _inject_replanner_skills()
            if skills_text:
                context["skills"] = skills_text
            result = outer.plan_fn(
                outer.brief, state.get("worktree_root", ""),
                context)
            return {"plan": result.get("plan", ""),
                    "allowed_files": list(result.get("allowed_files", [])),
                    "verification_commands": list(
                        result.get("verification_commands", [])),
                    "approved": False,
                    "implementation_notes": "",
                    "errors": [],
                    "replans": state.get("replans", 0) + 1,
                    "fix_attempts": 0}

        def persist_node(state):
            import decisions
            import ledger
            calls = [{"name": "td worktree create",
                      "args": f"--issue {outer.issue_id}"},
                     {"name": "td verify-scope + td audit-code",
                      "args": f"attempts={state.get('fix_attempts', 0)}"}]
            lines = decisions.build_decisions(
                calls, [f"valid={state.get('valid')}"],
                (state.get("implementation_notes", "") or "")[:500],
                reason="coder loop closed")
            ledger.record(
                "coder.py", os.environ.get("S5_CODER_MODEL", "qwen3:8b"),
                outer.thread_id, [], time.time(),
                extra={"valid": state.get("valid"),
                       "issue_id": outer.issue_id,
                       "worktree_root": state.get("worktree_root", ""),
                       "decisions": lines})
            return {"persisted": True}

        builder = StateGraph(CoderState)
        builder.add_node("init", init_node)
        builder.add_node("plan", plan_node)
        builder.add_node("plan_review", plan_review_node)
        builder.add_node("implement", implement_node)
        builder.add_node("deterministic_gate", gate_node)
        builder.add_node("fix_loop", fix_bookkeeping)
        builder.add_node("validated_replanner", replan_node)
        builder.add_node("persist", persist_node)
        builder.add_edge(START, "init")
        builder.add_conditional_edges(
            "init", lambda s: END if s.get("fatal") else "plan",
            {"plan": "plan", END: END})
        builder.add_conditional_edges(
            "plan", lambda s: END if s.get("fatal") else "plan_review",
            {"plan_review": "plan_review", END: END})
        builder.add_conditional_edges(
            "plan_review",
            lambda s: END if s.get("fatal")
            else ("implement" if s.get("approved") else "plan"),
            {"implement": "implement", "plan": "plan", END: END})
        builder.add_conditional_edges(
            "implement",
            lambda s: END if s.get("fatal") else "deterministic_gate",
            {"deterministic_gate": "deterministic_gate", END: END})
        builder.add_conditional_edges("deterministic_gate", route_after_gate,
                                      {"persist": "persist",
                                       "implement": "fix_loop", "replan": "validated_replanner",
                                       END: END})
        builder.add_edge("fix_loop", "implement")
        builder.add_edge("validated_replanner", "plan_review")
        builder.add_edge("persist", END)
        return builder.compile(checkpointer=self._checkpointer)

    def run(self, resume=None):
        """Invoke to completion (or interrupt); resume via Command."""
        from langgraph.types import Command
        graph = self.build_graph()
        config = {"configurable": {"thread_id": self.thread_id}}
        if resume is None:
            return graph.invoke({}, config)
        return graph.invoke(Command(resume=resume), config)
