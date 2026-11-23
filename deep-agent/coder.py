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
freshness cap on resume), CODER_MODEL (override), CODER_STRONG_MODEL (model
used when the pipeline escalates to model_tier 'strong'; required
then), CODER_THINK=1
(Ollama thinking traces), CODER_VERIFY_ROUNDS (verify re-prompts,
default 2, 0 disables), CODER_PLAN (always|never|unset auto),
CODER_FALLBACK_MODEL (explicit TwT model after initial default-route model-cap
exhaustion; defaults to Nebius GLM-5.3-Flash, empty disables),
plus the model_router knobs (ASK_MODEL, OLLAMA_URL, NEBIUS_*).
"""

import argparse
import contextvars
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

from backend.shared import cli

cli.bootstrap()

# run_check allowlist lives in s5tools now, shared with the chat/ask
# run_repo_command tool (#633); names re-exported here so existing
# imports (coder.check_allowed, tests) keep working unchanged.
from backend.shared.s5tools import (  # noqa: E402,F401
    CHECK_OUTPUT_LIMIT,
    CHECK_PREFIXES,
    CHECK_TIMEOUT,
    TEST_FILE_RE,
    allowed_commands_text,
    check_allowed,
    check_env,
    exec_argv,
    parse_check_command,
    verification_env,
    verification_guidance,
)

HERE = os.path.dirname(os.path.abspath(__file__))
CODER_DB = os.path.join(HERE, ".coder_threads", "coder.db")

DEFAULT_TIMEOUT = 1800
MUTATING_TOOLS = frozenset({"write_file", "edit_file", "delete"})
DEFAULT_VERIFY_ROUNDS = 2
REPAIR_TOOL_LIMIT = 20
NEXT_OFFSET_RE = re.compile(r"\bnext offset (\d+)")
REPAIR_REPEAT_LIMIT = 3
REPAIR_MODEL_LIMIT = 12
INITIAL_MODEL_LIMIT = 20
INITIAL_TOOL_LIMIT = 32
INSPECTION_TOOLS = frozenset({"read_file", "grep", "ls", "glob"})
EXPLORE_NUDGE = 12
EXPLORE_LIMIT = 20
DEFAULT_FALLBACK_MODEL = "nebius/zai-org/GLM-5.3-Flash"
# Middleware adds graph steps beyond the model and tool calls themselves.
RECURSION_LIMIT = int(
    os.environ.get(
        "CODER_RECURSION_LIMIT", str(4 * (REPAIR_MODEL_LIMIT + REPAIR_TOOL_LIMIT))
    )
)

DEFAULT_TOOL_OUTPUT_LIMIT = 4000
READ_FILE_OUTPUT_LIMIT = 8000


def _truncate_tool_output(text: str, limit: int = DEFAULT_TOOL_OUTPUT_LIMIT) -> str:
    """Deterministically bound tool output length with head/tail preservation.

    Preserves the first limit // 2 and last limit // 2 characters with an
    explicit omitted-character marker so command headers and trailing
    tracebacks/assertions remain visible.
    """
    if not isinstance(text, str) or len(text) <= limit:
        return text
    half = limit // 2
    head = text[:half]
    tail = text[-half:] if half > 0 else ""
    omitted = len(text) - len(head) - len(tail)
    return f"{head}\n... [{omitted} chars omitted] ...\n{tail}"


class ReadCache:
    """Run-scoped cache of file contents by SHA-256 digest.

    Avoids re-injecting duplicate, unchanged file contents across turns
    within a single coder run.
    """

    def __init__(self):
        self._cache: dict[str, str] = {}

    def get(self, path: str) -> str | None:
        return self._cache.get(path)

    def check_and_update(self, path: str, content: str | bytes) -> tuple[bool, str]:
        """Check if content matches cached digest for path.

        Returns (is_duplicate, sha256_digest). If duplicate, cache is unchanged.
        If new or changed, the cache is updated with the new digest.
        """
        data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        digest = hashlib.sha256(data).hexdigest()
        if self._cache.get(path) == digest:
            return True, digest
        self._cache[path] = digest
        return False, digest

    def invalidate(self, path: str) -> None:
        """Invalidate cached digest for path when modified or deleted."""
        self._cache.pop(path, None)

    def clear(self) -> None:
        self._cache.clear()


# Dithering guard (#1160 gap 3, #1176): this many consecutive
# identical unproductive run_check calls means the agent is spinning,
# not progressing — the tool tells it to stop, and the verify loop
# refuses further re-prompts. Unproductive is failing (nonzero exit)
# or an empty success (exit 0 with no output: the command is not
# seeing what the agent expects). A success with output still resets.
DITHER_THRESHOLD = 5

DITHER_NOTICE = (
    "DITHERING: this exact command has failed {streak} times in a "
    "row. Stop retrying it unchanged — fix the code, or run a "
    "different check. Further identical retries will keep failing."
)

DITHER_EMPTY_NOTICE = (
    "DITHERING: this exact command has returned empty output "
    "{streak} times in a row. Stop retrying it unchanged — it is "
    "not seeing what you expect (wrong path, ignored directory?). "
    "Fix the invocation, or run a different check."
)

DITHER_UNCHANGED_NOTICE = (
    "DITHERING: this exact command has returned identical output "
    "{streak} times in a row. Stop re-inspecting: the check is "
    "not seeing change. Make the edit it is waiting for, verify "
    "it landed where the check looks, or run a different check."
)

# Re-prompt sent when a coder turn finishes with no passing run_check
# in the trace. Same thread, so the agent sees its own prior work.
VERIFY_NUDGE = (
    "No passing run_check was recorded for your edits. Run the "
    "checks for your edits with run_check and fix any failures; "
    "do not finish without a passing run. Invoke actual function tools; "
    "printing JSON commands does not execute them."
)

CODER_RULES = (
    """You are implementing the task below in the checkout at {worktree}. Rules:
- File paths are repo-relative like frontend/src/views/NowView.tsx; never use absolute paths or a leading /.
- Start filesystem discovery with ls(path="."). The worktree above is your sole filesystem root; never list the parent checkout even if task context names it.
- Only change files the task needs; when a scope list is given, stay inside it.
- Read before editing; keep diffs minimal and match surrounding style.
- Invoke tools through the function-calling interface. Never print JSON commands or pretend a prose command executed.
- Verify with run_check (the touched area's tests/lint) before finishing.
- Do NOT commit, push, or open PRs; leave changes in the working tree.
- End with a short summary: files changed + verification run, then a final DONE #<issue> or BLOCKED #<issue> line."""
    + "\n\n"
    + verification_guidance()
)

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
    r"|[A-Za-z0-9_-]+\.[A-Za-z][A-Za-z0-9]{0,4})\b"
)
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
PROSE_ABBREVS = frozenset({"e.g", "i.e"})

PLAN_PROMPT = (
    """Before writing any code, produce an implementation plan for the task below. Call the propose_plan tool exactly once with your plan, then stop. Do not call any other tool. Do not write or edit any file.

Plan shape — numbered steps; each step names its files and its checks:
PLAN:
1. <step> [files: <paths>] [checks: <commands>]
2. ...

Task:
{brief}"""
    + "\n\n"
    + verification_guidance()
)

# Replan/runbook skill for the plan + validated_replanner loop. The
# registry tolerates a missing skills dir (worktrees), yielding PLAN_PROMPT
# unchanged.
try:
    import skills_registry as _skills_registry

    PLAN_PROMPT = _skills_registry.SkillsRegistry().inject_skills_for_node(
        "validated_replanner_node", PLAN_PROMPT
    )
except Exception:
    pass

