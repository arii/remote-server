"""Minimal Deep Agents harness check: one stub tool, one Nebius call."""

import os
import sys

from dotenv import load_dotenv
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from deepagents import create_deep_agent


@tool
def get_stack_name(prompt: str) -> str:
    """Return the name of this home-server stack. Ignores its input."""
    return "TRIGKEY S5 remote-server"


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
        max_tokens=300,
    )
    agent = create_deep_agent(
        model=model,
        tools=[get_stack_name],
        system_prompt=(
            "You are a harness check. Use the get_stack_name tool once, "
            "then reply with its exact result and nothing else."
        ),
    )
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "What stack is this?"}]},
        config={"configurable": {"thread_id": "harness-check-1"}},
    )
    print(result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
