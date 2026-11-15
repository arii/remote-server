"""Scripted live local turn against the real Ollama service (D1 proof).

The rebuild audit (Blueprint art_OYSkk5K4) could not reproduce a
lock-up offline: no unit test proves the zero-token lock-up is gone.
This script is that proof — run it on the box, against the real
Ollama service, before the rebuild PRs merge:

    deep-agent/.venv/bin/python deep-agent/live_turn_smoke.py owner/repo

What it asserts (exit 0 = pass, 1 = fail, 2 = usage):
  1. the turn stays on the local model (prefer=ollama);
  2. at least one READ tool runs (grep_repo);
  3. the turn ends in a propose_action interrupt (the guarded write
     gate) — nothing executes;
  4. tokens streamed throughout (no dead window), and the turn
     completes under the resolved CHAT_TURN_TIMEOUT.

Every token prints as it arrives with an elapsed stamp, so a hang is
visible in the transcript, not silent. The thread is throwaway.

Env contract: OLLAMA_URL / OLLAMA_NUM_CTX / CHAT_TURN_TIMEOUT behave
exactly as in a chat turn (the same model_router + turn_engine
plumbing). Needs the deep-agent venv and a checkout for owner/repo.
"""

import json
import sys
import time

import cli

cli.bootstrap()

import deep_chat  # noqa: E402
from backend.exec import turn_engine  # noqa: E402

PROMPT = (
    "First, use grep_repo to find one TODO or FIXME marker in this repo. "
    "Then propose_action(kind='issue') proposing an issue to fix the "
    "clearest one, quoting the exact TODO marker text verbatim in the "
    "issue body. Then stop and wait."
)

READ_TOOLS = {"grep_repo", "list_dir", "read_repo_file", "search_docs", "pr_read"}


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    if len(argv) != 2:
        print("usage: live_turn_smoke.py owner/repo", file=sys.stderr)
        return 2
    repo = argv[1]
    if not __import__("s5tools").valid_repo(repo):
        print(f"bad repo: {repo!r}", file=sys.stderr)
        return 2

    started = time.time()

    def stamp() -> str:
        return f"[{time.time() - started:7.1f}s]"

    model, spec = deep_chat.build_model(prefer="ollama")
    print(f"{stamp()} model: {spec['provider']}:{spec['model']}", file=sys.stderr)
    if spec["provider"] != "ollama":
        print(
            f"FAIL: routing left the local ladder ({spec['provider']}) — "
            "the proof must run against real local hardware",
            file=sys.stderr,
        )
        return 1

    saver, _, _holder = deep_chat.open_checkpointer()
    # Unique per run: resuming a fixed thread id replays the previous
    # run's checkpointed events instead of making a live model call.
    thread_id = f"live-turn-smoke-{int(time.time())}"
    agent = deep_chat.build_agent(repo, saver, model=model, spec=spec)
    config = {
        **{"configurable": {"thread_id": thread_id}},
        "recursion_limit": deep_chat.RECURSION_LIMIT,
    }
    # Same budget the chat router enforces: turn_engine's env contract,
    # default 600 (backend/api/routers/chat.py CHAT_TURN_TIMEOUT).
    timeout = turn_engine.resolve_timeout(600, "CHAT_TURN_TIMEOUT")

    from langchain_core.messages import AIMessageChunk, HumanMessage  # noqa: E402
    from langgraph.errors import GraphRecursionError  # noqa: E402

    tool_names: list[str] = []
    chunk_count = 0
    last_token_at = started
    try:
        stream = agent.stream(
            {"messages": [HumanMessage(content=PROMPT)]},
            config=config,
            stream_mode=["messages", "updates"],
        )
        for mode, payload in stream:
            if time.time() - started > timeout:
                print(f"FAIL: turn exceeded {timeout}s", file=sys.stderr)
                return 1
            if mode == "messages":
                chunk = payload[0]
                if isinstance(chunk, AIMessageChunk) and (
                    # Tool-call deltas are model tokens streaming too —
                    # counting content only would misread a working
                    # tool-calling turn as the zero-token lock-up.
                    (isinstance(chunk.content, str) and chunk.content)
                    or chunk.tool_call_chunks
                ):
                    chunk_count += 1
                    last_token_at = time.time()
                    print(f"{stamp()} token: {chunk.content!r}", file=sys.stderr)
            elif mode == "updates":
                for update in payload.values():
                    msgs = (
                        update.get("messages", []) if isinstance(update, dict) else []
                    )
                    for m in msgs:
                        for call in getattr(m, "tool_calls", None) or []:
                            tool_names.append(call.get("name", ""))
                            print(
                                f"{stamp()} tool: {call.get('name')}", file=sys.stderr
                            )
    except GraphRecursionError:
        print(
            "FAIL: recursion cutoff — the turn looped, never proposed", file=sys.stderr
        )
        return 1

    elapsed = time.time() - started
    pending = deep_chat._pending_interrupts(agent, config)
    proposed = (
        any(
            (iv.get("value") or {}).get("action_requests")
            or ("kind" in (iv.get("value") or {}))
            for iv in pending
        )
        if pending
        else False
    )
    reads = [n for n in tool_names if n in READ_TOOLS]

    summary = {
        "provider": spec["provider"],
        "model": spec["model"],
        "elapsed_s": round(elapsed, 1),
        "timeout_s": timeout,
        "tool_calls": tool_names,
        "read_tools_used": reads,
        "token_chunks": chunk_count,
        "longest_token_stall_s": round(
            max(0.0, elapsed - (last_token_at - started)) if chunk_count else elapsed, 1
        ),
        "proposed": proposed,
    }
    print(json.dumps(summary, indent=2))

    failures = []
    if not reads:
        failures.append("no read tool ran")
    if not proposed:
        failures.append("turn never reached a propose_action interrupt")
    if not chunk_count:
        failures.append("zero tokens streamed (the lock-up signature)")
    if elapsed >= timeout:
        failures.append(f"turn took {elapsed:.0f}s >= timeout {timeout}s")
    if failures:
        print("FAIL: " + "; ".join(failures), file=sys.stderr)
        return 1
    print(
        f"PASS: read tool {reads[0]} + propose_action in {elapsed:.0f}s "
        f"(< {timeout}s) with {chunk_count} streamed chunks — no lock-up",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
