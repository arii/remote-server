"""Shared RAG retriever factory for S5 agent slices (deep-agent/).

Venv-only (imports langchain_chroma): system-python tests cannot import
this; run its tests under deep-agent/.venv/bin/python like
test_chat_agent.py. New slices: use get_retriever() instead of
constructing Chroma + OllamaEmbeddings inline. Existing call sites
(search_docs.py, ask_investigator.py, triage_explainer.py) predate this
module and still work — adopt when touching them for other reasons,
not in a drive-by refactor.

Index and query sides MUST share one embedding configuration
(langchain-rag fix-consistent-embeddings): build_index.py builds the
index through get_embeddings()/chroma_dir()/collection_name() so an
S5_EMBED_MODEL / OLLAMA_URL override can never silently fork the
index and query embeddings.

Readiness (#699): the buttons entrypoint builds the index in the
background while the server already serves. Marker files in the
index dir carry the build state — .index_building while a build
runs, .index_failed when the last build failed. index_status() is
stdlib-only so /health and every query path can consult it without
the venv; get_retriever() refuses a non-ready index with
IndexNotReadyError instead of serving empty-silent results.
"""

import os

import cli

DB_DIR = os.path.join(cli.repo_root(), "deep-agent", "chroma_db")
COLLECTION = "s5-docs"
EMBED_MODEL = "nomic-embed-text"
DEFAULT_URL = "http://ollama:11434"

BUILDING_MARKER = ".index_building"
FAILED_MARKER = ".index_failed"
SQLITE_NAME = "chroma.sqlite3"

WARMING_MESSAGE = "RAG index is warming, retry shortly"
FAILED_MESSAGE = "RAG index build failed; doc search unavailable"
MISSING_MESSAGE = "RAG index is missing; rebuild it (deep-agent/build_index.py)"

_retrievers = {}


class IndexNotReadyError(RuntimeError):
    """The RAG index cannot serve queries yet (warming/failed/missing)."""


def index_status():
    """'ready' | 'warming' | 'failed' | 'missing'. Stdlib-only.

    A live build marker always wins (the on-disk sqlite may be a
    partial in-progress write). A failed marker only degrades when no
    usable sqlite survives; a stale failure beside a good index (e.g.
    operator-rebuilt) still serves.
    """
    index_dir = chroma_dir()
    usable = (
        os.path.isfile(os.path.join(index_dir, SQLITE_NAME))
        and os.path.getsize(os.path.join(index_dir, SQLITE_NAME)) > 0
    )
    if os.path.isfile(os.path.join(index_dir, BUILDING_MARKER)):
        return "warming"
    if os.path.isfile(os.path.join(index_dir, FAILED_MARKER)) and not usable:
        return "failed"
    if usable:
        return "ready"
    return "missing"


def require_ready():
    """Raise IndexNotReadyError with the degraded message unless ready."""
    state = index_status()
    if state == "ready":
        return
    if state == "warming":
        raise IndexNotReadyError(WARMING_MESSAGE)
    if state == "failed":
        raise IndexNotReadyError(FAILED_MESSAGE)
    raise IndexNotReadyError(MISSING_MESSAGE)


def _env(name, default):
    val = os.environ.get(name, default)
    if not isinstance(val, str) or not val.strip():
        return default
    return val


def chroma_dir():
    """Persist directory for the S5 docs index (S5_CHROMA_DIR override)."""
    return _env("S5_CHROMA_DIR", DB_DIR)


def collection_name():
    """Chroma collection for the S5 docs index (S5_COLLECTION override)."""
    return _env("S5_COLLECTION", COLLECTION)


def get_embeddings():
    """Shared Ollama embeddings for index AND query sides."""
    from langchain_ollama import OllamaEmbeddings

    return OllamaEmbeddings(
        model=_env("S5_EMBED_MODEL", EMBED_MODEL),
        base_url=_env("OLLAMA_URL", DEFAULT_URL),
    )


def get_retriever(k=3):
    """Shared Chroma retriever over the S5 docs index. Cached per k.

    Raises IndexNotReadyError while the background build is warming
    (or failed/missing) instead of serving empty-silent results.
    """
    if isinstance(k, bool) or not isinstance(k, int) or k <= 0:
        raise ValueError(f"bad k: {k!r}")
    require_ready()
    if k not in _retrievers:
        from langchain_chroma import Chroma

        store = Chroma(
            persist_directory=chroma_dir(),
            embedding_function=get_embeddings(),
            collection_name=collection_name(),
        )
        _retrievers[k] = store.as_retriever(search_kwargs={"k": k})
    return _retrievers[k]


def format_hits(hits, limit=700):
    """'[repo/source] excerpt' lines for tool output. Never raises."""
    parts = []
    try:
        for hit in hits or []:
            meta = getattr(hit, "metadata", {}) or {}
            text = getattr(hit, "page_content", "") or ""
            parts.append(
                f"[{meta.get('repo')}/{meta.get('source')}] {text[:limit]}"
            )
        return "\n\n".join(parts)[: limit * 4] or "no doc hits"
    except Exception as exc:
        return f"doc search unavailable: {exc}"
