"""Spike: suggest-next-actions shape with HITL approval (dry-run tools).

NOTE (consolidated): SUGGEST-generation here is SUPERSEDED by
`td agent suggest -r` (deterministic, verified working). This file
stays only as the proven HITL interrupt/approve/resume pattern.

Original shape: compact briefing context in, one validated td command
proposed, human approval gate, dry-run execution. No real td
execution, no side effects. Verb list grounded in
`td --help` output (2026-11-02); read-only verbs only.
"""

import os
import shlex
import sys

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from deepagents import create_deep_agent

# Read-only investigation verbs only. Mutating verbs (fix-ci dispatches a
# Jules session; create-*/merge-*/apply-patch write) are deliberately
# excluded from this spike — they need side-effect approval design.
ALLOWED = {
    ("agent", "list-sessions"),
    ("agent", "run-feedback-check"),
    ("agent", "get-session"),
    ("agent", "messages"),
    ("repo", "ci-logs"),
    ("repo", "workflow-runs"),
    ("repo", "logs"),
    ("gh", "issue-view"),
    ("gh", "list-pr-commits"),
    ("gh", "detect-conflicts"),
    (None, "preflight"),
    (None, "doctor"),
    (None, "context-warm"),
}

VERBLIST = "\n".join(
    sorted(
        f"td {g} {v}" if g else f"td {v}"
        for g, v in ALLOWED
    )
)

FIXTURE_BRIEFING = """Repo: arii/tech-dancer (fixture)
Attention: 1 PR with failing CI.
PR #4827 "Fix wasm deploy" — CI: failing (Lighthouse), merge: clean, updated 2h ago.
Failing runs: Lighthouse on fix-branch (1 failed job: build).
Jules sessions: 0 active.
"""


@tool
def run_td_command(command: str) -> str:
    """Validate a td command against the allowlist, then dry-run it.

    Input must be a single command starting with 'td '. Never executes.
    """
    if any(ch in command for ch in ";|&$()`"):
        return "REJECTED: shell metacharacters are not allowed"
    try:
        parts = shlex.split(command)
    except ValueError:
        return "REJECTED: unparseable command"
    if len(parts) < 2 or parts[0] != "td":
        return f"REJECTED: not a td command: {command!r}"
    if "--force" in parts:
        return "REJECTED: --force is never allowed"
    if len(parts) >= 3 and (parts[1], parts[2]) in ALLOWED:
        return f"WOULD RUN: {command}"
    if (None, parts[1]) in ALLOWED:
        return f"WOULD RUN: {command}"
    return f"REJECTED: not an allowlisted td command: {command!r}"


def main() -> int:
    load_dotenv("/srv/remote-server/.env")
    if not os.environ.get("NEBIUS_API_KEY"):
        print("NEBIUS_API_KEY is not set", file=sys.stderr)
        return 1
    model = ChatOpenAI(
        model="zai-org/GLM-5.3-Flash",
        base_url="https://api.studio.nebius.com/v1",
        api_key=os.environ["NEBIUS_API_KEY"],
        temperature=0,
        # Reasoning model: needs headroom for hidden reasoning tokens
        # plus the visible reply + tool call.
        max_tokens=2000,
    )
    agent = create_deep_agent(
        model=model,
        tools=[run_td_command],
        system_prompt=(
            "You triage CI failures. You MUST call the run_td_command tool "
            "exactly once, using one of these exact commands (you may append "
            " ordinary PR/repo flags like --pr 4827):\n"
            + VERBLIST
            + "\nFirst reply with exactly one line 'SUGGEST: <td command> :: "
            "<reason>'. Then call run_td_command with that exact command. "
            "Never finish without calling the tool."
        ),
        interrupt_on={"run_td_command": True},
        checkpointer=MemorySaver(),
    )
    config = {"configurable": {"thread_id": "suggest-spike-2"}}
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Given this briefing, suggest exactly one td "
                        "command to investigate the failing CI:\n"
                        + FIXTURE_BRIEFING
                    ),
                }
            ]
        },
        config=config,
    )
    if "__interrupt__" not in result:
        print("NO INTERRUPT: approval gate did not trigger", file=sys.stderr)
        return 1
    interrupt = result["__interrupt__"][0]
    print("INTERRUPT value:", str(interrupt.value)[:300])
    resumed = agent.invoke(
        Command(resume={"decisions": [{"type": "approve"}]}),
        config=config,
    )
    print("FINAL:", resumed["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
