"""Build the shared S5 docs RAG index (Chroma, local embeddings).

Usage: build_index.py
Indexes repo docs + Pi skills into deep-agent/chroma_db with a `repo`
metadata field so per-repo agents filter to their own slice. Purely
local (Ollama nomic-embed-text); no hosted calls. Re-run after docs
change or whenever chroma_db is wiped. Each run clears the collection
first, so re-runs never stack stale duplicates. Embedding model/URL
come from retrieval.get_embeddings() (S5_EMBED_MODEL / OLLAMA_URL
overrides); S5_CHROMA_DIR / S5_COLLECTION override the target.
"""

import glob
import os
import sys

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

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


def main() -> int:
    docs = []
    for pattern in SOURCES:
        for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
            with open(path, encoding="utf-8") as f:
                docs.append(
                    Document(
                        page_content=f.read(),
                        metadata={
                            "source": os.path.relpath(path, ROOT),
                            "repo": "remote-server",
                        },
                    )
                )
    print(f"loaded {len(docs)} documents")
    if not docs:
        print("no source documents matched; index left untouched",
              file=sys.stderr)
        return 1
    splits = RecursiveCharacterTextSplitter(
        chunk_size=1000, chunk_overlap=200
    ).split_documents(docs)
    print(f"split into {len(splits)} chunks")
    # Shared embedding config: the query side (retrieval.get_retriever)
    # resolves the same model/URL, so an override can never fork them.
    embeddings = retrieval.get_embeddings()
    db_dir = os.environ.get("S5_CHROMA_DIR") or DB_DIR
    collection = retrieval.collection_name()
    print(f"embeddings: model={getattr(embeddings, 'model', '?')} "
          f"base_url={getattr(embeddings, 'base_url', '?')}")
    print(f"target: {db_dir} [{collection}]")
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
    print(f"indexed into {db_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
