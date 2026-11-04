"""Chat turn agent: LangChain replacement for `pi -p` in /buttons/api/chat.

Usage: chat_agent.py owner/repo <session-id> <base64-prompt> [base64-history]

Stateless per turn (like `pi -p`): the server owns the session, this
script answers one prompt with optional prior history (base64 JSON list
of {"role": "user"|"assistant", "content": ...}, capped at 20 turns).

Stdout is PURE answer text (token-chunks printed as they stream, so the
server's existing SSE chunk reader works unchanged — same abort via
proc.terminate, no server changes needed for cutover). Progress and
diagnostics go to stderr. Read-only repo tools, all scoped to the
checkout. Model via model_router (Ollama local first, Nebius cheap
fallback). Every run is ledgered.

Cutover (peer's lane, not done here): swap the `pi -p ...` command in
_handle_chat_stream for this script; the SSE plumbing stays identical.
"""

import json
import os
import re
import sys
import time

import cli

cli.bootstrap()

import s5tools  # noqa: E402
import model_router  # noqa: E402
import deep_chat  # noqa: E402
import ledger  # noqa: E402
import tracing  # noqa: E402
from langgraph.errors import GraphRecursionError  # noqa: E402
from langchain.agents import create_agent  # noqa: E402
from langchain_core.messages import (  # noqa: E402
    AIMessage,
    AIMessageChunk,
    HumanMessage,
)
from langchain_core.tools import tool  # noqa: E402

SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_HISTORY = 20
# Tool results carried into the salvage pass after a recursion cutoff.
SALVAGE_TOOL_TAIL = 12

_checkout = ""

