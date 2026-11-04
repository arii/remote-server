"""Build the shared S5 RAG index (Chroma, local embeddings).

Usage: build_index.py
Indexes repo docs + source code + GitHub issues/PRs into
deep-agent/chroma_db with `repo`, `kind` (doc|code|issue|pr), and
`language` metadata so per-repo agents filter to their own slice.
Purely local (Ollama nomic-embed-text); no hosted calls, except the
gh-sourced pass which shells out to `gh issue/pr list` (bodies only,
no comments, v1). Re-run after docs or code change or whenever
chroma_db is wiped. Each run clears the collection first, so re-runs
never stack stale duplicates. Embedding model/URL come from
retrieval.get_embeddings() (S5_EMBED_MODEL / OLLAMA_URL overrides);
S5_CHROMA_DIR / S5_COLLECTION override the target, S5_GH_INDEX=0
skips the GitHub pass (offline builds).
"""

import glob
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import retrieval  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_DIR = os.path.join(HERE, "chroma_db")
SOURCES = [
    "docs/*.md",
    "terminal/pi-config/skills/*/*.md",
    "terminal/pi-config/AGENTS.md",
    "terminal/pi-config/APPEND_SYSTEM.md",
    "AGENTS.md",
]

# (base dir, recursive, extensions, language) for code (#668).
# deep-agent is top-level only: no venv/traces/chroma_db descent.
CODE_SOURCES = [
    ("backend", True, (".py",), "python"),
    ("deep-agent", False, (".py",), "python"),
    ("frontend/src", True, (".ts", ".tsx"), "typescript"),
    ("core_cli", True, (".py",), "python"),
]
# Pruned before descent: the walker never enters these, and the
# pattern tests assert they contribute zero chunks.
SKIP_DIRS = frozenset({
    "tests",
    "fixtures",
    ".venv",
    "traces",
    "chroma_db",
    "__pycache__",
    "node_modules",
    "dist",
    ".git",
})


def _skip_file(name):
    """True for hidden files and *.test.* files (never indexed)."""
    return name.startswith(".") or ".test." in name


