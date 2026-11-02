"""Triage explainer: minimal-context local agent for one repo.

Usage: triage_explainer.py owner/repo
Uses deterministic `td agent suggest` counts plus RAG runbook context,
explained by the local 8b model in at most 3 sentences.
"""

import sys
import time
from pathlib import Path

from langchain.agents import create_agent
from langchain_chroma import Chroma
from langchain_core.tools import tool
from langchain_ollama import ChatOllama, OllamaEmbeddings

import ledger
import triage_common

DB_DIR = str(Path(__file__).resolve().parent / "chroma_db")
MODEL_ID = "qwen3:8b"

_embeddings = OllamaEmbeddings(
    model="nomic-embed-text", base_url="http://ollama:11434"
)
_retriever = Chroma(
    persist_directory=DB_DIR,
    embedding_function=_embeddings,
    collection_name="s5-docs",
).as_retriever(search_kwargs={"k": 2})


@tool
def triage_counts(repo: str) -> str:
    """Get deterministic triage counts for a repository.

    Use this first, before answering anything about triage state.

    Args:
        repo: Repository in owner/name format, e.g. arii/tech-dancer.
    """
    return triage_common.fetch_triage_counts(repo)


@tool
def search_docs(query: str) -> str:
    """Search S5 runbooks for triage guidance.

    Use this after triage_counts if you need runbook context for the
    explanation.

    Args:
        query: Short question about triage process or commands.
    """
    hits = _retriever.invoke(query)
    parts = [
        f"[{h.metadata.get('source')}] {h.page_content[:700]}" for h in hits
    ]
    return "\n\n".join(parts)[:1600]


def main() -> int:
    if len(sys.argv) < 2 or not triage_common.valid_repo(sys.argv[1]):
        print("usage: triage_explainer.py owner/repo", file=sys.stderr)
        return 2
    repo = sys.argv[1]
    started = time.time()
    # qwen3:8b emits valid tool calls; qwen2.5-coder:7b narrates them
    # as JSON text under LangChain, so 8b is the proven floor.
    model = ChatOllama(
        model=MODEL_ID,
        base_url="http://ollama:11434",
        temperature=0,
    )
    agent = create_agent(
        model=model,
        tools=[triage_counts, search_docs],
        system_prompt=(
            "You are a triage explainer. Call the triage_counts tool "
            "once for the requested repo, optionally call search_docs "
            "once for runbook context, then reply in at most 3 "
            "sentences: state the counts, then name the single next "
            "action. Always invoke tools through the function-calling "
            "interface. Never write a tool call out as JSON text."
        ),
    )
    thread_id = "explainer-" + repo.replace("/", "-")
    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": f"Explain the triage state of {repo} "
                    "and the single next action.",
                }
            ]
        },
        config={"recursion_limit": 10},
    )
    calls = triage_common.count_tool_calls(result["messages"])
    ledger.record(
        "triage_explainer.py",
        f"ollama:{MODEL_ID}",
        thread_id,
        result["messages"],
        started,
    )
    print(f"tool_calls_made: {calls}")
    print("FINAL:", result["messages"][-1].content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
