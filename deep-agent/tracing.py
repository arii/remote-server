"""LangSmith tracing switch for deep-agent/ (roadmap item 1).

When LANGSMITH_TRACING=true and LANGSMITH_API_KEY are both set,
tracing_callbacks() returns one LangChainTracer (project from
LANGSMITH_PROJECT, server default otherwise) for agent stream configs.
Anything else — flag off, key missing, library absent — returns [],
so tracing can never break a run. Stdlib import surface: the langchain
import is guarded, and constructing the tracer performs no I/O.
"""

import os

try:
    from langchain_core.tracers.langchain import LangChainTracer
except ImportError:
    LangChainTracer = None


def tracing_callbacks():
    """[] unless tracing is fully configured; else [LangChainTracer]."""
    if os.environ.get("LANGSMITH_TRACING", "").lower() != "true":
        return []
    if not os.environ.get("LANGSMITH_API_KEY", "").strip():
        return []
    if LangChainTracer is None:
        return []
    project = os.environ.get("LANGSMITH_PROJECT", "").strip() or None
    return [LangChainTracer(project_name=project)]
