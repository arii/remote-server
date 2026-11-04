"""Session coder: implement a task brief inside a worktree (disk writes).

Spawner-compatible executor CLI (replaces pi): the session supervisor
runs ``coder -p --system-prompt <ctx> [--model M] <brief>`` with
cwd=worktree. The agent gets real file tools rooted at the worktree
(FilesystemBackend, virtual_mode) plus a scoped run_check tool for
tests/lint; no shell otherwise. It never commits, pushes, or opens
PRs — the pipeline (submit-pr) does that after the scope gate.

Safety layers: worktree root enforced by virtual_mode; run_check runs
an allowlisted command set with cwd pinned and output capped; no
interrupts (the dispatch approval is the human gate; scope is
enforced at submit, not mid-run).

Stdout contract: progress goes to stderr; stdout carries the final
summary only. Exit 0 on success, 1 with a Sorry line on failure.
Every run is traced + ledgered (recorder totals, like deep_chat).

Env: CODER_TIMEOUT (wall seconds, default 1800), CODER_MODEL
(override), CODER_THINK=1 (Ollama thinking traces),
CODER_VERIFY_ROUNDS (verify re-prompts, default 2, 0 disables),
plus the model_router knobs (ASK_MODEL, OLLAMA_URL, NEBIUS_*).
"""

import argparse
import concurrent.futures
import json
import os
import shlex
import sys
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
)

HERE = os.path.dirname(os.path.abspath(__file__))
CODER_DB = os.path.join(HERE, ".coder_threads", "coder.db")

# Coding needs more graph steps than chat turns (read/plan/edit/verify).
RECURSION_LIMIT = 100
DEFAULT_TIMEOUT = 1800
DEFAULT_VERIFY_ROUNDS = 2

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


def parse_args(argv):
    """Spawner-shaped argv: -p --system-prompt CTX [--model M] BRIEF."""
    parser = argparse.ArgumentParser(prog="coder")
    parser.add_argument("-p", action="store_true")
    parser.add_argument("--system-prompt", default="")
    parser.add_argument("--model", default=None)
    parser.add_argument("--worktree", default=None)
    parser.add_argument("--thread", default=None)
    parser.add_argument("brief", nargs="?")
    args = parser.parse_args(argv)
    if not args.brief:
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
    kwargs = model_router.chat_kwargs(spec)
    think = os.environ.get("CODER_THINK", "") == "1"
    if spec["provider"] == "ollama":
        from langchain_ollama import ChatOllama  # noqa: E402

        if not think:
            kwargs["model_kwargs"] = {"think": False}
        return ChatOllama(**kwargs), spec
    from langchain_openai import ChatOpenAI  # noqa: E402

    return ChatOpenAI(api_key=os.environ["NEBIUS_API_KEY"], **kwargs), spec


def build_check_tool(worktree):
    """run_check tool: allowlisted verification commands in worktree."""
    from langchain_core.tools import tool  # noqa: E402

    allowed = ", ".join(" ".join(p) for p in CHECK_PREFIXES)

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
                timeout=CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            return f"TIMEOUT after {CHECK_TIMEOUT}s: {command[:200]}"
        except OSError as exc:
            return f"exec failed: {exc}"
        out = ((proc.stdout or "") + (proc.stderr or ""))[-CHECK_OUTPUT_LIMIT:]
        return f"exit={proc.returncode}\n{out}"

    run_check.description = run_check.description.format(allowed=allowed)
    return run_check


def build_agent(worktree, system, model=None, recorder=None,
                checkpointer=None):
    """create_deep_agent coder: disk backend rooted at worktree. Venv-only."""
    from deepagents import create_deep_agent  # noqa: E402
    from deepagents.backends import FilesystemBackend  # noqa: E402

    if model is None:
        model, _ = build_model()
    kwargs = {}
    if recorder is not None:
        import observe  # noqa: E402

        kwargs["middleware"] = observe.trace_middleware(recorder)
    return create_deep_agent(
        model=model,
        tools=[build_check_tool(worktree)],
        system_prompt=system,
        backend=FilesystemBackend(root_dir=worktree, virtual_mode=True),
        checkpointer=checkpointer,
        **kwargs,
    )


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


def run_with_verify(initial_invoke, reinvoke, has_passing_check,
                    max_rounds):
    """One invoke plus bounded verify re-invokes.

    initial_invoke() runs first; while has_passing_check() is false,
    reinvoke() runs up to max_rounds times. Returns
    (result, verify_rounds, verified).
    """
    result = initial_invoke()
    rounds = 0
    verified = has_passing_check()
    while not verified and rounds < max_rounds:
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
                        checkpointer=saver)
    config = {"configurable": {"thread_id": thread}}

    def _invoke(msgs):
        return agent.invoke(
            {"messages": msgs},
            config={**config, "recursion_limit": RECURSION_LIMIT})

    max_rounds = verify_rounds()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            def _submit(msgs):
                return pool.submit(_invoke, msgs).result(timeout=timeout)

            result, rounds, verified = run_with_verify(
                lambda: _submit([HumanMessage(content=brief)]),
                lambda: _submit([HumanMessage(content=VERIFY_NUDGE)]),
                lambda: trace_has_passing_check(recorder.path),
                max_rounds,
            )
    except concurrent.futures.TimeoutError:
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


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    import ledger  # noqa: E402

    worktree = os.path.abspath(args.worktree or os.getcwd())
    if not os.path.isdir(worktree):
        return cli.sorry(f"Sorry, no worktree at {worktree}.")
    thread = args.thread or f"coder-{os.getpid()}-{int(time.time())}"
    system = build_system(worktree, args.system_prompt)
    timeout = _timeout()
    print(f"thread: {thread} worktree: {worktree} timeout: {timeout}s",
          file=sys.stderr, flush=True)
    label = "unknown"
    try:
        (summary, usage, calls, started, label, trace, verified,
         rounds) = run_coder(
            worktree, args.brief, system, thread, timeout)
    except Exception as exc:  # noqa: BLE001 - sorry contract
        ledger.record(
            "coder.py", label,
            thread, [{"role": "user", "content": args.brief}],
            time.time(),
            extra={"worktree": worktree, "error": str(exc)[:200]},
        )
        return cli.sorry(f"Sorry, the coder failed: {exc}")
    from langchain_core.messages import (  # noqa: E402
        AIMessage, HumanMessage)

    ledger.record(
        "coder.py", label, thread,
        [HumanMessage(content=args.brief),
         AIMessage(content=summary)],
        started,
        extra={"worktree": worktree, "trace": trace,
               "verified": verified, "verify_rounds": rounds},
        usage=usage, calls=calls,
    )
    print(summary or "(empty summary)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
