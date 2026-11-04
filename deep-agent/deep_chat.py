"""Multi-turn chat slice on create_deep_agent (spike).

Usage:
  deep_chat.py owner/repo <thread-id> <b64-prompt>
  deep_chat.py owner/repo <thread-id> --resume <approve|reject> [b64-message]
  deep_chat.py --validate-proposal <b64-json-proposal>

What this proves over chat_agent.py (stateless one-shot on
create_agent):

- create_deep_agent with a persistent SqliteSaver checkpointer keyed
  by thread_id, so conversation memory AND HITL pause state survive
  across one-shot processes (the server spawns per turn / per tap).
- 5 custom tools reusing the shared helpers (s5tools read-only file
  tools + retrieval RAG + one gated propose_action).
- interrupt_on={"propose_action": approve/reject} mirrors the buttons
  Approve/Reject taps; resume is Command(resume={"decisions": [...]}).

Stdout contract (turn/resume modes): PURE answer text, token chunks
printed as they stream, so the server's existing SSE chunk reader
works unchanged. On a HITL pause the last stdout line is exactly:

  INTERRUPT: {"thread_id": ..., "interrupts": [...]}

and the exit code is 3. Progress/diagnostics go to stderr. Every
invocation is ledgered.

Stdlib-only at import (like s5tools/model_router): langchain,
deepagents, and retrieval import lazily inside the functions that
need them, so system python can import the validators and run
--validate-proposal. Building/running the agent needs the venv.

Harness note: create_deep_agent also installs its built-in tools
(write_todos, ls/read_file/write_file/edit_file/glob/grep, task).
Those run on the default ephemeral StateBackend — scratch state that
never touches disk or the checkout. gate_chat_harness removes task
from the model (ungated subagent spend) and denies virtual-fs writes
(scratch the model could mistake for repo state); reads stay. The
only repo-write path is propose_action, and it only formats a
proposal; execution stays server-side behind the existing validated
endpoints.
"""

import base64
import json
import os
import re
import sys
import time

import cli

cli.bootstrap()

import s5tools  # noqa: E402
import model_router  # noqa: E402
import ledger  # noqa: E402
import observe  # noqa: E402
import tracing  # noqa: E402
import intent  # noqa: E402
import skills  # noqa: E402
import decisions  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
THREAD_DB = os.path.join(HERE, ".chat_threads", "chat.db")

THREAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PROMPT = 3000
# Trace middleware nodes consume budget (~4 steps per tool call,
# measured); 40 preserves room for ~9 tool calls per turn.
RECURSION_LIMIT = 40
# Tool-call budget per turn (ToolMessages in thread state). Past this
# the model sees only propose_action plus a nudge: looper models that
# would investigate to the recursion cutoff must propose-or-answer
# instead, and the cutoff becomes near-unreachable (#307). Legit
# turns use 1-3 tools; 6 splits the difference. Counts whole-thread
# history, so a post-approval continuation stays narrowed (it only
# needs to summarize).
CHAT_TOOL_BUDGET = 6
BUDGET_SPENT_NUDGE = (
    "Tool budget spent. Either call propose_action once with the "
    "exact change and stop, or answer from what you already gathered. "
    "No other tools are available."
)
# Sliding window over checkpoint history, applied at turn start.
# Checkpoints append indefinitely; without this long threads blow
# the context window. ~5-10 turns of history.
MAX_HISTORY_MESSAGES = 40
# Tool results carried into the salvage pass after a recursion cutoff.
SALVAGE_TOOL_TAIL = 12

# Propose-only maintainer sweep (roadmap item 7): read-only checks
# the agent can actually run, findings via propose_action (which
# always needs human approval). Scheduling is a deploy concern;
# this is the runnable mode.
SUPERVISOR_BRIEF = (
    "You are the repo maintainer on a scheduled sweep. Work read-only "
    "with list_dir/grep_repo/read_repo_file; track the sweep with "
    "write_todos. Checks: (1) TODO/FIXME/XXX/HACK markers — report "
    "file:line for each; (2) docs drift — docs/*.md paths that no "
    "longer exist in the checkout. Then either report all-clean or "
    "call propose_action(kind='issue') once per real finding (title + "
    "file:line evidence). Propose, never fix; end with a summary."
)
EXIT_NEEDS_APPROVAL = 3

