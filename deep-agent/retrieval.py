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

import hashlib
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


def index_chunks():
    """Embedding row count in the watched sqlite, or None. Stdlib-only.

    Ground truth for "servable": a non-empty sqlite can still hold a
    stale thin build (issue #1119). Counts every row in the embeddings
    table (this index is single-purpose); any failure — missing file,
    unreadable schema, locked db — is None, never a raise, so /health
    stays total.
    """
    try:
        import sqlite3
        from urllib.request import pathname2url

        path = os.path.join(chroma_dir(), SQLITE_NAME)
        if not os.path.isfile(path):
            return None
        with sqlite3.connect("file:%s?mode=ro" % pathname2url(path), uri=True) as con:
            row = con.execute("SELECT COUNT(*) FROM embeddings").fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


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


RAG_INDEX_DIRNAME = ".td_rag"
RAG_MANIFEST_NAME = "s5_source_manifest.json"
RAG_REINDEX_TIMEOUT = 300

SKILLS_RELDIR = os.path.join("deep-agent", "skills")
DOCS_RELDIR = "docs"


def manifest_sources(repo_path):
    """Repo-relative source files the freshness manifest covers.

    Skill markdown plus docs markdown, sorted. Bounded on purpose:
    these are the hand-edited sources whose stale chunks caused real
    confusion; the td incremental indexer diffs the rest by git SHA.
    """
    rels = []
    for reldir in (SKILLS_RELDIR, DOCS_RELDIR):
        top = os.path.join(str(repo_path), reldir)
        if not os.path.isdir(top):
            continue
        for root, _dirs, files in os.walk(top):
            for name in files:
                if not name.endswith(".md"):
                    continue
                full = os.path.join(root, name)
                rels.append(os.path.relpath(full, str(repo_path)))
    return sorted(rels)


def _hash_file(path):
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def source_manifest(repo_path):
    """{relpath: sha256} over manifest sources (None when unreadable)."""
    manifest = {}
    for rel in manifest_sources(repo_path):
        manifest[rel] = _hash_file(os.path.join(str(repo_path), rel))
    return manifest


def manifest_path(repo_path):
    """Manifest file, co-located with the td index it guards."""
    return os.path.join(str(repo_path), RAG_INDEX_DIRNAME, RAG_MANIFEST_NAME)


def read_manifest(repo_path):
    """Stored manifest dict, or None when missing/corrupt."""
    import json as _json

    try:
        with open(manifest_path(repo_path), encoding="utf-8") as handle:
            payload = _json.load(handle)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_manifest(repo_path, manifest):
    """Atomically persist a manifest (tmp file + os.replace)."""
    import json as _json

    path = manifest_path(repo_path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        _json.dump(manifest, handle, sort_keys=True)
    os.replace(tmp, path)


def reindex_repo(repo_path, timeout=RAG_REINDEX_TIMEOUT):
    """Run ``td rag index`` for a checkout. Raises RuntimeError on failure.

    Success is the process exit code (rc 0); the JSON report is best
    effort. Mirrors query_td_repo's error style.
    """
    import subprocess as _sp

    try:
        proc = _sp.run(
            ["td", "rag", "index", "--repo-root", str(repo_path), "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (_sp.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"td rag index failed for {repo_path}: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"td rag index exited {proc.returncode} for {repo_path}: "
            f"{((proc.stderr or proc.stdout) or '')[:300]}"
        )
    return True


def ensure_fresh_index(repo_path, timeout=RAG_REINDEX_TIMEOUT):
    """Reindex when the manifest mismatches; True when reindexed.

    Missing/corrupt manifest counts as stale (fail-closed first run).
    A failed reindex raises, so a stale index is never served silent.
    """
    current = source_manifest(repo_path)
    if read_manifest(repo_path) == current:
        return False
    reindex_repo(repo_path, timeout=timeout)
    write_manifest(repo_path, current)
    return True


def query_td_repo(query, repo_path, top_k=8, timeout=120):
    """Query the td-managed vector index for a checkout.

    Shells to ``td rag query --json`` (boomtick capability); returns
    the raw hit list ``[{content, filepath, start_line, score}]``.
    Validates manifest freshness first (#1214): a hash mismatch
    reindexes before serving hits, so edits never read stale. Raises
    RuntimeError when td is missing, errors, or returns non-JSON.
    This is the agent retrieval path.
    """
    import json as _json
    import subprocess as _sp

    ensure_fresh_index(repo_path)
    proc = _sp.run(
        [
            "td",
            "rag",
            "query",
            "--repo-root",
            str(repo_path),
            "--query",
            query,
            "--top-k",
            str(top_k),
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    try:
        payload = _json.loads(out) if out else {}
    except ValueError as exc:
        raise RuntimeError(
            f"td rag query returned non-JSON output: {out[:200]}"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"td rag query exited {proc.returncode}: {(proc.stderr or out)[:300]}"
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
        raise RuntimeError(f"td rag query returned unexpected payload: {out[:200]}")
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
                f"{(hit.get('content') or '')[:700]}"
            )
        except Exception:
            continue
    return "\n\n".join(parts)[:2800] or "no doc hits"
