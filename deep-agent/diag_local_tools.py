"""Diagnose local tool calling: native vs OpenAI-compat route, 7b vs 8b."""

from langchain_core.tools import tool
from langchain_ollama import ChatOllama
from langchain_openai import ChatOpenAI


@tool
def triage_counts(repo: str) -> str:
    """Get deterministic triage counts for a repository.

    Args:
        repo: Repository in owner/name format.
    """
    return '{"open_prs": 0}'


CANDIDATES = {
    "ollama-native-7b": ChatOllama(
        model="qwen2.5-coder:7b", base_url="http://ollama:11434", temperature=0
    ),
    "ollama-native-qwen3-8b": ChatOllama(
        model="qwen3:8b", base_url="http://ollama:11434", temperature=0
    ),
    "openai-compat-7b": ChatOpenAI(
        model="qwen2.5-coder:7b",
        base_url="http://ollama:11434/v1",
        api_key="ollama",
        temperature=0,
    ),
}

for name, model in CANDIDATES.items():
    try:
        bound = model.bind_tools([triage_counts])
        msg = bound.invoke("Call triage_counts for arii/tech-dancer.")
        calls = msg.tool_calls or []
        print(f"{name}: tool_calls={len(calls)} content={str(msg.content)[:120]!r}")
    except Exception as exc:  # noqa: BLE001 - diagnostic matrix must show all rows
        print(f"{name}: ERROR {type(exc).__name__}: {str(exc)[:200]}")