SYSTEM = (
    "You are helping Ari from his phone. Be terse, no filler. "
    "Answer the question directly; when it is about the repo, investigate "
    "read-only with your tools first (list_dir, grep_repo, read_repo_file). "
    "For how-to/run/deploy questions, read the relevant docs, README, or "
    "compose file before answering; never guess run commands from "
    "directory names alone. "
    "You cannot run, change, or deploy anything — say so if asked. "
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


@tool
def read_repo_file(path: str) -> str:
    """Read a file from the repo checkout (read-only).

    Args:
        path: Repo-relative path, e.g. buttons/server.py.
    """
    return s5tools.read_repo_file(_checkout, path)


@tool
def grep_repo(pattern: str) -> str:
    """Search file contents in the repo checkout (read-only grep).

    If a long pattern has no matches, retry with a shorter
    distinctive fragment.

    Args:
        pattern: Literal search text.
    """
    return s5tools.grep_repo(_checkout, pattern)


@tool
def list_dir(path: str = "") -> str:
    """List files in a repo checkout directory (read-only).

    Args:
        path: Repo-relative directory, '' for the checkout root.
    """
    return s5tools.list_dir(_checkout, path or "")


def _decode_history(raw):
    try:
        items = json.loads(cli.b64decode(raw))
    except Exception:
        return None
    if not isinstance(items, list) or len(items) > MAX_HISTORY:
        return None
    messages = []
    for item in items:
        if not isinstance(item, dict):
            return None
        role, content = item.get("role"), item.get("content")
        if not isinstance(content, str) or not content or len(content) > 2000:
            return None
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            messages.append(AIMessage(content=content))
        else:
            return None
    return messages


def _salvage_messages(prompt, history, trace, limit=SALVAGE_TOOL_TAIL):
    """Final-answer message list after a tool-loop recursion cutoff.

    Pure (no LLM): the original question plus the most recent tool
    results, with an instruction to answer from gathered evidence and
    make no more tool calls.
    """
    tail = [m for m in trace[-limit:]
            if m.__class__.__name__ == "ToolMessage"]
    if not tail:
        tail = trace[-limit:]
    return history + [HumanMessage(content=prompt)] + tail + [HumanMessage(
        content="Answer the original question now using only the tool "
        "results above. Do not make any more tool calls.")]


def _invoke_with_failover(model, spec, messages):
    """model.invoke with one local retry on retryable cloud errors.

    Returns (answer, failover): failover is None when the primary
    leg served, else {"from", "to", "error"} recording which legs
    ran. Local-primary calls never enter the helper (single hop, no
    ping-pong); local-unreachable surfaces the ORIGINAL cloud error.
    The local leg reuses deep_chat.build_model (same ChatOllama
    construction as this slice's own ollama branch).
    """
    failover = {}

    def _note(exc):
        failover["error"] = _short_err(exc)
        print(f"model: {spec['provider']} {_short_err(exc)}"
              " — retrying locally", file=sys.stderr)

    def _make_local():
        local_model, local_spec = deep_chat.build_model(prefer="ollama")
        failover["from"] = f"{spec['provider']}:{spec['model']}"
        failover["to"] = (f"{local_spec['provider']}:"
                          f"{local_spec['model']}")
        return lambda: local_model.invoke(messages)

    answer = model_router.with_local_fallback(
        lambda: model.invoke(messages), _make_local, _note,
        primary_is_local=(spec.get("provider") == "ollama"))
    return answer, (failover or None)


def main() -> int:
    if len(sys.argv) < 4 or not s5tools.valid_repo(sys.argv[1]):
        return cli.usage_error(
            "usage: chat_agent.py owner/repo <session-id> <b64-prompt>"
            " [b64-history]")
    if not SESSION_RE.match(sys.argv[2]):
        return cli.usage_error("bad session-id")
    repo, session_id = sys.argv[1], sys.argv[2]
    global _checkout
    try:
        _checkout = s5tools.checkout_for(repo)
    except ValueError:
        return cli.usage_error(f"bad repo: {repo!r}")
    prompt = cli.b64decode(sys.argv[3])
    if prompt is None:
        return cli.usage_error("bad base64 prompt")
    if not prompt.strip() or len(prompt) > 3000:
        return cli.usage_error("bad prompt")
    history = []
    if len(sys.argv) > 4:
        history = _decode_history(sys.argv[4])
        if history is None:
            return cli.usage_error("bad history")

    started = time.time()
    # Initialized ahead of the run so failure paths can record partial
    # progress (streamed text, tool trace, usage) instead of nothing.
    text_parts = []
    trace_messages = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    model = None
    try:
        spec = model_router.resolve_llm()
    except (RuntimeError, ValueError) as exc:
        return cli.sorry(
            f"Sorry, no model is available right now: {_short_err(exc)}")
    print(f"chat via {spec['provider']}:{spec['model']}", file=sys.stderr)
    kwargs = model_router.chat_kwargs(spec)
    # No-think is the production setting: on this CPU-only box a qwen3
    # thinking trace takes 15+ min per prompt. CHAT_THINK=1 re-enables.
    think = os.environ.get("CHAT_THINK", "") == "1"
    try:
        if spec["provider"] == "ollama":
            from langchain_ollama import ChatOllama

            if not think:
                kwargs["model_kwargs"] = {"think": False}
            model = ChatOllama(**kwargs)
        else:
            from langchain_openai import ChatOpenAI

            model = ChatOpenAI(api_key=os.environ["NEBIUS_API_KEY"], **kwargs)
        agent = create_agent(
            model=model,
            tools=[list_dir, grep_repo, read_repo_file],
            system_prompt=f"Repo: {repo}. " + SYSTEM,
        )
        messages = history + [HumanMessage(content=prompt)]
        stream = agent.stream(
            {"messages": messages},
            config={"recursion_limit": 12,
                    "callbacks": tracing.tracing_callbacks()},
            stream_mode=["messages", "updates"],
        )
        for mode, payload in stream:
            if mode == "messages":
                chunk = payload[0]
                if isinstance(chunk, AIMessageChunk):
                    meta = getattr(chunk, "usage_metadata", None) or {}
                    for k in usage:
                        usage[k] += meta.get(k, 0) or 0
                    if isinstance(chunk.content, str) and chunk.content:
                        print(chunk.content, end="", flush=True)
                        text_parts.append(chunk.content)
            elif mode == "updates":
                for update in payload.values():
                    if not isinstance(update, dict):
                        continue
                    for m in update.get("messages", []):
                        if m.__class__.__name__ == "ToolMessage" or getattr(
                            m, "tool_calls", None
                        ):
                            trace_messages.append(m)
        print("", flush=True)
        reply = "".join(text_parts).strip()
    except Exception as exc:
        if isinstance(exc, GraphRecursionError) and model is not None:
            # Tool-loop cutoff with findings in hand: one final no-tools
            # pass answers from the gathered evidence instead of dying.
            salvaged = ""
            failover = None
            try:
                answer, failover = _invoke_with_failover(
                    model, spec,
                    _salvage_messages(prompt, history, trace_messages))
                salvaged = (answer.content or "").strip()
                meta = getattr(answer, "usage_metadata", None) or {}
                for k in usage:
                    usage[k] += meta.get(k, 0) or 0
            except Exception:
                salvaged = ""
            if salvaged:
                print(salvaged, flush=True)
                reply = salvaged
                ledger.record(
                    "chat_agent.py",
                    f"{spec['provider']}:{spec['model']}",
                    session_id,
                    messages + trace_messages
                    + [AIMessage(content=reply)],
                    started,
                    extra={"repo": repo, "salvaged": True,
                           "prompt": prompt,
                           **({"failover": failover}
                              if failover else {})},
                    usage=usage,
                )
                return 0
        reply = f"Sorry, the assistant failed: {_short_err(exc)}"
        partial = "".join(text_parts).strip()
        messages = history + [HumanMessage(content=prompt)]
        ledger.record(
            "chat_agent.py", f"{spec['provider']}:{spec['model']}",
            session_id,
            messages + trace_messages
            + [AIMessage(content=partial or reply)],
            started,
            extra={"repo": repo, "error": str(exc)[:200],
                   "prompt": prompt},
            usage=usage,
        )
        return cli.sorry(reply)
    ledger.record(
        "chat_agent.py", f"{spec['provider']}:{spec['model']}",
        session_id, messages + trace_messages + [AIMessage(content=reply)],
        started, extra={"repo": repo, "prompt": prompt}, usage=usage,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
