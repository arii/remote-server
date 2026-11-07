"""Shared RAG helpers for S5 agent slices (deep-agent/). Stdlib-only.

The one indexer is ``td rag``: query_td_repo()/search_td() shell to
``td rag query`` and never raise (failures degrade to an explanatory
string). Live Chroma users (ask_investigator) consult the readiness
helpers: marker files in the index dir carry the build state —
.index_building while a build runs, .index_failed when the last
build failed. index_status() is stdlib-only so /health and every
query path can consult it without the venv; require_ready() raises
IndexNotReadyError instead of serving empty-silent results.
"""

import os
import re

import cli

DB_DIR = os.path.join(cli.repo_root(), "deep-agent", "chroma_db")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

BUILDING_MARKER = ".index_building"
FAILED_MARKER = ".index_failed"
SQLITE_NAME = "chroma.sqlite3"

WARMING_MESSAGE = "RAG index is warming, retry shortly"
FAILED_MESSAGE = "RAG index build failed; doc search unavailable"
MISSING_MESSAGE = "RAG index is missing; rebuild it (deep-agent/build_index.py)"


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


def normalize_repo(repo):
    """Full owner/name for a repo filter, or None when unfiltered.

    Blank/None means all repos, ranked mixed. A bare checkout name
    gains the arii/ owner prefix (index metadata is always the full
    owner/name). Raises ValueError on malformed input. Stdlib-only
    so every search_docs tool shares the one normalization.
    """
    if repo is None:
        return None
    if not isinstance(repo, str):
        raise ValueError(f"bad repo filter: {repo!r}")
    text = repo.strip()
    if not text:
        return None
    if "/" not in text:
        text = f"arii/{text}"
    if not REPO_RE.match(text):
        raise ValueError(f"bad repo filter: {repo!r}")
    return text


def query_td_repo(query, repo_path, top_k=8, timeout=120):
    """Query the td-managed vector index for a checkout.

    Shells to ``td rag query --json`` (boomtick capability); returns
    the raw hit list ``[{content, filepath, start_line, score}]``.
    Raises RuntimeError when td is missing, errors, or returns
    non-JSON. This is the agent retrieval path.
    """
    import json as _json
    import subprocess as _sp

    proc = _sp.run(
        ["td", "rag", "query", "--repo-root", str(repo_path),
         "--query", query, "--top-k", str(top_k), "--json"],
        capture_output=True, text=True, timeout=timeout)
    out = (proc.stdout or "").strip()
    try:
        payload = _json.loads(out) if out else {}
    except ValueError as exc:
        raise RuntimeError(
            f"td rag query returned non-JSON output: {out[:200]}"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"td rag query exited {proc.returncode}: "
            f"{(proc.stderr or out)[:300]}")
    if not isinstance(payload, dict) or not isinstance(
            payload.get("hits"), list):
        raise RuntimeError(
            f"td rag query returned unexpected payload: {out[:200]}")
    return payload["hits"]


def search_td(query, repo_path, top_k=8):
    """td-backed doc search for tool output.

    Returns '[filepath:line] excerpt' lines, or 'no doc hits'.
    Never raises: td failures degrade to an explanatory string.
    """
    try:
        hits = query_td_repo(query, repo_path, top_k=top_k)
    except Exception as exc:
        return f"doc search unavailable: {exc}"
    parts = []
    for hit in hits or []:
        try:
            parts.append(
                f"[{hit.get('filepath')}:{hit.get('start_line')}] "
                f"{(hit.get('content') or '')[:700]}")
        except Exception:
            continue
    return "\n\n".join(parts)[:2800] or "no doc hits"
