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
never touches disk or the checkout. The only repo-write path is
propose_action, and it only formats a proposal; execution stays
server-side behind the existing validated endpoints.
"""

import base64
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import s5tools  # noqa: E402
import model_router  # noqa: E402
import ledger  # noqa: E402
import observe  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
THREAD_DB = os.path.join(HERE, ".chat_threads", "chat.db")

THREAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_PROMPT = 3000
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
    return False, "REJECTED: kind must be 'issue' or 'td'"


def decision_for_verdict(verdict, message=""):
    """Map a buttons tap to one HITL decision. Raises ValueError."""
    if verdict == "approve":
        return {"type": "approve"}
    if verdict == "reject":
        note = message if isinstance(message, str) and message.strip() \
            else "Declined by reviewer."
        return {"type": "reject", "message": note[:500]}
    raise ValueError(f"bad verdict {verdict!r}: want 'approve'/'reject'")


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
                       body: str = "", command: str = "") -> str:
        """Propose the ONE write action this turn needs (human-approved).

        Call at most once per turn, after read-only investigation, then
        STOP and wait for the human's approve/reject decision. Never
        executes anything: on approval this only returns the validated
        proposal text for the server to run.

        Args:
            kind: 'issue' (file a GitHub issue) or 'td' (run one
                allowlisted td command).
            title: Issue title (kind='issue').
            body: Issue body markdown (kind='issue').
            command: Exact td command (kind='td'; allowlisted shapes
                only, changing N to digits).
        """
        ok, text = validate_proposal(
            {"kind": kind, "title": title, "body": body, "command": command}
        )
        return text

    return [read_repo_file, grep_repo, list_dir, search_docs, propose_action]


def build_model():
    """Chat model via model_router. Returns (model, spec). Venv-only."""
    spec = model_router.resolve_llm()
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


def build_agent(repo, checkpointer, model=None, recorder=None):
    """create_deep_agent chat slice. Venv-only. Model injectable."""
    from deepagents import create_deep_agent  # noqa: E402

    if model is None:
        model, _ = build_model()
    kwargs = {}
    if recorder is not None:
        kwargs["middleware"] = observe.trace_middleware(recorder)
    return create_deep_agent(
        model=model,
        tools=build_tools(s5tools.checkout_for(repo)),
        system_prompt=f"Repo: {repo}. " + SYSTEM,
        interrupt_on=dict(INTERRUPT_ON),
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


def _stream(agent, first_input, config):
    """Stream one agent invocation; returns (reply, tool_msgs, msgs)."""
    from langchain_core.messages import AIMessage, AIMessageChunk  # noqa: E402

    text_parts = []
    tool_messages = []
    last_ai = ""
    stream = agent.stream(
        first_input,
        # 20: trace middleware nodes consume budget (~4 steps per
        # tool call, measured); preserves room for ~4 tool calls.
        config={**config, "recursion_limit": 20},
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
                # Updates are usually {messages: [...]} dicts, but some
                # nodes (e.g. around interrupts) emit tuples instead.
                msgs = update.get("messages", []) \
                    if isinstance(update, dict) else []
                for m in msgs:
                    if m.__class__.__name__ == "ToolMessage":
                        tool_messages.append(m)
                    elif isinstance(m, AIMessage) and isinstance(
                        m.content, str
                    ) and m.content and not getattr(m, "tool_calls", None):
                        last_ai = m.content
    print("", flush=True)
    # Streaming models feed text_parts; non-streaming ones only appear
    # whole in updates. Prefer the last text-only AI message.
    reply = (last_ai or "".join(text_parts)).strip()
    return reply, tool_messages, [AIMessage(content=reply)]


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
        payload = json.loads(base64.b64decode(raw).decode())
    except Exception:
        print(json.dumps({"ok": False, "text": "REJECTED: bad input"}))
        return 2
    ok, text = validate_proposal(payload)
    print(json.dumps({"ok": ok, "text": text}))
    return 0


def _cmd_turn(repo, thread_id, raw_prompt):
    try:
        prompt = base64.b64decode(raw_prompt).decode()
    except Exception:
        print("bad base64 prompt", file=sys.stderr)
        return 2
    if not prompt.strip() or len(prompt) > MAX_PROMPT:
        print("bad prompt", file=sys.stderr)
        return 2
    started = time.time()
    try:
        model, spec = build_model()
    except (RuntimeError, ValueError) as exc:
        print(f"Sorry, no model is available right now: {_short_err(exc)}")
        return 1
    except ImportError as exc:
        # Stdout, like every other Sorry line: exit 1 promises the
        # reply on stdout so the SSE chunk reader shows it on the phone.
        print(f"Sorry, the chat backend is not installed: {_short_err(exc)}")
        return 1
    print(f"deep_chat via {spec['provider']}:{spec['model']}",
          file=sys.stderr)
    from langchain_core.messages import HumanMessage  # noqa: E402

    recorder = observe.TraceRecorder(
        observe.default_trace_path("deep_chat", thread_id))
    recorder.run_start("deep_chat.py", thread_id,
                       f"{spec['provider']}:{spec['model']}")
    saver, _, _holder = open_checkpointer()
    agent = build_agent(repo, saver, model=model, recorder=recorder)
    config = {"configurable": {"thread_id": thread_id}}
    messages = [HumanMessage(content=prompt)]
    try:
        reply, tool_msgs, _ = _stream(
            agent, {"messages": messages}, config)
    except Exception as exc:
        reply = f"Sorry, the assistant failed: {_short_err(exc)}"
        print(reply, flush=True)
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "deep_chat.py", f"{spec['provider']}:{spec['model']}",
            thread_id, messages, started,
            extra={"repo": repo, "mode": "turn", "trace": recorder.path,
                   "error": str(exc)[:200]},
        )
        return 1
    from langchain_core.messages import AIMessage  # noqa: E402

    recorder.run_end(True)
    recorder.close()
    print(f"trace: {recorder.path}", file=sys.stderr)
    ledger.record(
        "deep_chat.py", f"{spec['provider']}:{spec['model']}",
        thread_id, messages + tool_msgs + [AIMessage(content=reply)],
        started, extra={"repo": repo, "mode": "turn",
                        "trace": recorder.path},
    )
    pending = _pending_interrupts(agent, config)
    if pending:
        return _emit_interrupt(thread_id, pending)
    return 0


def _cmd_resume(repo, thread_id, verdict, raw_message):
    try:
        decision = decision_for_verdict(verdict)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    message = ""
    if raw_message is not None:
        try:
            message = base64.b64decode(raw_message).decode()
        except Exception:
            print("bad base64 message", file=sys.stderr)
            return 2
        try:
            decision = decision_for_verdict(verdict, message)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    started = time.time()
    try:
        model, spec = build_model()
    except (RuntimeError, ValueError) as exc:
        print(f"Sorry, no model is available right now: {_short_err(exc)}")
        return 1
    except ImportError as exc:
        # Stdout, like every other Sorry line: exit 1 promises the
        # reply on stdout so the SSE chunk reader shows it on the phone.
        print(f"Sorry, the chat backend is not installed: {_short_err(exc)}")
        return 1
    print(f"deep_chat resume via {spec['provider']}:{spec['model']}",
          file=sys.stderr)
    from langgraph.types import Command  # noqa: E402
    from langchain_core.messages import AIMessage  # noqa: E402

    recorder = observe.TraceRecorder(
        observe.default_trace_path("deep_chat", thread_id))
    recorder.run_start("deep_chat.py", thread_id,
                       f"{spec['provider']}:{spec['model']}")
    saver, _, _holder = open_checkpointer()
    agent = build_agent(repo, saver, model=model, recorder=recorder)
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
        reply, tool_msgs, _ = _stream(
            agent, Command(resume=resume_payload([decision] * count)),
            config)
    except Exception as exc:
        reply = f"Sorry, the assistant failed: {_short_err(exc)}"
        print(reply, flush=True)
        recorder.run_end(False)
        recorder.close()
        ledger.record(
            "deep_chat.py", f"{spec['provider']}:{spec['model']}",
            thread_id, [AIMessage(content=reply)], started,
            extra={"repo": repo, "mode": "resume", "verdict": verdict,
                   "trace": recorder.path, "error": str(exc)[:200]},
        )
        return 1
    recorder.run_end(True)
    recorder.close()
    print(f"trace: {recorder.path}", file=sys.stderr)
    ledger.record(
        "deep_chat.py", f"{spec['provider']}:{spec['model']}",
        thread_id, tool_msgs + [AIMessage(content=reply)], started,
        extra={"repo": repo, "mode": "resume", "verdict": verdict,
               "trace": recorder.path},
    )
    pending = _pending_interrupts(agent, config)
    if pending:
        return _emit_interrupt(thread_id, pending)
    return 0


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    if len(argv) == 3 and argv[1] == "--validate-proposal":
        return _cmd_validate(argv[2])
    if len(argv) < 4 or not s5tools.valid_repo(argv[1]):
        print("usage: deep_chat.py owner/repo <thread-id> <b64-prompt>",
              file=sys.stderr)
        print("   or: deep_chat.py owner/repo <thread-id> --resume "
              "approve|reject [b64-message]", file=sys.stderr)
        print("   or: deep_chat.py --validate-proposal <b64-json>",
              file=sys.stderr)
        return 2
    if not valid_thread_id(argv[2]):
        print("bad thread-id", file=sys.stderr)
        return 2
    repo, thread_id = argv[1], argv[2]
    try:
        s5tools.checkout_for(repo)
    except ValueError:
        print(f"bad repo: {repo!r}", file=sys.stderr)
        return 2
    if argv[3] == "--resume":
        if len(argv) < 5:
            print("resume needs a verdict", file=sys.stderr)
            return 2
        return _cmd_resume(repo, thread_id, argv[4],
                           argv[5] if len(argv) > 5 else None)
    return _cmd_turn(repo, thread_id, argv[3])


if __name__ == "__main__":
    raise SystemExit(main())