def _walk_code(root, base, recursive, extensions):
    """Sorted relpaths of indexable code files under base."""
    start = os.path.join(root, base)
    if not os.path.isdir(start):
        return []
    if not recursive:
        return sorted(
            os.path.join(base, name)
            for name in os.listdir(start)
            if name.endswith(extensions)
            and not _skip_file(name)
            and os.path.isfile(os.path.join(start, name))
        )
    out = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if not name.endswith(extensions) or _skip_file(name):
                continue
            out.append(
                os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(out)


def collect_source_files(root):
    """Sorted [(relpath, kind, language)] for docs + code under root.

    Stdlib only: system-python tests import this module (langchain
    imports live in build()). Docs keep their historical per-pattern
    order; code is appended after.
    """
    found = []
    for pattern in SOURCES:
        for path in sorted(glob.glob(os.path.join(root, pattern))):
            found.append(
                (os.path.relpath(path, root), "doc", "markdown"))
    for base, recursive, extensions, language in CODE_SOURCES:
        found.extend(
            (rel, "code", language)
            for rel in _walk_code(root, base, recursive, extensions)
        )
    return found


def doc_metadata(relpath, kind, language):
    """Chunk metadata: path attribution + kind/language tags."""
    return {
        "source": relpath,
        "repo": "remote-server",
        "kind": kind,
        "language": language,
    }


# GitHub-sourced pass (#826): issues + PRs share the same collection,
# labeled by kind. Bodies only in v1 (no comments, no live sync).
GH_REPO = "arii/remote-server"
GH_CAP = 500
GH_FIELDS = "number,title,body,state,labels,updatedAt,url"
GH_TIMEOUT = 60


def fetch_gh_items(kind, repo, limit, runner=None):
    """Raw issue/PR dicts, most-recently-updated first, capped.

    Stdlib-only (subprocess + json): system-python tests stub
    `runner` (a subprocess-like module) with canned gh stdout, so no
    network in the suite. Sort-then-cap is client-side: gh list order
    is not updatedAt order, so the cap alone would keep stale items.
    Raises RuntimeError when gh fails, ValueError on a bad kind.
    """
    if kind not in ("issue", "pr"):
        raise ValueError(f"bad gh kind: {kind!r}")
    if limit is None or limit <= 0:
        return []
    argv = [
        "gh", kind, "list",
        "-R", repo,
        "--state", "all",
        "--limit", str(int(limit)),
        "--json", GH_FIELDS,
    ]
    run = runner or subprocess
    proc = run.run(
        argv,
        capture_output=True,
        text=True,
        timeout=GH_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            (proc.stderr or "").strip().splitlines()[-1]
            if (proc.stderr or "").strip()
            else "gh command failed"
        )
    if not (proc.stdout or "").strip():
        raise RuntimeError("gh returned empty output")
    items = json.loads(proc.stdout)
    ordered = sorted(
        items,
        key=lambda item: item.get("updatedAt") or "",
        reverse=True,
    )
    return ordered[:int(limit)]


def gh_labels(item):
    """Comma-joined label names (Chroma metadata holds no lists)."""
    return ",".join(
        label.get("name", "")
        for label in item.get("labels") or []
        if label.get("name")
    )


def gh_metadata(item, kind):
    """Chunk metadata: kind/number/state/url + doc-style attribution.

    `source` carries the kind prefix (issue#N / pr#N) so the generic
    `[repo/source]` rendering in search_docs/format_hits surfaces the
    kind with no query-side change.
    """
    return {
        "source": f"{kind}#{item.get('number')}",
        "repo": "remote-server",
        "kind": kind,
        "language": "markdown",
        "number": item.get("number"),
        "state": item.get("state"),
        "url": item.get("url"),
        "labels": gh_labels(item),
    }


def gh_page_content(item, kind):
    """Indexable text: header line + labels + body."""
    lines = [
        f"{kind} #{item.get('number')}: {item.get('title') or ''}".rstrip(),
        f"State: {item.get('state')}",
    ]
    labels = gh_labels(item)
    if labels:
        lines.append(f"Labels: {labels}")
    lines.extend(["", item.get("body") or ""])
    return "\n".join(lines)


def collect_gh_documents(fetcher=None, cap=GH_CAP, repo=None):
    """[(page_content, metadata)] for issues + PRs, issues first.

    `fetcher` is fetch_gh_items by default; tests inject a stub.
    `cap` bounds EACH kind (500 issues + 500 PRs max).
    """
    fetch = fetcher or fetch_gh_items
    target = repo or GH_REPO
    out = []
    for kind in ("issue", "pr"):
        for item in fetch(kind, target, cap):
            out.append(
                (gh_page_content(item, kind), gh_metadata(item, kind)))
    return out


def _is_missing_collection_error(exc: BaseException) -> bool:
    """True only for delete_collection on a collection that isn't there.

    The fresh-index signal. chromadb raises NotFoundError on the
    local-client path and ValueError on the segment path; both say
    "does not exist" (verified against chromadb 1.5.9). Matched by
    class name so this module stays stdlib-only at import. Anything
    else (locks, permissions, corruption) must fail loud, never pass:
    from_documents below would otherwise stack onto the surviving
    collection.
    """
    if "does not exist" not in str(exc):
        return False
    if isinstance(exc, ValueError):
        return True
    return any(
        cls.__name__ == "NotFoundError" for cls in type(exc).__mro__
    )


def build(root, db_dir, collection, embeddings,
          gh_fetcher=None, gh_cap=GH_CAP):
    """Read, split, embed, upsert. Returns (n_docs, counts).

    Clears the collection first (#542: re-runs never stack stale
    duplicates). Raises RuntimeError when nothing matches, leaving
    the index untouched.

    gh_fetcher=None skips the GitHub pass (hermetic tests); main()
    passes the real fetcher. A failing gh pass warns on stderr and
    the build continues file-only — a gh auth hiccup must never nuke
    doc search. counts always carries all four kinds.
    """
    from langchain_chroma import Chroma
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    infos = collect_source_files(root)
    gh_texts = []
    if gh_fetcher is not None and gh_cap is not None and gh_cap > 0:
        try:
            gh_texts = collect_gh_documents(
                fetcher=gh_fetcher, cap=gh_cap)
        except Exception as exc:
            print(f"warning: GitHub pass skipped ({exc})",
                  file=sys.stderr)
    if not infos and not gh_texts:
        raise RuntimeError(
            "no source documents matched; index left untouched")
    docs = []
    for relpath, kind, language in infos:
        with open(os.path.join(root, relpath), encoding="utf-8") as f:
            docs.append(
                Document(
                    page_content=f.read(),
                    metadata=doc_metadata(relpath, kind, language),
                )
            )
    for page_content, metadata in gh_texts:
        docs.append(
            Document(page_content=page_content, metadata=metadata))
    splits = RecursiveCharacterTextSplitter(
        chunk_size=1000, chunk_overlap=200
    ).split_documents(docs)
    counts = {"doc": 0, "code": 0, "issue": 0, "pr": 0}
    for split in splits:
        kind = split.metadata.get("kind")
        if kind in counts:
            counts[kind] += 1
    try:
        Chroma(
            persist_directory=db_dir,
            embedding_function=embeddings,
            collection_name=collection,
        ).delete_collection()
    except Exception as exc:
        if not _is_missing_collection_error(exc):
            raise RuntimeError(
                f"refusing to index: failed to clear collection "
                f"{collection!r} ({exc})"
            ) from exc
        # Fresh index: no prior collection to clear.
    Chroma.from_documents(
        documents=splits,
        embedding=embeddings,
        persist_directory=db_dir,
        collection_name=collection,
    )
    return len(docs), counts


def main() -> int:
    # Shared embedding config: the query side (retrieval.get_retriever)
    # resolves the same model/URL, so an override can never fork them.
    embeddings = retrieval.get_embeddings()
    db_dir = os.environ.get("S5_CHROMA_DIR") or DB_DIR
    collection = retrieval.collection_name()
    print(f"embeddings: model={getattr(embeddings, 'model', '?')} "
          f"base_url={getattr(embeddings, 'base_url', '?')}")
    print(f"target: {db_dir} [{collection}]")
    gh_fetcher = None
    if os.environ.get("S5_GH_INDEX") != "0":
        gh_fetcher = fetch_gh_items
        print(f"github: {GH_REPO} (cap {GH_CAP} per kind, "
              f"most-recently-updated first)")
    else:
        print("github: skipped (S5_GH_INDEX=0)")
    try:
        n_docs, counts = build(
            ROOT, db_dir, collection, embeddings,
            gh_fetcher=gh_fetcher)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"loaded {n_docs} documents")
    total = sum(counts.values())
    print(f"split into {total} chunks "
          f"({counts['doc']} doc, {counts['code']} code, "
          f"{counts['issue']} issue, {counts['pr']} pr)")
    print(f"indexed into {db_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
