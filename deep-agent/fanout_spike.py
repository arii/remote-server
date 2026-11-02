"""Spike: per-repo fan-out via task delegation (one scout, two repos)."""

import os
import re
import subprocess
import sys
import time

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from deepagents import create_deep_agent

import ledger

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
MODEL_ID = "zai-org/GLM-5.3-Flash"
THREAD_ID = "fanout-spike-1"


@tool
def triage_counts(repo: str) -> str:
    """Get deterministic triage counts for one repository.

    Args:
        repo: Repository in owner/name format.
    """
    if not REPO_RE.match(repo):
        return "REJECTED: repo must look like owner/name"
    proc = subprocess.run(
        ["td", "agent", "suggest", "-r", repo],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return (proc.stdout + proc.stderr).strip()[:1500]


def main() -> int:
    started = time.time()
    load_dotenv("/srv/remote-server/.env")
    if not os.environ.get("NEBIUS_API_KEY"):
        print("NEBIUS_API_KEY is not set", file=sys.stderr)
        return 1
    model = ChatOpenAI(
        model=MODEL_ID,
        base_url="https://api.studio.nebius.com/v1",
        api_key=os.environ["NEBIUS_API_KEY"],
        temperature=0,
        max_tokens=2000,
    )
    agent = create_deep_agent(
        model=model,
        system_prompt=(
            "You coordinate per-repo triage scouts. Delegate with the "
            "task tool: one call for arii/tech-dancer, one for "
            "arii/boomtick, each instructing the scout to return the "
            "raw triage_counts output plus one line naming the top "
            "action. Subagents are stateless: put complete instructions "
            "in each call. Then merge both reports into at most 4 lines."
        ),
        subagents=[
            {
                "name": "repo_scout",
                "description": "Gather triage counts for exactly one repo",
                "system_prompt": "Call triage_counts once for the repo "
                "named in your instructions. Return its raw output, then "
                "one line TOP ACTION: <the single next action>.",
                "tools": [triage_counts],
            }
        ],
        checkpointer=MemorySaver(),
    )
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Triage arii/tech-dancer and arii/boomtick.",
                }
            ]
        },
        config={"configurable": {"thread_id": THREAD_ID}},
    )
    tasks = sum(
        1
        for m in result["messages"]
        for tc in (getattr(m, "tool_calls", []) or [])
        if tc.get("name") == "task"
    )
    ledger.record(
        "fanout_spike.py",
        f"nebius:{MODEL_ID}",
        THREAD_ID,
        result["messages"],
        started,
        extra={"task_delegations": tasks},
    )
    print(f"task_delegations: {tasks}")
    print("FINAL:", result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
