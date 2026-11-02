"""Spike: mid-run user question via interrupt() inside a tool (local 8b)."""

from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command, interrupt


@tool
def ask_user(question: str) -> str:
    """Ask the user a question and wait for their answer.

    Use this when you need a decision only the user can make.

    Args:
        question: The exact question to present.
    """
    answer = interrupt({"question": question})
    return str(answer)


def main() -> None:
    agent = create_agent(
        model=ChatOllama(
            model="qwen3:8b", base_url="http://ollama:11434", temperature=0
        ),
        tools=[ask_user],
        checkpointer=MemorySaver(),
        system_prompt=(
            "Call ask_user once to confirm the deploy window, then "
            "reply 'NOTED: <their answer>'. Use the function-calling "
            "interface; never write a tool call out as JSON text."
        ),
    )
    config = {"configurable": {"thread_id": "ask-spike-1"}}
    first = agent.invoke(
        {
            "messages": [
                {"role": "user", "content": "Confirm the deploy window."}
            ]
        },
        config=config,
    )
    if "__interrupt__" not in first:
        print("NO INTERRUPT")
        for m in first["messages"]:
            print("-", type(m).__name__, str(m.content)[:200])
        return
    print("QUESTION:", first["__interrupt__"][0].value)
    resumed = agent.invoke(
        Command(resume="Saturday 6am"), config=config
    )
    print("FINAL:", resumed["messages"][-1].content)


if __name__ == "__main__":
    main()