# Mirror of ALLOWED_TD in ask_investigator.py (which mirrors
# ALLOWED_TD_COMMANDS in buttons/server.py, the source of truth).
# "N" is a PR-number placeholder; otherwise match verbatim.
ALLOWED_TD = [
    "td --json agent fix-ci --pr N --execute",
    "td gh read-pr-reviews --pr N",
    "td gh read-pr-comments --pr N",
    "td gh resolve-conflicts --pr N",
    "td context-warm",
    "td --json preflight",
    "td --json agent list-sessions",
    "td --json jules list-sessions",
    "td --json agent run-feedback-check",
]

# The buttons confirm pattern: Approve tap -> approve, Decline tap ->
# reject. Batched mode (deepagents default): one interrupt per model
# turn, value is a HITLRequest, resumed with {"decisions": [...]}
# matched to action_requests by position.
INTERRUPT_ON = {
    "propose_action": {
        "allowed_decisions": ["approve", "reject"],
        "description": "Review the proposed write action before it runs.",
    }
}

# deepagents resolves harness profiles by the BUILT instance's
# (provider, identifier): ChatOllama -> ollama:<model>, ChatOpenAI
# (our Nebius path) -> openai:<model>. Translate our spec provider
# names; anything else passes through for custom model classes.
PROFILE_PROVIDERS = {"ollama": "ollama", "nebius": "openai"}


def gate_chat_harness(spec):
    """Exclude task + deny virtual-fs writes for one chat model.

    Venv-only. Registers an exact provider:model harness profile
    excluding the task tool (spawns an ungated general-purpose
    subagent: unbounded spend with no approval gate) and returns the
    permissions list denying virtual-fs writes (scratch the model
    could mistake for repo state). Reads stay allowed; our 5 real
    tools are untouched. (#306)
    """
    from deepagents import HarnessProfileConfig, register_harness_profile
    from deepagents.middleware.filesystem import FilesystemPermission

    provider = PROFILE_PROVIDERS.get(spec["provider"], spec["provider"])
    register_harness_profile(
        f"{provider}:{spec['model']}",
        HarnessProfileConfig(excluded_tools={"task"}))
    return [FilesystemPermission(operations=["write"], paths=["/**"],
                                 mode="deny")]

SYSTEM = (
    "You are helping Ari from his phone. Be terse, no filler. "
    "Answer the question directly; when it is about the repo, "
    "investigate read-only first "
    "(list_dir, grep_repo, read_repo_file, search_docs). "
    "You cannot run, change, or deploy anything yourself — the only "
    "write path is the propose_action tool: call it once with the "
    "exact change and STOP; a human approves or rejects it. "
    "Always invoke tools through the function-calling interface. "
    "Never write a tool call out as JSON text. "
    "If a grep pattern has no matches, retry with a shorter distinctive "
    "fragment. "
    "Keep replies readable on a phone: short paragraphs or a short "
    "list, no wide tables."
)

# Trivial-turn system prompt: no tools, no repo context, no history.
TRIVIAL_SYSTEM = ("You are helping Ari from his phone. Be terse, no "
                  "filler. Answer briefly in one or two sentences.")


def _short_err(exc, limit=160):
    """Cap exception detail for Sorry lines (the prefix is kept verbatim,
    so eval gates still classify the run as invalid, not failed)."""
    text = str(exc)
    return text if len(text) <= limit else text[:limit] + "…"

DECISION_TYPES = ("approve", "edit", "reject", "respond")


def valid_thread_id(tid):
    """Thread ids double as chat session ids (same charset as before)."""
    return isinstance(tid, str) and bool(THREAD_RE.match(tid))


def _td_shape_re(shape):
    parts = []
    for tok in shape.split():
        if tok == "N":
            parts.append(r"[1-9][0-9]*")
        else:
            parts.append(re.escape(tok))
    return re.compile(r"\s+".join(parts) + r"\Z")


_TD_RES = [_td_shape_re(s) for s in ALLOWED_TD]

BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
DISPATCH_TASK_LIMIT = 4000


def _valid_branch(branch):
    """git-plausible branch name: charset, length, no dot-dot."""
    return (isinstance(branch, str)
            and 1 <= len(branch) <= 120
            and bool(BRANCH_RE.match(branch))
            and ".." not in branch
            and branch[0] not in "/-.")