PLAN_NUDGE = (
    "Call the propose_plan tool now with your numbered plan "
    "(steps + files + checks each). Do not write code."
)


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
    2+ distinct file-like tokens in brief+context trigger a plan, as
    does any investigation verb when files fall short (#1235).
    """
    mode = os.environ.get(PLAN_ENV, "").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    if len(files_named(brief, context)) >= 2:
        return True
    text = " ".join(t for t in (brief, context) if isinstance(t, str))
    return bool(
        re.search(
            r"\b(find|locate|search|grep|explore|investigate|trace|audit"
            r"|discover|what|where)\b",
            text,
            re.IGNORECASE,
        )
    )


def validate_plan(plan):
    """Validate a propose_plan payload. Returns (ok, text). Pure."""
    if not isinstance(plan, str) or not plan.strip():
        return False, "REJECTED: plan must be non-empty text"
    if len(plan) > PLAN_LIMIT:
        return False, (f"REJECTED: plan is too long (max {PLAN_LIMIT} chars)")
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
    parser.add_argument("--resume", default=None, choices=["approve", "reject"])
    parser.add_argument("brief", nargs="?")
    args = parser.parse_args(argv)
    if not args.brief and not args.resume:
        parser.error("BRIEF is required")
    return args


def build_system(worktree, context, repo=None):
    """Coder rules plus spawner context, static head first.

    The rules block is identical every turn; per-task context varies,
    so the stable head stays first and the dynamic context trails the
    separator. Any provider-side prefix cache then survives new tasks.
    """
    rules = CODER_RULES.format(worktree=worktree)
    if repo is not None:
        # Swap the default verification policy for the repo's profile
        # (identical text for unprofiled/unknown repos).
        rules = rules.replace(verification_guidance(), verification_guidance(repo))
    if context and context.strip():
        return rules + "\n\n---\n\n" + context.strip()
    return rules


def build_model(model_override=None):
    """LLM via model_router (Ollama no-think default). Venv-only."""
    from backend.shared import model_router  # noqa: E402

    spec = model_router.resolve_llm()
    model_override = model_override or os.environ.get("CODER_MODEL", "").strip()
    if model_override:
        spec = dict(spec, model=model_override)
    think = os.environ.get("CODER_THINK", "") == "1"
    return model_router.build_chat_model(spec=spec, think=think, stage="coder")


def build_fallback_model(model_name):
    from backend.shared import model_router

    spec = model_router.resolve_llm(prefer="twt")
    if spec["provider"] != "twt":
        raise RuntimeError("coder fallback requires TwT; local-only mode forbids it")
    spec = dict(spec, model=model_name)
    spec.pop("provider_models", None)
    spec.pop("provider_reasoning_effort", None)
    return model_router.build_chat_model(
        spec=spec, think=os.environ.get("CODER_THINK", "") == "1"
    )


def _default_plan_fn(brief, worktree_root, context):
    """Out-of-the-box planner: model proposes plan + files, parsed
    deterministically. Injectable seam is build_model (monkeypatch in
    tests); the graph signature stays (brief, root, context)."""
    model, spec = build_model()
    context = dict(context or {})
    skills = (context.get("skills") or "").strip()
    skills_block = "\n\n---\n\nSkills:\n" + skills if skills else ""
    prompt = (
        "Propose an implementation plan for this brief. "
        "Reply with a numbered step list, one step per line, each "
        "naming its files like [files: path1, path2]. "
        "Keep it to the brief; no questions."
        f"\n\n{verification_guidance(context.get('repo') or None)}"
        f"{skills_block}"
        f"\n\nBrief:\n{brief}\n\nErrors so far:\n{context.get('errors', [])}"
        f"\n\nRepair notes:\n{context.get('repair_notes', '')}"
    )
    reply = None
    error = ""
    started = time.time()
    recorder = None
    if spec.get("model"):
        from backend.shared import ledger
        import observe

        thread = context.get("thread_id") or f"coder-plan-{time.time_ns()}"
        recorder = observe.TraceRecorder(
            observe.default_trace_path("coder_plan", thread)
        )
        recorder.run_start("coder.py", thread, spec["model"])
        recorder.model_request(1)
    try:
        reply = model.invoke(prompt)
    except Exception as exc:
        error = exc
        raise
    finally:
        if recorder is not None:
            messages = [reply] if reply is not None else []
            usage = ledger._usage_total(messages)
            if error:
                usage = ledger.error_usage(error)
            known = bool(getattr(reply, "usage_metadata", None)) or bool(
                (getattr(reply, "response_metadata", None) or {}).get(
                    "prompt_eval_count"
                )
            )
            recorder.model_response(usage)
            recorder.run_end(ok=reply is not None)
            recorder.close()
            ledger.record(
                "coder.py",
                spec["model"],
                thread,
                messages,
                started,
                usage=usage,
                provider=spec.get("provider"),
                extra={
                    "stage": "code",
                    "phase": "plan",
                    "llm_call": usage.get("not_dispatched") is not True,
                    "usage_missing": not known,
                    "trace": recorder.path,
                    **({"error": type(error).__name__} if error else {}),
                },
            )
    text = _model_text(reply)
    return {
        "plan": text.strip(),
        "allowed_files": files_named(text),
        "verification_commands": [],
        "model_spec": spec,
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

        injected = _skills_registry.SkillsRegistry().inject_skills_for_node(
            "validated_replanner_node", PLAN_PROMPT
        )
        return injected if injected != PLAN_PROMPT else ""
    except Exception:
        return ""


def build_check_tool(worktree, written_files=None, repo=None):
    """run_check tool: allowlisted verification commands in worktree."""
    from langchain_core.tools import tool  # noqa: E402

    allowed = allowed_commands_text(repo)
    # Per-tool dither streak: consecutive identical failing,
    # empty-success, or unchanged-output (#1374) commands. Any
    # different command or a success with new output resets it, and
    # so does an actual edit observed through written_files.
    spin = {"last": None, "streak": 0, "last_out": None, "edits": 0}

    @tool
    def run_check(command: str) -> str:
        """Run a verification command (tests/lint/git-inspect) in the worktree.

        Args:
            command: Full command line; must start with one of:
                {allowed}.
        """
        try:
            argv = parse_check_command(command, repo)
        except ValueError as exc:
            return f"REJECTED: bad command line: {exc}"
        import subprocess  # noqa: E402

        try:
            with verification_env(worktree) as env:
                proc = subprocess.run(
                    exec_argv(argv),
                    cwd=worktree,
                    capture_output=True,
                    text=True,
                    timeout=CHECK_TIMEOUT,
                    env=env,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            return f"TIMEOUT after {CHECK_TIMEOUT}s: {command[:200]}"
        except OSError as exc:
            return f"exec failed: {exc}"
        out = _truncate_tool_output(
            (proc.stdout or "") + (proc.stderr or ""), limit=CHECK_OUTPUT_LIMIT
        )
        result = f"exit={proc.returncode}\n{out}"
        empty_success = proc.returncode == 0 and not out.strip()
        if written_files is None:
            edits = 0
        else:
            edits = getattr(written_files, "mutations", len(written_files))
        if edits != spin["edits"]:
            spin["edits"] = edits
            spin["streak"] = 0
            spin["last_out"] = None
        unchanged_success = (
            proc.returncode == 0
            and not empty_success
            and command == spin["last"]
            and spin["last_out"] is not None
            and out == spin["last_out"]
        )
        unproductive = proc.returncode != 0 or empty_success or unchanged_success
        if command == spin["last"] and unproductive:
            spin["streak"] += 1
        else:
            spin["streak"] = 0 if not unproductive else 1
        spin["last"] = command
        if proc.returncode == 0:
            spin["last_out"] = out
        if spin["streak"] >= DITHER_THRESHOLD:
            if empty_success:
                notice = DITHER_EMPTY_NOTICE
            elif unchanged_success:
                notice = DITHER_UNCHANGED_NOTICE
            else:
                notice = DITHER_NOTICE
            result += "\n" + notice.format(streak=spin["streak"])
        return result

    run_check.description = run_check.description.format(allowed=allowed)
    return run_check


def worktree_file_key(worktree, path):
    root = os.path.abspath(worktree)
    if (
        os.path.isabs(path)
        and os.path.commonpath((root, os.path.abspath(path))) == root
    ):
        return os.path.relpath(path, root)
    return path.lstrip("/")


def build_filesystem_backend(worktree):
    from deepagents.backends import FilesystemBackend

    class CoderFilesystemBackend(FilesystemBackend):
        def _resolve_path(self, key):
            try:
                key = worktree_file_key(worktree, key)
                assert_inside(worktree, key)
            except ValueError as exc:
                raise PermissionError(
                    "outside_worktree_root: use worktree-relative paths "
                    f"under {self.cwd}"
                ) from exc
            return super()._resolve_path(key)

    return CoderFilesystemBackend(root_dir=worktree, virtual_mode=True)


class WriteTracker(set):
    """written_files set with a mutation counter: re-edits to an
    already-tracked file still count, so the run_check dither guard
    resets after every actual edit, not just new files (#1374)."""

    def __init__(self):
        super().__init__()
        self.mutations = 0


def worktree_file_middleware(worktree, written_files, read_cache=None):
    from langchain.agents.middleware import wrap_tool_call
    from langchain_core.messages import ToolMessage

    if read_cache is None:
        read_cache = ReadCache()
    unchanged_reads = {}
    cache_lock = threading.Lock()

    @wrap_tool_call
    def guard_file_call(request, handler):
        call = request.tool_call
        if call["name"] in {
            "ls",
            "read_file",
            "write_file",
            "edit_file",
            "delete",
            "glob",
            "grep",
        }:
            args = dict(call["args"])
            for key in ("path", "file_path"):
                path = call["args"].get(key)
                if path is None:
                    continue
                try:
                    relative = worktree_file_key(worktree, path)
                    target = assert_inside(worktree, relative)
                    if (
                        os.path.isabs(path)
                        and path != "/"
                        and relative == path.lstrip("/")
                        and not os.path.exists(target)
                    ):
                        raise ValueError("absolute host path")
                except ValueError:
                    return ToolMessage(
                        content="outside_worktree_root: use worktree-relative paths "
                        + f"under {worktree}",
                        tool_call_id=call["id"],
                        name=call["name"],
                        status="error",
                    )
                args[key] = relative
            # files_with_matches on a single file only echoes its path back.
            if (
                call["name"] == "grep"
                and "output_mode" not in args
                and os.path.isfile(os.path.join(worktree, args.get("path") or ""))
            ):
                args["output_mode"] = "content"
            call = {**call, "args": args}
            request = request.override(tool_call=call)
        result = handler(request)

        # Invalidate read cache on file mutations
        if (
            call["name"] in {"write_file", "edit_file", "delete"}
            and getattr(result, "status", None) == "success"
        ):
            target_file = call["args"].get("file_path") or call["args"].get("path")
            if target_file:
                rel = worktree_file_key(worktree, target_file)
                with cache_lock:
                    read_cache.invalidate(rel)
                    unchanged_reads.pop(rel, None)
            if written_files is not None and target_file:
                written_files.add(call["args"].get("file_path", target_file))
                try:
                    written_files.mutations += 1
                except AttributeError:
                    pass

        # Check ReadCache on read_file
        if (
            call["name"] == "read_file"
            and getattr(result, "status", None) == "success"
            and isinstance(getattr(result, "content", None), str)
        ):
            target_file = call["args"].get("file_path") or call["args"].get("path")
            if target_file:
                rel = worktree_file_key(worktree, target_file)
                with cache_lock:
                    is_dup, digest = read_cache.check_and_update(rel, result.content)
                    if is_dup:
                        if unchanged_reads.get(rel) == digest:
                            raise CoderBudgetExceeded(
                                f"BLOCKED: repeated unchanged read of {rel} "
                                "after the already-in-context warning"
                            )
                        unchanged_reads[rel] = digest
                    else:
                        unchanged_reads.pop(rel, None)
                if is_dup:
                    sentinel = f"[UNCHANGED: {rel} (sha256:{digest}) already in context. Do not re-read.]"
                    more = NEXT_OFFSET_RE.search(result.content.split("\n", 1)[0])
                    if more:
                        sentinel += (
                            f" That read stopped early: call read_file with "
                            f"offset={more.group(1)} for the rest of the file."
                        )
                    return ToolMessage(
                        content=sentinel,
                        tool_call_id=call["id"],
                        name=call["name"],
                        status="success",
                    )

        # Enforce deterministic head/tail output truncation across tools
        if isinstance(getattr(result, "content", None), str):
            limit = (
                READ_FILE_OUTPUT_LIMIT
                if call["name"] == "read_file"
                else DEFAULT_TOOL_OUTPUT_LIMIT
            )
            truncated = _truncate_tool_output(result.content, limit=limit)
            if truncated != result.content:
                return ToolMessage(
                    content=truncated,
                    tool_call_id=call["id"],
                    name=call["name"],
                    status=getattr(result, "status", "success"),
                )

        return result

    return guard_file_call


def implementation_tool_middleware():
    from langchain.agents.middleware import AgentMiddleware

    def grounded_request(request):
        if request.tools and not any(
            message.type == "tool" and message.status == "success"
            for message in request.messages
        ):
            return request.override(tool_choice="required")
        return request

    class GroundImplementation(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            return handler(grounded_request(request))

        async def awrap_model_call(self, request, handler):
            return await handler(grounded_request(request))

    return GroundImplementation()


def is_call_cap(error):
    """True for langchain's ModelCallLimitExceededError, without importing it."""
    return any(c.__name__ == "ModelCallLimitExceededError" for c in type(error).__mro__)


class CoderBudgetExceeded(RuntimeError):
    pass


class RepairBudgetExceeded(CoderBudgetExceeded):
    pass


def issuing_message(request):
    """The newest model response holding request's tool call, or None."""
    state = getattr(request, "state", None)
    messages = state.get("messages", []) if isinstance(state, dict) else []
    for message in reversed(messages):
        calls = getattr(message, "tool_calls", None) or ()
        if any(c.get("id") == request.tool_call["id"] for c in calls):
            return message
    return None


def coder_tool_middleware(*, repair=False, planning=False):
    from langchain.agents.middleware import wrap_tool_call
    from langchain_core.messages import ToolMessage

    seen = {}
    earlier = {}
    calls = 0
    explored = 0
    nudged = None
    bound_exploring = not (repair or planning)
    lock = threading.Lock()
    limit = REPAIR_TOOL_LIMIT if repair else INITIAL_TOOL_LIMIT
    label = "Repair" if repair else "Initial implementation"
    error = RepairBudgetExceeded if repair else CoderBudgetExceeded

    @wrap_tool_call
    def bound_calls(request, handler):
        nonlocal calls, explored, nudged
        call = request.tool_call
        key = (call["name"], json.dumps(call["args"], sort_keys=True))
        with lock:
            calls += 1
            if calls > limit:
                raise error(f"BLOCKED: {label} tool budget exhausted ({limit} calls)")
            if bound_exploring and call["name"] in INSPECTION_TOOLS:
                # Calls batched with the nudged one get the nudge too, so the
                # model sees it before the hard stop.
                message = issuing_message(request)
                if nudged is None or message is not nudged:
                    explored += 1
                    if explored >= EXPLORE_LIMIT:
                        raise error(
                            f"BLOCKED: {label} made {explored} inspection calls "
                            "without editing"
                        )
                if explored == EXPLORE_NUDGE:
                    nudged = nudged or message
                    return ToolMessage(
                        content=(
                            f"{explored} inspection calls without an edit. You have "
                            "enough context: make the planned edit now with "
                            "edit_file or write_file, or explain the blocker and "
                            "end with the BLOCKED #<issue> line."
                        ),
                        tool_call_id=call["id"],
                        status="error",
                    )
            if call["name"] != "run_check" and (repair or call["name"] != "read_file"):
                seen[key] = seen.get(key, 0) + 1
                if seen[key] >= REPAIR_REPEAT_LIMIT:
                    raise error(
                        f"BLOCKED: {label} repeated unchanged tool call: {key[0]} {key[1]}"
                    )
                if seen[key] > 1:
                    content = (
                        "Already called with these args without a successful edit. "
                        "Use the earlier result, fix the code, or finish with the blocker."
                    )
                    if key in earlier:
                        content += "\nEarlier result:\n" + earlier[key]
                    return ToolMessage(
                        content=content,
                        tool_call_id=call["id"],
                        status="error",
                    )
        result = handler(request)
        if isinstance(getattr(result, "content", None), str):
            with lock:
                earlier[key] = result.content
        if call["name"] in MUTATING_TOOLS and result.status == "success":
            with lock:
                seen.clear()
                explored = 0
                nudged = None
        return result

    return bound_calls


def build_agent(
    worktree,
    system,
    model=None,
    recorder=None,
    checkpointer=None,
    tools=None,
    interrupt_on=None,
    permissions=None,
    spec=None,
    written_files=None,
    repair=False,
    repo=None,
    planning=False,
    thread_floor=0,
    call_cap=None,
):
    """create_deep_agent coder: disk backend rooted at worktree. Venv-only.

    thread_floor is the model-call count already checkpointed on this
    thread (a resumed turn); the turn gets the usual limit on top of it,
    clipped to call_cap when the parent budget has less left.

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
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain.agents.middleware import ModelCallLimitMiddleware

    from backend.shared import model_router  # noqa: E402

    if model is None:
        model, _ = build_model()
    if spec is not None:
        model_router.disable_general_purpose(spec)

    backend = build_filesystem_backend(worktree)
    middleware = [
        FilesystemMiddleware(
            backend=backend,
            tools=[
                "ls",
                "read_file",
                "write_file",
                "edit_file",
                "delete",
                "glob",
                "grep",
            ],
        ),
        worktree_file_middleware(worktree, written_files),
    ]
    if written_files is not None:
        middleware.append(implementation_tool_middleware())
    middleware.insert(0, coder_tool_middleware(repair=repair, planning=planning))
    if not planning:
        middleware.insert(
            0,
            ModelCallLimitMiddleware(
                thread_limit=thread_floor
                + min(
                    REPAIR_MODEL_LIMIT if repair else INITIAL_MODEL_LIMIT,
                    INITIAL_MODEL_LIMIT if call_cap is None else call_cap,
                ),
                exit_behavior="error",
            ),
        )
    if recorder is not None:
        import observe  # noqa: E402

        middleware = observe.trace_middleware(recorder) + middleware
    if tools is None:
        default_tools = [build_check_tool(worktree, written_files, repo)]
    else:
        default_tools = list(tools)
    return create_deep_agent(
        model=model,
        tools=default_tools,
        system_prompt=system,
        backend=backend,
        interrupt_on=interrupt_on,
        permissions=permissions,
        checkpointer=checkpointer,
        middleware=middleware,
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


def build_plan_agent(
    worktree, system, model=None, recorder=None, checkpointer=None, spec=None
):
    """Planning agent: propose_plan only, writes denied, pauses on it.

    Same model/backend/threading as the implement agent; the write
    deny (FilesystemPermission, like deep_chat's harness gate) is
    what guarantees zero file writes before approval. spec passes
    through to build_agent for the general-purpose disable. Venv-only.
    """
    from deepagents.middleware.filesystem import (  # noqa: E402
        FilesystemPermission,
    )

    return build_agent(
        worktree,
        system,
        model=model,
        recorder=recorder,
        checkpointer=checkpointer,
        tools=[build_plan_tool()],
        interrupt_on=dict(CODER_INTERRUPT_ON),
        permissions=[
            FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")
        ],
        spec=spec,
        planning=True,
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
        return max(0, int(os.environ.get("CODER_VERIFY_ROUNDS", DEFAULT_VERIFY_ROUNDS)))
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
    verified = False
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
                if event.get("name") in MUTATING_TOOLS:
                    verified = False
                    continue
                if event.get("name") != "run_check":
                    continue
                preview = event.get("result_preview", "")
                if isinstance(preview, str) and (
                    preview == "exit=0" or preview.startswith("exit=0\n")
                ):
                    verified = True
    except (OSError, TypeError):
        return False
    return verified


def trace_verification_evidence(trace_path, repo=None):
    evidence = []
    try:
        with open(trace_path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict) or event.get("type") != "tool_call":
                        continue
                    if event.get("name") in MUTATING_TOOLS:
                        for check in evidence:
                            check["stale"] = True
                        continue
                    if event.get("name") != "run_check":
                        continue
                    command = json.loads(event.get("args_preview", "{}"))["command"]
                    if not isinstance(command, str):
                        continue
                    parse_check_command(command, repo)
                except (ValueError, TypeError, KeyError):
                    continue
                output = event.get("check_output", event.get("result_preview", ""))
                if not isinstance(output, str):
                    continue
                evidence.append(
                    {
                        "command": command,
                        "passed": event.get("ok") is True
                        and (output == "exit=0" or output.startswith("exit=0\n")),
                        "output": output[:CHECK_OUTPUT_LIMIT],
                        **(
                            {"output_truncated": True}
                            if event.get("check_output_truncated") is True
                            else {}
                        ),
                    }
                )
    except (OSError, TypeError):
        return []
    return evidence


def trace_check_history(trace_path):
    """[(command, exit_code, output_empty, output_digest)] per run_check.

    Entries are in order, and only strictly consecutive tool_call
    events count toward a streak: any other tool call between two
    checks (an edit, a read) means the agent changed approach, not
    dithered. output_digest hashes the recorded result preview so
    unchanged repeated output is detectable; previews are bounded,
    so outputs differing only past the preview limit read as
    unchanged (conservative). Never raises: unreadable traces read
    as empty (no dither).
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
                text = preview if isinstance(preview, str) else ""
                match = re.match(r"exit=(\d+)", text)
                code = int(match.group(1)) if match else -1
                empty = code == 0 and (
                    text == "exit=0"
                    or (
                        text.startswith("exit=0\n")
                        and not text[len("exit=0\n") :].strip()
                    )
                )
                digest = hashlib.sha1(text.encode("utf-8")).hexdigest()
                history.append((event.get("args_preview", ""), code, empty, digest))
    except (OSError, TypeError):
        return []
    return history


def _entry_unproductive(entry, previous=None):
    """True when a history entry shows no useful progress. Pure.

    Failing entries (nonzero exit) are always unproductive. Successful
    entries count when the trace records empty output, or when the
    output is identical to the previous entry for the same command:
    a repeated read-only inspection with no intervening progress
    (#1374). Legacy 2-tuples carry no output info, so a recorded
    pass still breaks the streak for them (the pre-#1176 contract);
    3-tuples carry no digest, so the unchanged-output rule applies
    only when both entries are 4-tuples. Never raises.
    """
    try:
        if len(entry) < 3:
            return entry[1] != 0
        if entry[1] != 0 or bool(entry[2]):
            return True
        return (
            previous is not None
            and len(entry) > 3
            and len(previous) > 3
            and entry[0] == previous[0]
            and entry[3] == previous[3]
        )
    except (TypeError, ValueError, IndexError):
        return False


def find_dithered_command(history, threshold=DITHER_THRESHOLD):
    """Dithering command in a check history, or None. Pure.

    Returns the command when the last `threshold` entries are the
    same command, all unproductive: failing, succeeding with empty
    output, or succeeding with output identical to the previous
    entry. The tail's first entry compares against the history entry
    right before it, so a repeated unchanged inspection is detected
    once its first observation falls out of the tail. A success
    with new output, a different command, or too few entries read
    as no dither. Never raises.
    """
    try:
        entries = list(history or [])
        tail = entries[-threshold:]
        if len(tail) < threshold:
            return None
        command = tail[0][0]
        start = len(entries) - threshold
        for offset, entry in enumerate(tail):
            if entry[0] != command:
                return None
            index = start + offset
            previous = entries[index - 1] if index > 0 else None
            if not _entry_unproductive(entry, previous):
                return None
        return command
    except (TypeError, ValueError, IndexError):
        return None


def trace_has_dither(trace_path, threshold=DITHER_THRESHOLD):
    """True when a trace ends in a dithering check streak. Never raises."""
    try:
        return (
            find_dithered_command(trace_check_history(trace_path), threshold)
            is not None
        )
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

    context = contextvars.copy_context()
    worker = threading.Thread(target=lambda: context.run(_target), daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"timed out after {timeout}s")
    if "error" in box:
        raise box["error"]
    return box.get("result")


def run_with_verify(
    initial_invoke,
    reinvoke,
    has_passing_check,
    max_rounds,
    dithered=None,
    check_stagnant=None,
):
    """One invoke plus bounded verify re-invokes.

    initial_invoke() runs first; while has_passing_check() is false,
    reinvoke() runs up to max_rounds times. Returns
    (result, verify_rounds, verified). When dithered() reports a
    repeated-identical-unproductive streak (failing or empty-success
    repeats), re-invokes stop early (#1160 gap 3: fail fast instead
    of burning verify rounds on a spinning agent).
    When check_stagnant() reports no progress between rounds,
    re-invokes stop early (Issue #1393 circuit breaker).
    """
    result = initial_invoke()
    rounds = 0
    verified = has_passing_check()
    while not verified and rounds < max_rounds:
        if dithered is not None and dithered():
            break
        if check_stagnant is not None and check_stagnant():
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
            text = content if isinstance(content, str) else json.dumps(content)[:2000]
            return text.strip()
    except Exception:  # noqa: BLE001 - summary best effort
        pass
    return ""


def mark_unverified(summary):
    """Prefix a summary that exhausted the verify bound without a pass."""
    if summary:
        return "[unverified]\n" + summary
    return "[unverified]"


def tier_model(model_tier):
    """Model override for a pipeline model_tier ('fast', 'strong', None).

    'strong' requires CODER_STRONG_MODEL and fails loudly without it;
    'fast' and None use CODER_MODEL (None means the router default).
    """
    if model_tier not in (None, "fast", "strong"):
        raise ValueError(f"unknown model_tier {model_tier!r}")
    if model_tier == "strong":
        strong = (os.environ.get("CODER_STRONG_MODEL") or "").strip()
        if not strong:
            raise ValueError("model_tier 'strong' requires CODER_STRONG_MODEL")
        return strong
    return os.environ.get("CODER_MODEL") or None


CONTINUE_PROMPT = "Continue; your changes are preserved."


def run_coder(
    worktree,
    brief,
    system,
    thread,
    timeout,
    *,
    repair_files=(),
    repair=False,
    model_tier=None,
    repo=None,
    continuations=0,
    call_budget=None,
    meter=None,
):
    """Code normally; on a model-call cap, resume, then allow one TwT fallback.

    A cap hit resumes on the SAME checkpointed thread with a fresh
    per-turn budget, at most ``continuations`` times and never past
    ``call_budget`` total model calls (None: unbounded by the caller).
    The one-shot TwT fallback on initial default-route exhaustion runs
    after the continuations. ``meter`` (a dict) accumulates "calls" and
    "continuations" across every turn, including failed ones. Cap
    exhaustion re-raises ModelCallLimitExceededError.
    """
    from langchain.agents.middleware.model_call_limit import ModelCallLimitExceededError

    from backend.shared import model_router

    meter = meter if meter is not None else {}
    meter.setdefault("calls", 0)
    meter.setdefault("continuations", 0)
    kwargs = dict(
        repair_files=repair_files,
        repair=repair,
        model_tier=model_tier,
        repo=repo,
        meter=meter,
    )

    def left():
        return None if call_budget is None else call_budget - meter["calls"]

    deadline = time.monotonic() + timeout
    turn_brief, floor, prior_files = brief, 0, ()
    while True:
        try:
            return _run_coder_attempt(
                worktree,
                turn_brief,
                system,
                thread,
                timeout,
                deadline=deadline,
                thread_floor=floor,
                call_cap=left(),
                prior_files=prior_files,
                **kwargs,
            )
        except ModelCallLimitExceededError as error:
            capped = error
            if meter["continuations"] >= continuations or (
                call_budget is not None and left() <= 0
            ):
                break
            meter["continuations"] += 1
            turn_brief, floor = CONTINUE_PROMPT, error.thread_count
            prior_files = error.coder_written_files
    error = capped
    spec = getattr(error, "coder_model_spec", {})
    fallback = os.environ.get("CODER_FALLBACK_MODEL", DEFAULT_FALLBACK_MODEL).strip()
    if (
        repair
        or (call_budget is not None and left() <= 0)
        or model_router.local_only()
        or spec.get("provider") != "twt"
        or spec.get("model") not in ("default", "route:default")
        or error.thread_limit - floor != INITIAL_MODEL_LIMIT
        or error.thread_count - floor < INITIAL_MODEL_LIMIT
        or not fallback
    ):
        raise error
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError(f"coder timed out after {timeout}s before fallback")
    return _run_coder_attempt(
        worktree,
        brief + "\n\nThe default-route attempt exhausted its model budget. "
        "Its worktree changes are preserved. Inspect them, complete the task, "
        "and verify before finishing.",
        system,
        thread + "-fallback",
        remaining,
        deadline=deadline,
        fallback_model=fallback,
        prior_files=error.coder_written_files,
        call_cap=left(),
        **kwargs,
    )


def _run_coder_attempt(
    worktree,
    brief,
    system,
    thread,
    timeout,
    *,
    repair_files=(),
    repair=False,
    model_tier=None,
    repo=None,
    fallback_model=None,
    prior_files=(),
    deadline=None,
    thread_floor=0,
    call_cap=None,
    meter=None,
):
    """Invoke the coder with a verify gate.

    Returns (summary, usage, calls, started, label, trace, verified,
    verify_rounds). Unless CODER_VERIFY_ROUNDS=0, a turn with no
    passing run_check in the trace re-invokes (same thread) with the
    verify nudge, up to the round bound; an exhausted bound prefixes
    the summary with [unverified]. Missing terminal contracts, blocked runs,
    and implementations with no successful writes fail. Venv-only.
    """
    import observe  # noqa: E402
    from langchain_core.messages import HumanMessage  # noqa: E402

    import deep_chat  # noqa: E402
    from backend.shared import ledger  # noqa: E402

    started = time.time()
    deadline = time.monotonic() + timeout if deadline is None else deadline
    override = tier_model(model_tier)
    model, spec = (
        build_fallback_model(fallback_model)
        if fallback_model
        else build_model(override)
    )
    recorder = observe.TraceRecorder(observe.default_trace_path("coder", thread))
    if meter is not None:
        meter["trace"] = recorder.path
    label = f"{spec['provider']}:{spec['model']}"
    marker = " (tier=strong)" if model_tier == "strong" else ""
    print(f"coder via {label}{marker}", file=sys.stderr, flush=True)
    recorder.run_start("coder.py", thread, label + marker)
    invocation_started = False
    written_files = WriteTracker()
    written_files.update(prior_files)

    def fail(error, reply=""):
        if not invocation_started:
            error.llm_not_dispatched = True
        error_usage = ledger.error_usage(error)
        if "reported_cost_usd" in error_usage:
            recorder.model_response(error_usage)
        recorder.run_end(False)
        recorder.close()
        usage, calls = recorder.totals()
        if meter is not None:
            meter["calls"] = meter.get("calls", 0) + recorder.model_calls
        ledger.record(
            "coder.py",
            spec["model"],
            thread,
            [
                {"role": "user", "content": brief},
                {"role": "assistant", "content": reply},
            ],
            started,
            provider=spec["provider"],
            usage=usage,
            calls=calls,
            extra={
                "error": f"{type(error).__name__}: {error}",
                "worktree": worktree,
                "trace": recorder.path,
                "verified": False,
                "llm_call": invocation_started
                or error_usage.get("not_dispatched") is not True,
            },
        )

    try:
        saver, _persistent, holder = deep_chat.open_checkpointer(CODER_DB)
        agent = build_agent(
            worktree,
            system,
            model=model,
            recorder=recorder,
            checkpointer=saver,
            spec=spec,
            written_files=written_files,
            repair=repair,
            repo=repo,
            thread_floor=thread_floor,
            call_cap=call_cap,
        )
        config = {"configurable": {"thread_id": thread}}

        def _invoke(msgs):
            nonlocal invocation_started
            invocation_started = True
            return agent.invoke(
                {"messages": msgs},
                config={**config, "recursion_limit": RECURSION_LIMIT},
            )

        def invoke_bounded(message):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out after {timeout}s")
            return run_bounded(
                lambda: _invoke([HumanMessage(content=message)]), remaining
            )

        last_diff = None

        def is_stagnant():
            nonlocal last_diff
            try:
                cur_diff = subprocess.run(
                    ["git", "-C", worktree, "diff", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout
            except Exception:
                return False
            if last_diff is not None and cur_diff == last_diff:
                return True
            last_diff = cur_diff
            return False

        max_rounds = verify_rounds()
        result, rounds, verified = run_with_verify(
            lambda: invoke_bounded(brief),
            lambda: invoke_bounded(VERIFY_NUDGE),
            lambda: trace_has_passing_check(recorder.path),
            max_rounds,
            dithered=lambda: trace_has_dither(recorder.path),
            check_stagnant=is_stagnant,
        )
        # Keep the checkpointer connection alive until the run returns.
        _ = holder
        reply = extract_reply(result)
        if verified and repair_files:
            written_files.update(existing_implementation_files(worktree, repair_files))
        reason = implementation_failure(reply, written_files, verified)
        if reason:
            recorder.event("coder_verdict", ok=False, reason=reason)
            raise RuntimeError(reason)
        if not verified and max_rounds > 0:
            reply = mark_unverified(reply)
        recorder.run_end(True)
        recorder.close()
        usage, calls = recorder.totals()
        if meter is not None:
            meter["calls"] = meter.get("calls", 0) + recorder.model_calls
        print(f"trace: {recorder.path}", file=sys.stderr)
        return reply, usage, calls, started, label, recorder.path, verified, rounds
    except BaseException as error:
        fail(error)
        error.coder_model_spec = {key: spec.get(key) for key in ("provider", "model")}
        error.coder_written_files = tuple(written_files)
        if isinstance(error, TimeoutError):
            raise RuntimeError(f"coder timed out after {timeout}s") from error
        raise


def existing_implementation_files(worktree, allowed_files):
    allowed = set()
    for path in allowed_files:
        key = os.path.normpath(worktree_file_key(worktree, path))
        assert_inside(worktree, key)
        allowed.add(key)
    changed = set()
    for args in (
        ["diff", "--name-only", "--no-renames", "-z", "HEAD", "--"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
    ):
        try:
            output = subprocess.run(
                ["git", "-C", worktree, *args],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        except subprocess.CalledProcessError as exc:
            raise RuntimeError("cannot inspect existing implementation diff") from exc
        changed.update(output.split("\0"))
    return changed & allowed


NO_WRITES = "no written files for implementation"


def implementation_failure(reply, written_files, verified=False):
    """Why the coder's reply fails its contract, or "" when it holds.

    A reply missing the terminal DONE/BLOCKED line still counts as done
    when the run wrote files and passed run_check; the pipeline's
    verification gate and reviewer stage still judge the diff.
    """
    lines = reply.strip().splitlines()
    contract = (
        re.fullmatch(r"(DONE|BLOCKED) #\d+", lines[-1].strip()) if lines else None
    )
    reasons = []
    if contract is None:
        if not (verified and written_files):
            reasons.append("missing terminal DONE #<issue> or BLOCKED #<issue> line")
    elif contract.group(1) == "BLOCKED":
        reasons.append("coder reported " + contract.group(0))
    if not written_files:
        reasons.append(NO_WRITES)
    return "; ".join(reasons)


def latest_plan(thread):
    """Newest plan_pending ledger row for thread, or None. Never raises."""
    from backend.shared import ledger  # noqa: E402

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
    return {
        "ts": best.get("ts"),
        "model": best.get("model") or "unknown",
        "brief": extra.get("brief") or "",
        "worktree": extra.get("worktree") or "",
        "system": extra.get("system") or "",
        "plan": extra.get("plan") or "",
        "trace": extra.get("trace") or "",
    }


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
        AIMessage,
        HumanMessage,
    )

    import deep_chat  # noqa: E402
    from backend.shared import ledger  # noqa: E402

    started = time.time()
    recorder = observe.TraceRecorder(observe.default_trace_path("coder_plan", thread))
    model, spec = build_model(os.environ.get("CODER_MODEL") or None)
    label = f"{spec['provider']}:{spec['model']}"
    print(f"coder plan via {label}", file=sys.stderr, flush=True)
    recorder.run_start("coder.py", thread, label)
    saver, _persistent, holder = deep_chat.open_checkpointer(CODER_DB)
    agent = build_plan_agent(
        worktree, system, model=model, recorder=recorder, checkpointer=saver, spec=spec
    )
    config = {"configurable": {"thread_id": thread}}

    def _invoke(msgs):
        return agent.invoke(
            {"messages": msgs}, config={**config, "recursion_limit": RECURSION_LIMIT}
        )

    try:
        run_bounded(
            lambda: _invoke([HumanMessage(content=PLAN_PROMPT.format(brief=brief))]),
            timeout,
        )
        pending = deep_chat._pending_interrupts(agent, config)
        if not pending:
            run_bounded(lambda: _invoke([HumanMessage(content=PLAN_NUDGE)]), timeout)
            pending = deep_chat._pending_interrupts(agent, config)
    except TimeoutError:
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py",
            label,
            thread,
            [HumanMessage(content=brief)],
            started,
            extra={
                "worktree": worktree,
                "coder_phase": "plan_failed",
                "trace": recorder.path,
                "error": f"plan timed out after {timeout}s",
            },
        )
        return cli.sorry(f"Sorry, the coder plan step timed out after {timeout}s.")
    except Exception as exc:  # noqa: BLE001 - sorry contract
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py",
            label,
            thread,
            [HumanMessage(content=brief)],
            started,
            extra={
                "worktree": worktree,
                "coder_phase": "plan_failed",
                "trace": recorder.path,
                "error": str(exc)[:200],
            },
        )
        return cli.sorry(f"Sorry, the coder plan step failed: {exc}")
    _ = holder
    if not pending:
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py",
            label,
            thread,
            [HumanMessage(content=brief)],
            started,
            extra={
                "worktree": worktree,
                "coder_phase": "plan_failed",
                "trace": recorder.path,
                "error": "plan step did not call propose_plan",
            },
        )
        return cli.sorry("Sorry, the coder plan step produced no plan for approval.")
    plan_text = ""
    requests = _plan_requests(pending)
    if requests and isinstance(requests[0], dict):
        plan_text = (requests[0].get("args") or {}).get("plan") or ""
    recorder.event(
        "plan", plan_preview=str(plan_text)[:2000], interrupt_id=pending[0].get("id")
    )
    recorder.run_end(True)
    recorder.close()
    usage, calls = recorder.totals()
    print(f"trace: {recorder.path}", file=sys.stderr)
    ledger.record(
        "coder.py",
        label,
        thread,
        [HumanMessage(content=brief), AIMessage(content=plan_text or "(empty plan)")],
        started,
        extra={
            "worktree": worktree,
            "brief": brief,
            "system": system,
            "plan": str(plan_text)[:PLAN_LIMIT],
            "coder_phase": "plan_pending",
            "trace": recorder.path,
        },
        usage=usage,
        calls=calls,
    )
    return deep_chat._emit_interrupt(thread, pending)


def _implement_and_finish(worktree, brief, system, thread, timeout, extra=None):
    """run_coder plus the ledger row + stdout summary. Returns exit code."""
    from backend.shared import ledger  # noqa: E402
    from langchain_core.messages import (  # noqa: E402
        AIMessage,
        HumanMessage,
    )

    label = "unknown"
    try:
        (summary, usage, calls, started, label, trace, verified, rounds) = run_coder(
            worktree, brief, system, thread, timeout
        )
    except Exception as exc:  # noqa: BLE001 - sorry contract
        ledger.record(
            "coder.py",
            label,
            thread,
            [{"role": "user", "content": brief}],
            time.time(),
            extra={"worktree": worktree, "error": str(exc)[:200], **(extra or {})},
        )
        return cli.sorry(f"Sorry, the coder failed: {exc}")
    ledger.record(
        "coder.py",
        label,
        thread,
        [HumanMessage(content=brief), AIMessage(content=summary)],
        started,
        extra={
            "worktree": worktree,
            "trace": trace,
            "verified": verified,
            "verify_rounds": rounds,
            **(extra or {}),
        },
        usage=usage,
        calls=calls,
    )
    print(summary or "(empty summary)")
    return 0


def _cmd_resume(
    worktree_arg, thread, verdict, brief_override=None, system_prompt_override=None
):
    """Resume a paused coder plan. Returns an exit code.

    reject: ledger plan_rejected, no agent touched, no edits,
    exit 0 (a human decision, like deep_chat's resume-reject).
    approve: stale plans (older than CODER_TIMEOUT) abort as
    plan_stale with Sorry exit 1; fresh plans resume the SAME
    thread into the normal implement + verify path.
    """
    import observe  # noqa: E402
    from langchain_core.messages import (  # noqa: E402
        AIMessage,
        HumanMessage,
    )

    import deep_chat  # noqa: E402
    from backend.shared import ledger  # noqa: E402

    if verdict not in ("approve", "reject"):
        return cli.usage_error(f"bad verdict {verdict!r}: want 'approve'/'reject'")
    started = time.time()
    plan = latest_plan(thread)
    if plan is None:
        return cli.sorry(f"Sorry, no pending plan for thread {thread!r}.")
    worktree = os.path.abspath(worktree_arg or plan["worktree"] or os.getcwd())
    if not os.path.isdir(worktree):
        return cli.sorry(f"Sorry, no worktree at {worktree}.")
    if verdict == "reject":
        recorder = observe.TraceRecorder(
            observe.default_trace_path("coder_resume", thread)
        )
        recorder.run_start("coder.py", thread, plan["model"])
        recorder.event("plan_decision", verdict="reject")
        recorder.run_end(True)
        recorder.close()
        ledger.record(
            "coder.py",
            plan["model"],
            thread,
            [
                HumanMessage(content=plan["brief"] or "(brief unavailable)"),
                AIMessage(content="plan rejected"),
            ],
            started,
            extra={
                "worktree": worktree,
                "coder_phase": "plan_rejected",
                "trace": recorder.path,
            },
        )
        print("plan rejected, no changes made")
        return 0
    timeout = _timeout()
    if plan_is_stale(plan["ts"], time.time(), timeout):
        ledger.record(
            "coder.py",
            plan["model"],
            thread,
            [HumanMessage(content=plan["brief"] or "(brief unavailable)")],
            started,
            extra={
                "worktree": worktree,
                "coder_phase": "plan_stale",
                "error": f"plan older than {timeout}s",
            },
        )
        return cli.sorry(
            f"Sorry, the plan for thread {thread!r} expired "
            f"(older than {timeout}s); re-dispatch the task."
        )
    brief = brief_override or plan["brief"]
    if not brief:
        return cli.sorry(
            f"Sorry, the plan for thread {thread!r} has no brief; re-dispatch the task."
        )
    if system_prompt_override and system_prompt_override.strip():
        system = build_system(worktree, system_prompt_override)
    elif plan["system"]:
        system = plan["system"]
    else:
        system = build_system(worktree, "")
    # Complete the paused planning turn on its own agent first, so
    # the thread carries plan→approve before implementation starts.
    recorder = observe.TraceRecorder(observe.default_trace_path("coder_resume", thread))
    model, spec = build_model(os.environ.get("CODER_MODEL") or None)
    label = f"{spec['provider']}:{spec['model']}"
    print(f"coder resume via {label}", file=sys.stderr, flush=True)
    recorder.run_start("coder.py", thread, label)
    saver, persistent, holder = deep_chat.open_checkpointer(CODER_DB)
    agent = build_plan_agent(
        worktree, system, model=model, recorder=recorder, checkpointer=saver, spec=spec
    )
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
        recorder.event(
            "plan_decision", verdict="approve", resume="ledger-plan-cross-process"
        )
        recorder.run_end(True)
        recorder.close()
        resume_usage, resume_calls = recorder.totals()
        ledger.record(
            "coder.py",
            label,
            thread,
            [
                HumanMessage(content=brief),
                AIMessage(content=plan["plan"] or "(plan approved)"),
            ],
            started,
            extra={
                "worktree": worktree,
                "coder_phase": "plan_approved",
                "trace": recorder.path,
                "ledger_resume": True,
            },
            usage=resume_usage,
            calls=resume_calls,
        )
        return _implement_and_finish(
            worktree,
            f"{brief}\n\nApproved plan:\n{plan['plan']}",
            system,
            thread,
            timeout,
            extra={
                "coder_phase": "implement",
                "plan_approved": True,
                "ledger_resume": True,
            },
        )
    if not pending:
        recorder.run_end(False)
        recorder.close()
        return cli.sorry(f"Sorry, no pending approval for thread {thread!r}.")
    from langgraph.types import Command  # noqa: E402

    count = len(_plan_requests(pending)) or 1

    def _invoke(payload):
        return agent.invoke(
            payload, config={**config, "recursion_limit": RECURSION_LIMIT}
        )

    try:
        run_bounded(
            lambda: _invoke(
                Command(
                    resume=deep_chat.resume_payload(
                        [deep_chat.decision_for_verdict("approve")] * count
                    )
                )
            ),
            timeout,
        )
    except TimeoutError:
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py",
            label,
            thread,
            [HumanMessage(content=brief)],
            started,
            extra={
                "worktree": worktree,
                "coder_phase": "resume_failed",
                "trace": recorder.path,
                "error": f"resume timed out after {timeout}s",
            },
        )
        return cli.sorry(f"Sorry, the coder resume timed out after {timeout}s.")
    except Exception as exc:  # noqa: BLE001 - sorry contract
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "coder.py",
            label,
            thread,
            [HumanMessage(content=brief)],
            started,
            extra={
                "worktree": worktree,
                "coder_phase": "resume_failed",
                "trace": recorder.path,
                "error": str(exc)[:200],
            },
        )
        return cli.sorry(f"Sorry, the coder resume failed: {exc}")
    _ = holder
    recorder.event("plan_decision", verdict="approve")
    recorder.run_end(True)
    recorder.close()
    resume_usage, resume_calls = recorder.totals()
    ledger.record(
        "coder.py",
        label,
        thread,
        [
            HumanMessage(content=brief),
            AIMessage(content=plan["plan"] or "(plan approved)"),
        ],
        started,
        extra={
            "worktree": worktree,
            "coder_phase": "plan_approved",
            "trace": recorder.path,
        },
        usage=resume_usage,
        calls=resume_calls,
    )
    return _implement_and_finish(
        worktree,
        f"{brief}\n\nApproved plan:\n{plan['plan']}",
        system,
        thread,
        timeout,
        extra={"coder_phase": "implement", "plan_approved": True},
    )


def main(argv=None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    if args.model:
        os.environ["CODER_MODEL"] = args.model
    if args.resume:
        if not args.thread:
            return cli.usage_error("--thread is required with --resume")
        return _cmd_resume(
            args.worktree,
            args.thread,
            args.resume,
            brief_override=args.brief,
            system_prompt_override=args.system_prompt or None,
        )
    worktree = os.path.abspath(args.worktree or os.getcwd())
    if not os.path.isdir(worktree):
        return cli.sorry(f"Sorry, no worktree at {worktree}.")
    thread = args.thread or f"coder-{os.getpid()}-{int(time.time())}"
    system = build_system(worktree, args.system_prompt)
    timeout = _timeout()
    print(
        f"thread: {thread} worktree: {worktree} timeout: {timeout}s",
        file=sys.stderr,
        flush=True,
    )
    if needs_plan(args.brief, args.system_prompt):
        print("brief clears the plan bar: planning first", file=sys.stderr, flush=True)
        return plan_and_pause(worktree, args.brief, system, thread, timeout)
    return _implement_and_finish(worktree, args.brief, system, thread, timeout)


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


def gate_spec_markdown(brief, allowed_files):
    """Minimal verify-scope spec from a brief + allowed file list."""
    lines = [
        "# Task",
        "",
        "## Problem Statement",
        brief.strip(),
        "",
        "## Goal",
        brief.strip(),
        "",
        "## Scope",
        "",
        "## Definition of Done",
        "done.",
        "",
        "## Scope & File Boundaries",
        "### Allowed Files",
    ]
    for path in allowed_files or []:
        lines.append(f"- `{path}`")
    lines += [
        "### Forbidden Files",
        "- (none declared)",
        "",
        "## Acceptance Criteria",
        "- [ ] Gates pass",
        "",
    ]
    return "\n".join(lines)


class InitCoderGraph:
    """Worktree-isolated implement loop. Inject fakes for tests.

    plan_fn(brief, worktree_root, context) -> {"plan": str,
        "allowed_files": [...], "verification_commands": [...]}
    implement_fn(brief, plan, worktree_root, errors) -> notes str
    td_runner(args, cwd) -> (returncode, stdout-json)
    cmd_runner(checkout, command) -> s5tools.run_repo_command-shaped str
    """

    def __init__(
        self,
        repo,
        issue_id,
        brief,
        plan_fn=None,
        implement_fn=None,
        td_runner=None,
        cmd_runner=None,
        max_fix_loops=3,
        max_replans=1,
        thread_id=None,
    ):
        from backend.shared import s5tools  # noqa: E402  (stdlib-only module)

        self.repo = repo
        self.issue_id = issue_id
        self.brief = brief
        self.plan_fn = plan_fn or _default_plan_fn
        self.implement_fn = implement_fn or _default_implement_fn
        self.td_runner = td_runner if td_runner is not None else self._default_td_runner
        self.cmd_runner = (
            cmd_runner
            if cmd_runner is not None
            else lambda root, command: s5tools.run_repo_command(
                root, command, s5tools.repo_for_checkout(self.repo)
            )
        )
        self.max_fix_loops = max_fix_loops
        self.max_replans = max_replans
        self.thread_id = thread_id or f"coder-{issue_id}-{int(time.time())}"
        self.last_model_spec = {}

    @staticmethod
    def _default_td_runner(args, cwd):
        from backend.shared import s5tools

        out = s5tools.td(["--json"] + list(args), cwd)
        return 0, out

    # -- deterministic pieces -------------------------------------------

    def provision_worktree(self):
        """td worktree create --issue; returns (root, error-or-None)."""
        try:
            code, out = self.td_runner(
                ["worktree", "create", "--issue", str(self.issue_id)], self.repo
            )
        except Exception as exc:
            return None, f"td worktree create failed: {exc}"
        try:
            payload = json.loads(out) if out else {}
        except ValueError:
            return None, f"td returned non-JSON: {out[:200]}"
        if code != 0 or not isinstance(payload, dict):
            return None, (f"td worktree create exited {code}: {out[:200]}")
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
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        )
        commands_out = []
        try:
            handle.write(spec_text)
            handle.close()
            try:
                code, out = self.td_runner(
                    [
                        "verify-scope",
                        "--spec-file",
                        handle.name,
                        "--worktree",
                        worktree_root,
                    ],
                    worktree_root,
                )
            except Exception as exc:
                return {
                    "passed": False,
                    "scope": {},
                    "audit": {},
                    "commands": [],
                    "errors": [f"verify-scope failed: {exc}"],
                }
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
                    ["audit-code", "--worktree", worktree_root], worktree_root
                )
            except Exception as exc:
                return {
                    "passed": False,
                    "scope": scope,
                    "audit": {},
                    "commands": [],
                    "errors": errors + [f"audit-code failed: {exc}"],
                }
            try:
                audit = json.loads(out) if out else {}
            except ValueError:
                audit = {}
            if code != 0 or not audit.get("passed", False):
                for item in audit.get("ast_violations", []):
                    errors.append(
                        f"audit: {item.get('file')}:{item.get('line')}: "
                        f"{item.get('rule')}: {item.get('message')}"
                    )
                for item in audit.get("slop_findings", []):
                    errors.append(
                        f"slop: {item.get('script')}: "
                        f"{str(item.get('summary', ''))[:200]}"
                    )
                if code != 0 and not errors:
                    errors.append(f"audit-code exited {code}")
            for command in verification_commands or []:
                result = self.cmd_runner(worktree_root, command)
                match = re.match(r"exit=(\d+)", result or "")
                exit_code = int(match.group(1)) if match else -1
                commands_out.append({"command": command, "exit": exit_code})
                if exit_code != 0:
                    errors.append(
                        f"command failed (exit={exit_code}): {command} {result[-300:]}"
                    )
        finally:
            try:
                os.unlink(handle.name)
            except OSError:
                pass
        return {
            "passed": not errors,
            "scope": scope,
            "audit": audit,
            "commands": commands_out,
            "errors": errors,
        }

    # -- graph ------------------------------------------------------------

    def build_graph(self):
        """Compile the coding loop (needs langgraph)."""
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.graph import END, START, StateGraph
        from langgraph.types import interrupt
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
            # Best-effort: link shared node_modules into the fresh worktree (#1235).
            from backend.shared import s5tools  # noqa: E402  (stdlib-only module)

            s5tools.link_worktree_node_modules(
                root, os.environ.get("DEEP_AGENT_MODSTORE", "")
            )
            return {
                "worktree_root": root,
                "fix_attempts": 0,
                "replans": 0,
                "plan_attempts": 0,
                "errors": [],
                "valid": False,
            }

        def plan_node(state):
            if state.get("plan") and not state.get("errors"):
                return {}
            if outer.plan_fn is None:
                return {"fatal": "plan_fn is required"}
            result = outer.plan_fn(
                outer.brief,
                state.get("worktree_root", ""),
                {"errors": state.get("errors", []), "thread_id": outer.thread_id},
            )
            outer.last_model_spec = dict(
                result.get("model_spec") or outer.last_model_spec
            )
            allowed = list(result.get("allowed_files", []))
            commands = list(result.get("verification_commands", []))
            return {
                "plan": result.get("plan", ""),
                "allowed_files": allowed,
                "verification_commands": commands,
                "approved": False,
            }

        def plan_review_node(state):
            if state.get("approved"):
                return {}
            if state.get("plan_attempts", 0) >= 3:
                return {"fatal": "plan rejected too many times"}
            reply = interrupt(
                {"plan": state.get("plan", ""), "issue_id": outer.issue_id}
            )
            decision = (reply or {}).get("decision", "approve")
            if decision == "approve":
                return {
                    "approved": True,
                    "plan_attempts": state.get("plan_attempts", 0) + 1,
                }
            notes = (reply or {}).get("notes", "")
            errors = list(state.get("errors", []))
            if notes:
                errors.append(f"plan revision requested: {notes}")
            return {
                "approved": False,
                "errors": errors,
                "plan_attempts": state.get("plan_attempts", 0) + 1,
            }

        def implement_node(state):
            if state.get("implementation_notes") and not state.get("errors"):
                return {}
            if outer.implement_fn is None:
                return {"fatal": "implement_fn is required"}
            notes = outer.implement_fn(
                outer.brief,
                state.get("plan", ""),
                state.get("worktree_root", ""),
                state.get("errors", []),
            )
            return {"implementation_notes": notes}

        def gate_node(state):
            gate = outer.run_gate(
                state.get("worktree_root", ""),
                state.get("allowed_files", []),
                state.get("verification_commands", []),
            )
            update = {"gate": gate, "errors": gate["errors"], "valid": gate["passed"]}
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
            context = {
                "errors": state.get("errors", []),
                "gate": state.get("gate", {}),
                "replan": True,
            }
            skills_text = _inject_replanner_skills()
            if skills_text:
                context["skills"] = skills_text
            result = outer.plan_fn(outer.brief, state.get("worktree_root", ""), context)
            outer.last_model_spec = dict(
                result.get("model_spec") or outer.last_model_spec
            )
            return {
                "plan": result.get("plan", ""),
                "allowed_files": list(result.get("allowed_files", [])),
                "verification_commands": list(result.get("verification_commands", [])),
                "approved": False,
                "implementation_notes": "",
                "errors": [],
                "replans": state.get("replans", 0) + 1,
                "fix_attempts": 0,
            }

        def persist_node(state):
            import decisions
            from backend.shared import ledger

            calls = [
                {"name": "td worktree create", "args": f"--issue {outer.issue_id}"},
                {
                    "name": "td verify-scope + td audit-code",
                    "args": f"attempts={state.get('fix_attempts', 0)}",
                },
            ]
            lines = decisions.build_decisions(
                calls,
                [f"valid={state.get('valid')}"],
                (state.get("implementation_notes", "") or "")[:500],
                reason="coder loop closed",
            )
            ledger.record(
                "coder.py",
                outer.last_model_spec.get("model") or "none",
                outer.thread_id,
                [],
                time.time(),
                extra={
                    "stage": "code",
                    "phase": "summary",
                    "llm_call": False,
                    "valid": state.get("valid"),
                    "issue_id": outer.issue_id,
                    "worktree_root": state.get("worktree_root", ""),
                    "decisions": lines,
                },
                provider=outer.last_model_spec.get("provider"),
            )
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
            "init",
            lambda s: END if s.get("fatal") else "plan",
            {"plan": "plan", END: END},
        )
        builder.add_conditional_edges(
            "plan",
            lambda s: END if s.get("fatal") else "plan_review",
            {"plan_review": "plan_review", END: END},
        )
        builder.add_conditional_edges(
            "plan_review",
            lambda s: (
                END
                if s.get("fatal")
                else ("implement" if s.get("approved") else "plan")
            ),
            {"implement": "implement", "plan": "plan", END: END},
        )
        builder.add_conditional_edges(
            "implement",
            lambda s: END if s.get("fatal") else "deterministic_gate",
            {"deterministic_gate": "deterministic_gate", END: END},
        )
        builder.add_conditional_edges(
            "deterministic_gate",
            route_after_gate,
            {
                "persist": "persist",
                "implement": "fix_loop",
                "replan": "validated_replanner",
                END: END,
            },
        )
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


if __name__ == "__main__":
    sys.exit(main())
