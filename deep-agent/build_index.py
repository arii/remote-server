"""Build the shared S5 RAG index (Chroma, local embeddings).

Usage: build_index.py
Indexes repo docs + source code into deep-agent/chroma_db with `repo`,
`kind` (doc|code), and `language` metadata so per-repo agents filter
to their own slice. Purely local (Ollama nomic-embed-text); no hosted
calls. Re-run after docs or code change or whenever chroma_db is
wiped. Each run clears the collection first, so re-runs never stack
stale duplicates. Embedding model/URL come from
retrieval.get_embeddings() (S5_EMBED_MODEL / OLLAMA_URL overrides);
S5_CHROMA_DIR / S5_COLLECTION override the target.
"""

import glob
import os
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


def build(root, db_dir, collection, embeddings):
    """Read, split, embed, upsert. Returns (n_docs, counts).

    Clears the collection first (#542: re-runs never stack stale
    duplicates). Raises RuntimeError when nothing matches, leaving
    the index untouched.
    """
    from langchain_chroma import Chroma
    from langchain_core.documents import Document
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    infos = collect_source_files(root)
    if not infos:
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
    splits = RecursiveCharacterTextSplitter(
        chunk_size=1000, chunk_overlap=200
    ).split_documents(docs)
    counts = {"doc": 0, "code": 0}
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
    except Exception:
        pass  # Fresh index: no prior collection to clear.
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
    try:
        n_docs, counts = build(ROOT, db_dir, collection, embeddings)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"loaded {n_docs} documents")
    total = counts["doc"] + counts["code"]
    print(f"split into {total} chunks "
          f"({counts['doc']} doc, {counts['code']} code)")
    print(f"indexed into {db_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