def validate_proposal(payload):
    """Validate a propose_action payload. Returns (ok, text).

    ok=True: text is the formatted PROPOSE block (the same shape the
    server already parses for Ask proposals). ok=False: text is a
    REJECTED reason. Never raises on odd inputs; never executes.
    """
    if not isinstance(payload, dict):
        return False, "REJECTED: proposal must be an object"
    kind = payload.get("kind")
    if kind == "issue":
        title = payload.get("title")
        body = payload.get("body")
        if not isinstance(title, str) or not title.strip() \
                or len(title) > 120:
            return False, "REJECTED: issue needs a title (1-120 chars)"
        if not isinstance(body, str) or not body.strip() \
                or len(body) > 4000:
            return False, "REJECTED: issue needs a body (1-4000 chars)"
        return True, (
            f"PROPOSE issue\nTITLE: {title.strip()}\n"
            f"BODY:\n{body.strip()}\nEND PROPOSE"
        )
    if kind == "td":
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip() \
                or len(command) > 300:
            return False, "REJECTED: td needs a command (1-300 chars)"
        command = " ".join(command.split())
        for rx in _TD_RES:
            if rx.match(command):
                return True, (
                    f"PROPOSE td\nCOMMAND: {command}\nEND PROPOSE"
                )
        return False, (
            "REJECTED: td command is not an allowlisted shape "
            "(verbatim, changing only N to digits)"
        )
    if kind == "dispatch":
        repo = payload.get("repo")
        branch = payload.get("branch")
        task = payload.get("task")
        if not s5tools.valid_repo(repo):
            return False, "REJECTED: dispatch needs a repo (owner/name)"
        branch = branch.strip() if isinstance(branch, str) else branch
        if not _valid_branch(branch):
            return False, ("REJECTED: dispatch needs a branch "
                           "(1-120 chars, [A-Za-z0-9._/-], no ..)")
        if not isinstance(task, str) or not task.strip() \
                or len(task) > DISPATCH_TASK_LIMIT:
            return False, ("REJECTED: dispatch needs a task "
                           "(1-4000 chars)")
        return True, (
            f"PROPOSE dispatch\nREPO: {repo}\nBRANCH: {branch}\n"
            f"TASK:\n{task.strip()}\nEND PROPOSE"
        )
    return False, "REJECTED: kind must be 'issue', 'td', or 'dispatch'"


def decision_for_verdict(verdict, message=""):
    """Map a buttons tap to one HITL decision. Raises ValueError."""
    if verdict == "approve":
        return {"type": "approve"}
    if verdict == "reject":
        note = message if isinstance(message, str) and message.strip() \
            else "Declined by reviewer."
        return {"type": "reject", "message": note[:500]}
    raise ValueError(f"bad verdict {verdict!r}: want 'approve'/'reject'")


def approved_proposal_blocks(action_requests):
    """Validated PROPOSE/REJECTED texts for approved interrupt requests.

    Pure: the model decides what to SAY after approval, so the server
    cannot rely on the reply carrying anything machine-readable.
    This re-validates the approved args deterministically so _cmd_resume
    can print the executable blocks itself.
    """
    texts = []
    for req in action_requests or []:
        if not isinstance(req, dict):
            continue
        _ok, text = validate_proposal(req.get("args", {}))
        texts.append(text)
    return texts


def resume_payload(decisions):
    """Wrap per-request decisions for Command(resume=...)."""
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("decisions must be a non-empty list")
    for d in decisions:
        if not isinstance(d, dict) or d.get("type") not in DECISION_TYPES:
            raise ValueError(f"bad decision: {d!r}")
    return {"decisions": decisions}


def build_tools(checkout):
    """The 5 custom tools. Venv-only (imports langchain + retrieval)."""
    from langchain_core.tools import tool  # noqa: E402

    import retrieval  # noqa: E402

    @tool
    def read_repo_file(path: str) -> str:
        """Read a file from the repo checkout (read-only).

        Args:
            path: Repo-relative path, e.g. buttons/server.py.
        """
        return s5tools.read_repo_file(checkout, path)

    @tool
    def grep_repo(pattern: str) -> str:
        """Search file contents in the repo checkout (read-only grep).

        If a long pattern has no matches, retry with a shorter
        distinctive fragment.

        Args:
            pattern: Literal search text.
        """
        return s5tools.grep_repo(checkout, pattern)

    @tool
    def list_dir(path: str = "") -> str:
        """List files in a repo checkout directory (read-only).

        Args:
            path: Repo-relative directory, '' for the checkout root.
        """
        return s5tools.list_dir(checkout, path or "")

    @tool
    def search_docs(query: str) -> str:
        """Search S5 runbooks and docs for how-to and process answers.

        Args:
            query: Short question (2-10 words).
        """
        try:
            hits = retrieval.get_retriever(k=3).invoke(query)
        except Exception as exc:
            return f"doc search unavailable: {exc}"
        return retrieval.format_hits(hits)

    @tool
    def propose_action(kind: str, title: str = "",
                       body: str = "", command: str = "",
                       repo: str = "", branch: str = "",
                       task: str = "") -> str:
        """Propose the ONE write action this turn needs (human-approved).

        Call at most once per turn, after read-only investigation, then
        STOP and wait for the human's approve/reject decision. Never
        executes anything: on approval this only returns the validated
        proposal text for the server to run.

        Args:
            kind: 'issue' (file a GitHub issue), 'td' (run one
                allowlisted td command), or 'dispatch' (implement via
                a BoomTick worktree session).
            title: Issue title (kind='issue').
            body: Issue body markdown (kind='issue').
            command: Exact td command (kind='td'; allowlisted shapes
                only, changing N to digits).
            repo: Target repo owner/name (kind='dispatch').
            branch: Base branch to work from (kind='dispatch').
            task: Implementation brief (kind='dispatch').
        """
        ok, text = validate_proposal(
            {"kind": kind, "title": title, "body": body,
             "command": command, "repo": repo, "branch": branch,
             "task": task}
        )
        return text

    return [read_repo_file, grep_repo, list_dir, search_docs, propose_action]


