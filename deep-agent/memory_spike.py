"""Memory spike: prove cross-thread file persistence via StoreBackend."""

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langchain_ollama import ChatOllama
from langgraph.store.memory import InMemoryStore

store = InMemoryStore()
# NOTE: installed deepagents is 0.7+, where backend factories were
# removed — pass initialized instances (the skill still shows lambdas).
backend = CompositeBackend(
    default=StateBackend(),
    routes={"/memories/": StoreBackend(namespace=lambda rt: ("memories",))},
)


def make_agent():
    return create_deep_agent(
        model=ChatOllama(
            model="qwen3:8b", base_url="http://ollama:11434", temperature=0
        ),
        backend=backend,
        store=store,
        system_prompt=(
            "Follow file instructions exactly. Use only the write_file "
            "and read_file tools."
        ),
    )


def main() -> None:
    agent = make_agent()
    agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Write exactly 'MEMO-Blue-42' to "
                    "/memories/triage-note.txt using write_file, then "
                    "reply DONE.",
                }
            ]
        },
        config={"configurable": {"thread_id": "mem-write-1"}},
    )
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Read /memories/triage-note.txt and reply "
                    "with its exact content.",
                }
            ]
        },
        config={"configurable": {"thread_id": "mem-read-2"}},
    )
    final = result["messages"][-1].content
    print("FINAL:", final)
    assert "MEMO-Blue-42" in final, "cross-thread read failed"
    print("MEMORY_OK")


if __name__ == "__main__":
    main()
