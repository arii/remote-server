"""Per-repo triage fan-out: orchestrator + one scout subagent per repo.

Usage: triage_fanout.py [owner/repo ...]
Defaults to arii/tech-dancer + arii/boomtick. Each scout is stateless:
complete instructions go in its single task call.
"""

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
MODEL_ID = os.environ.get("NEBIUS_MODEL", "nvidia/Nemotron-3_5-Lightning")
DEFAULT_REPOS = ["arii/tech-dancer", "arii/boomtick"]


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
    repos = sys.argv[1:] or DEFAULT_REPOS
    bad = [r for r in repos if not REPO_RE.match(r)]
    if bad:
        print(f"bad repo(s): {bad}", file=sys.stderr)
        return 2
    model = ChatOpenAI(
        model=MODEL_ID,
        base_url="https://api.studio.nebius.com/v1",
        api_key=os.environ["NEBIUS_API_KEY"],
        temperature=0,
        max_tokens=2000,
    )
    repo_list = ", ".join(repos)
    agent = create_deep_agent(
        model=model,
        system_prompt=(
            "You coordinate per-repo triage scouts. Delegate with the "
            f"task tool: one call each for {repo_list}, each "
            "instructing the scout to return the raw triage_counts "
            "output plus one line naming the top action. Subagents are "
            "stateless: put complete instructions in each call. Then "
            "merge the reports into at most 2 lines per repo."
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
                {"role": "user", "content": f"Triage {repo_list}."}
            ]
        },
        config={"configurable": {"thread_id": "fanout-run"}},
    )
    tasks = sum(
        1
        for m in result["messages"]
        for tc in (getattr(m, "tool_calls", []) or [])
        if tc.get("name") == "task"
    )
    ledger.record(
        "triage_fanout.py",
        f"nebius:{MODEL_ID}",
        "fanout-run",
        result["messages"],
        started,
        extra={"task_delegations": tasks, "repos": repos},
    )
    print(f"task_delegations: {tasks}")
    print("FINAL:", result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