def build_model(prefer=None):
    """Chat model via model_router. Returns (model, spec). Venv-only.

    prefer is the per-turn override ("ollama"/"nebius"); None keeps
    today's ladder (env contract applies)."""
    spec = model_router.resolve_llm(prefer=prefer)
    kwargs = model_router.chat_kwargs(spec)
    # No-think is the production setting: on this CPU-only box a qwen3
    # thinking trace takes 15+ min per prompt. CHAT_THINK=1 re-enables.
    think = os.environ.get("CHAT_THINK", "") == "1"
    if spec["provider"] == "ollama":
        from langchain_ollama import ChatOllama  # noqa: E402

        if not think:
            kwargs["model_kwargs"] = {"think": False}
        return ChatOllama(**kwargs), spec
    from langchain_openai import ChatOpenAI  # noqa: E402

    return ChatOpenAI(api_key=os.environ["NEBIUS_API_KEY"], **kwargs), spec


def open_checkpointer(path=THREAD_DB):
    """Persistent checkpointer; falls back to per-process memory.

    Returns (saver, persistent, holder). The caller MUST keep holder
    referenced for the checkpointer's whole lifetime — it owns the DB
    connection, and dropping it closes the database.
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
    except ImportError:
        from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

        print("sqlite saver unavailable; memory is per-process only",
              file=sys.stderr)
        return InMemorySaver(), False, None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cm = SqliteSaver.from_conn_string(path)
    return cm.__enter__(), True, cm


def tool_budget_middleware():
    """ToolBudgetMiddleware instance. Venv-only (langchain import).

    Counts ToolMessages in the thread: within budget the request
    passes through; past CHAT_TOOL_BUDGET the model sees only
    propose_action plus the spent nudge, and stray non-propose calls
    are rejected at the tool boundary instead of executing (#307).
    Sync hooks only: chat turns use sync invoke/stream.
    """
    from langchain.agents.middleware.types import AgentMiddleware
    from langchain_core.messages import SystemMessage, ToolMessage

    def _spent(state):
        try:
            messages = state.get("messages", [])
        except AttributeError:
            return 0
        return sum(isinstance(m, ToolMessage) for m in messages)

    def _name(tool):
        return tool.get("name") if isinstance(tool, dict) \
            else getattr(tool, "name", None)

    class ToolBudget(AgentMiddleware):
        def wrap_model_call(self, request, handler):
            if _spent(request.state) < CHAT_TOOL_BUDGET:
                return handler(request)
            narrowed = [t for t in request.tools
                        if _name(t) == "propose_action"]
            return handler(request.override(
                tools=narrowed,
                messages=[*request.messages,
                          SystemMessage(content=BUDGET_SPENT_NUDGE)]))

        def wrap_tool_call(self, request, handler):
            if request.tool_call["name"] == "propose_action":
                return handler(request)
            if _spent(request.state) < CHAT_TOOL_BUDGET:
                return handler(request)
            call = request.tool_call
            return ToolMessage(
                content="Error: tool budget spent, call propose_action "
                        "or answer from gathered evidence.",
                tool_call_id=call["id"] or "",
                name=call["name"],
                status="error",
            )

    return ToolBudget()


def build_agent(repo, checkpointer, model=None, recorder=None,
                intent_hint=None, spec=None):
    """create_deep_agent chat slice. Venv-only. Model injectable."""
    from deepagents import create_deep_agent  # noqa: E402

    if model is None:
        model, spec = build_model()
    stack = [tool_budget_middleware()]
    if recorder is not None:
        stack.extend(observe.trace_middleware(recorder))
    kwargs = {"middleware": stack}
    permissions = gate_chat_harness(spec) if spec is not None else None
    system = f"Repo: {repo}. " + SYSTEM
    if intent_hint:
        system += " " + intent_hint
    return create_deep_agent(
        model=model,
        tools=build_tools(s5tools.checkout_for(repo)),
        system_prompt=system,
        interrupt_on=dict(INTERRUPT_ON),
        permissions=permissions,
        checkpointer=checkpointer,
        **kwargs,
    )


def _pending_interrupts(agent, config):
    found = []
    snap = agent.get_state(config)
    for task in snap.tasks:
        for iv in getattr(task, "interrupts", None) or []:
            found.append({"id": iv.id, "value": iv.value})
    return found


def _message_role(message):
    """human/system/other by class name (no langchain import)."""
    name = message.__class__.__name__
    if name == "HumanMessage":
        return "human"
    if name == "SystemMessage":
        return "system"
    return "other"


def plan_history_trim(messages, keep=MAX_HISTORY_MESSAGES):
    """Ids to drop from checkpoint history. Pure, no langchain import.

    Keeps a leading SystemMessage plus the newest `keep` messages,
    snapped to start at a HumanMessage boundary so a ToolMessage is
    never orphaned from its AI tool call. Returns [] when within
    budget or when no safe boundary exists (trim skipped, not forced).
    """
    messages = list(messages or [])
    rest = messages[1:] if messages and _message_role(
        messages[0]) == "system" else messages
    if len(rest) <= keep:
        return []
    window = rest[-keep:]
    starts = [i for i, m in enumerate(window)
              if _message_role(m) == "human"]
    if not starts:
        return []
    keep_ids = {id(m) for m in window[starts[0]:]}
    return [m.id for m in rest if id(m) not in keep_ids
            and getattr(m, "id", None)]


def trim_thread_history(agent, config, keep=MAX_HISTORY_MESSAGES):
    """Drop aged-out checkpoint messages. Best-effort; never raises."""
    try:
        snap = agent.get_state(config)
        messages = (snap.values or {}).get("messages", []) or []
        drop = plan_history_trim(messages, keep=keep)
        if not drop:
            return 0
        from langchain_core.messages import RemoveMessage  # noqa: E402
        agent.update_state(
            config, {"messages": [RemoveMessage(id=i) for i in drop]})
        return len(drop)
    except Exception:
        return 0


def _salvage_input(agent, config, first_input, tool_messages):
    """Message list for the post-cutoff final-answer pass. Pure shape."""
    from langchain_core.messages import HumanMessage  # noqa: E402

    if isinstance(first_input, dict) and "messages" in first_input:
        base = list(first_input["messages"])
        evidence = list(tool_messages[-SALVAGE_TOOL_TAIL:])
    else:
        try:
            thread = (agent.get_state(config).values or {}).get(
                "messages", []) or []
        except Exception:
            thread = []
        base, evidence = [], list(thread[-SALVAGE_TOOL_TAIL:])
    return base + evidence + [HumanMessage(
        content="Answer now using only the conversation and tool "
        "results above. Do not make any more tool calls.")]


def _stream(agent, first_input, config, model=None):
    """Stream one agent invocation.

    Returns (reply, tool_msgs, msgs, salvaged). On a tool-loop
    recursion cutoff with a model available, one final no-tools
    pass answers from gathered evidence instead of dying (same
    salvage as chat_agent); without a model the error propagates.
    """
    from langchain_core.messages import AIMessage, AIMessageChunk  # noqa: E402
    from langgraph.errors import GraphRecursionError  # noqa: E402

    text_parts = []
    tool_messages = []
    last_ai = ""
    try:
        stream = agent.stream(
            first_input,
            config={**config, "recursion_limit": RECURSION_LIMIT,
                    "callbacks": tracing.tracing_callbacks()},
            stream_mode=["messages", "updates"],
        )
        for mode, payload in stream:
            if mode == "messages":
                chunk = payload[0]
                if isinstance(chunk, AIMessageChunk) and isinstance(
                    chunk.content, str
                ) and chunk.content:
                    print(chunk.content, end="", flush=True)
                    text_parts.append(chunk.content)
            elif mode == "updates":
                for update in payload.values():
                    # Updates are usually {messages: [...]} dicts, but
                    # some nodes (e.g. around interrupts) emit tuples
                    # instead.
                    msgs = update.get("messages", []) \
                        if isinstance(update, dict) else []
                    for m in msgs:
                        if m.__class__.__name__ == "ToolMessage":
                            tool_messages.append(m)
                        elif isinstance(m, AIMessage) and isinstance(
                            m.content, str
                        ) and m.content and not getattr(
                            m, "tool_calls", None
                        ):
                            last_ai = m.content
    except GraphRecursionError:
        if model is None:
            raise
        try:
            answer = model.invoke(_salvage_input(
                agent, config, first_input, tool_messages))
            salvaged = (answer.content or "").strip()
        except Exception:
            salvaged = ""
        if not salvaged:
            raise
        print(salvaged, flush=True)
        return salvaged, tool_messages, \
            [AIMessage(content=salvaged)], True
    print("", flush=True)
    # Streaming models feed text_parts; non-streaming ones only appear
    # whole in updates. Prefer the last text-only AI message.
    reply = (last_ai or "".join(text_parts)).strip()
    return reply, tool_messages, [AIMessage(content=reply)], False


def _emit_interrupt(thread_id, interrupts):
    try:
        blob = json.dumps({"thread_id": thread_id,
                           "interrupts": interrupts})
    except (TypeError, ValueError) as exc:
        print(f"interrupt payload not JSON: {exc}", file=sys.stderr)
        return 1
    print(f"INTERRUPT: {blob}", flush=True)
    return EXIT_NEEDS_APPROVAL


def _cmd_validate(raw):
    try:
        payload = json.loads(cli.b64decode(raw))
    except Exception:
        print(json.dumps({"ok": False, "text": "REJECTED: bad input"}))
        return 2
    ok, text = validate_proposal(payload)
    print(json.dumps({"ok": ok, "text": text}))
    return 0


def _cmd_trivial_turn(repo, thread_id, prompt, started, model, spec):
    """Fast path for greetings: one direct model call, no tools, no
    history. Still ledgered (usage when reported) with a minimal
    run_start/model_request/model_response/run_end trace so the run
    expands in Past runs like full turns."""
    from langchain_core.messages import (  # noqa: E402
        AIMessage, HumanMessage, SystemMessage)
    recorder = observe.TraceRecorder(
        observe.default_trace_path("deep_chat", thread_id))
    recorder.run_start("deep_chat.py", thread_id,
                       f"{spec['provider']}:{spec['model']}")
    messages = [SystemMessage(content=TRIVIAL_SYSTEM),
                HumanMessage(content=prompt)]
    try:
        recorder.model_request(len(messages))
        answer = model.invoke(messages)
        reply = (answer.content or "").strip()
    except Exception as exc:
        reply = f"Sorry, the assistant failed: {_short_err(exc)}"
        recorder.run_end(False)
        recorder.close()
        ledger.record("deep_chat.py", f"{spec['provider']}:{spec['model']}",
                      thread_id, messages, started,
                      extra={"repo": repo, "mode": "turn",
                             "intent": "trivial", "prompt": prompt,
                             "trace": recorder.path,
                             "decisions": decisions.build_decisions(
                                 [], [], reply, error=str(exc)),
                             "error": str(exc)[:200]})
        return cli.sorry(reply)
    print(reply, flush=True)
    raw_usage = getattr(answer, "usage_metadata", None)
    usage = None
    if isinstance(raw_usage, dict):
        usage = {k: raw_usage.get(k, 0)
                 for k in ("input_tokens", "output_tokens", "total_tokens")}
    recorder.model_response(raw_usage if isinstance(raw_usage, dict)
                            else {})
    recorder.run_end(True)
    recorder.close()
    ledger.record("deep_chat.py", f"{spec['provider']}:{spec['model']}",
                  thread_id, messages + [AIMessage(content=reply)], started,
                  extra={"repo": repo, "mode": "turn", "intent": "trivial",
                         "prompt": prompt, "trace": recorder.path,
                         "decisions": decisions.build_decisions(
                             [], [], reply)},
                  usage=usage, calls=[])
    return 0


def _cmd_turn(repo, thread_id, raw_prompt, prefer=None):
    prompt = cli.b64decode(raw_prompt)
    if prompt is None:
        return cli.usage_error("bad base64 prompt")
    if not prompt.strip() or len(prompt) > MAX_PROMPT:
        return cli.usage_error("bad prompt")
    started = time.time()
    try:
        model, spec = build_model(prefer=prefer)
    except (RuntimeError, ValueError) as exc:
        return cli.sorry(
            f"Sorry, no model is available right now: {_short_err(exc)}")
    except ImportError as exc:
        # Stdout, like every other Sorry line: exit 1 promises the
        # reply on stdout so the SSE chunk reader shows it on the phone.
        return cli.sorry(
            f"Sorry, the chat backend is not installed: {_short_err(exc)}")
    print(f"deep_chat via {spec['provider']}:{spec['model']}",
          file=sys.stderr)
    turn_intent = intent.classify(prompt)
    if turn_intent == "trivial":
        return _cmd_trivial_turn(repo, thread_id, prompt, started,
                                 model, spec)
    from langchain_core.messages import HumanMessage  # noqa: E402

    recorder = observe.TraceRecorder(
        observe.default_trace_path("deep_chat", thread_id))
    recorder.run_start("deep_chat.py", thread_id,
                       f"{spec['provider']}:{spec['model']}")
    saver, _, _holder = open_checkpointer()
    agent = build_agent(repo, saver, model=model, recorder=recorder,
                        intent_hint=skills.system_addition(turn_intent)
                        or None, spec=spec)
    config = {"configurable": {"thread_id": thread_id}}
    trimmed = trim_thread_history(agent, config)
    if trimmed:
        print(f"trimmed {trimmed} aged-out history message(s)",
              file=sys.stderr)
    messages = [HumanMessage(content=prompt)]
    try:
        reply, tool_msgs, _, salvaged = _stream(
            agent, {"messages": messages}, config, model=model)
    except Exception as exc:
        reply = f"Sorry, the assistant failed: {_short_err(exc)}"
        recorder.run_end(False)
        recorder.close()
        _usage, _calls = recorder.totals()
        ledger.record(
            "deep_chat.py", f"{spec['provider']}:{spec['model']}",
            thread_id, messages, started,
            extra={"repo": repo, "mode": "turn", "trace": recorder.path,
                   "intent": turn_intent, "prompt": prompt,
                   "decisions": decisions.build_decisions(
                       _calls, [], reply, error=str(exc)),
                   "error": str(exc)[:200]},
            usage=_usage, calls=_calls,
        )
        return cli.sorry(reply)
    from langchain_core.messages import AIMessage  # noqa: E402

    recorder.run_end(True)
    recorder.close()
    print(f"trace: {recorder.path}", file=sys.stderr)
    _usage, _calls = recorder.totals()
    tool_results = [decisions.tool_text(getattr(m, "content", ""))
                    for m in tool_msgs]
    ledger.record(
        "deep_chat.py", f"{spec['provider']}:{spec['model']}",
        thread_id, messages + tool_msgs + [AIMessage(content=reply)],
        started, extra={"repo": repo, "mode": "turn",
                        "trace": recorder.path, "intent": turn_intent,
                        "prompt": prompt,
                        "decisions": decisions.build_decisions(
                            _calls, tool_results, reply),
                        **({"salvaged": True} if salvaged else {})},
        usage=_usage, calls=_calls,
    )
    pending = _pending_interrupts(agent, config)
    if pending:
        return _emit_interrupt(thread_id, pending)
    return 0


def _cmd_resume(repo, thread_id, verdict, raw_message):
    try:
        decision = decision_for_verdict(verdict)
    except ValueError as exc:
        return cli.usage_error(str(exc))
    message = ""
    if raw_message is not None:
        message = cli.b64decode(raw_message)
        if message is None:
            return cli.usage_error("bad base64 message")
        try:
            decision = decision_for_verdict(verdict, message)
        except ValueError as exc:
            return cli.usage_error(str(exc))
    started = time.time()
    try:
        model, spec = build_model()
    except (RuntimeError, ValueError) as exc:
        return cli.sorry(
            f"Sorry, no model is available right now: {_short_err(exc)}")
    except ImportError as exc:
        # Stdout, like every other Sorry line: exit 1 promises the
        # reply on stdout so the SSE chunk reader shows it on the phone.
        return cli.sorry(
            f"Sorry, the chat backend is not installed: {_short_err(exc)}")
    print(f"deep_chat resume via {spec['provider']}:{spec['model']}",
          file=sys.stderr)
    from langgraph.types import Command  # noqa: E402
    from langchain_core.messages import AIMessage  # noqa: E402

    recorder = observe.TraceRecorder(
        observe.default_trace_path("deep_chat", thread_id))
    recorder.run_start("deep_chat.py", thread_id,
                       f"{spec['provider']}:{spec['model']}")
    saver, _, _holder = open_checkpointer()
    agent = build_agent(repo, saver, model=model, recorder=recorder,
                        spec=spec)
    config = {"configurable": {"thread_id": thread_id}}
    pending = _pending_interrupts(agent, config)
    if not pending:
        print(f"no pending approval for thread {thread_id!r}",
              file=sys.stderr)
        recorder.run_end(False)
        recorder.close()
        return 1
    # Batched mode: one decision per action_request, by position.
    first = pending[0].get("value") or {}
    count = len(first.get("action_requests", []) or []) or 1
    try:
        reply, tool_msgs, _, salvaged = _stream(
            agent, Command(resume=resume_payload([decision] * count)),
            config, model=model)
    except Exception as exc:
        reply = f"Sorry, the assistant failed: {_short_err(exc)}"
        recorder.run_end(False)
        recorder.close()
        _usage, _calls = recorder.totals()
        ledger.record(
            "deep_chat.py", f"{spec['provider']}:{spec['model']}",
            thread_id, [AIMessage(content=reply)], started,
            extra={"repo": repo, "mode": "resume", "verdict": verdict,
                   "trace": recorder.path,
                   "decisions": decisions.build_decisions(
                       _calls, [], reply, verdict=verdict,
                       message=message, error=str(exc),
                       proposals=approved_proposal_blocks(
                           first.get("action_requests", []))),
                   "error": str(exc)[:200]},
            usage=_usage, calls=_calls,
        )
        return cli.sorry(reply)
    recorder.run_end(True)
    recorder.close()
    print(f"trace: {recorder.path}", file=sys.stderr)
    _usage, _calls = recorder.totals()
    ledger.record(
        "deep_chat.py", f"{spec['provider']}:{spec['model']}",
        thread_id, tool_msgs + [AIMessage(content=reply)], started,
        extra={"repo": repo, "mode": "resume", "verdict": verdict,
               "trace": recorder.path,
               "decisions": decisions.build_decisions(
                   _calls, [decisions.tool_text(getattr(m, "content", ""))
                            for m in tool_msgs],
                   reply, verdict=verdict, message=message,
                   proposals=approved_proposal_blocks(
                       first.get("action_requests", []))),
               **({"salvaged": True} if salvaged else {})},
        usage=_usage, calls=_calls,
    )
    if verdict == "approve":
        for text in approved_proposal_blocks(
                first.get("action_requests", [])):
            print(text, flush=True)
    pending = _pending_interrupts(agent, config)
    if pending:
        return _emit_interrupt(thread_id, pending)
    return 0


def extract_prefer(argv):
    """Per-turn backend override from --prefer=ollama|nebius.

    Scanned (not positional) so it survives every argv shape; the
    b64 prompt slot can never match ('-' is outside the base64
    alphabet). Returns the provider or None. Raises ValueError on a
    value the router would reject, so misuse fails at the CLI, not
    as a confusing Sorry."""
    for arg in argv[1:]:
        if isinstance(arg, str) and arg.startswith("--prefer="):
            value = arg.split("=", 1)[1].strip().lower()
            if value not in ("ollama", "nebius"):
                raise ValueError(
                    f"bad --prefer={value!r}: want 'ollama' or 'nebius'")
            return value
    return None


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    try:
        prefer = extract_prefer(argv)
    except ValueError as exc:
        return cli.usage_error(str(exc))
    # Strip before positional parsing so the flag never shifts the
    # resume/message slots.
    argv = [a for a in argv
           if not (isinstance(a, str) and a.startswith("--prefer="))]
    if len(argv) == 3 and argv[1] == "--validate-proposal":
        return _cmd_validate(argv[2])
    if len(argv) == 3 and argv[2] == "--supervise":
        if not s5tools.valid_repo(argv[1]):
            return cli.usage_error(f"bad repo: {argv[1]!r}")
        thread = f"supervise-{argv[1].split('/')[1]}"
        raw = base64.b64encode(SUPERVISOR_BRIEF.encode()).decode()
        return _cmd_turn(argv[1], thread, raw)
    if len(argv) < 4 or not s5tools.valid_repo(argv[1]):
        return cli.usage_error(
            "usage: deep_chat.py owner/repo <thread-id> <b64-prompt>"
            " [--prefer=ollama|nebius]",
            "   or: deep_chat.py owner/repo <thread-id> --resume "
            "approve|reject [b64-message]",
            "   or: deep_chat.py owner/repo --supervise",
            "   or: deep_chat.py --validate-proposal <b64-json>")
    if not valid_thread_id(argv[2]):
        return cli.usage_error("bad thread-id")
    repo, thread_id = argv[1], argv[2]
    try:
        s5tools.checkout_for(repo)
    except ValueError:
        return cli.usage_error(f"bad repo: {repo!r}")
    if argv[3] == "--resume":
        if len(argv) < 5:
            return cli.usage_error("resume needs a verdict")
        return _cmd_resume(repo, thread_id, argv[4],
                           argv[5] if len(argv) > 5 else None)
    return _cmd_turn(repo, thread_id, argv[3], prefer=prefer)


if __name__ == "__main__":
    raise SystemExit(main())
