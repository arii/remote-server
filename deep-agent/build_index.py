"""Build the shared S5 docs RAG index (Chroma, local embeddings).

Usage: build_index.py
Indexes repo docs + Pi skills into deep-agent/chroma_db with a `repo`
metadata field so per-repo agents filter to their own slice. Purely
local (Ollama nomic-embed-text); no hosted calls. Re-run after docs
change or whenever chroma_db is wiped.
"""

import glob
import os

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_ollama import OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter

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
    splits = RecursiveCharacterTextSplitter(
        chunk_size=1000, chunk_overlap=200
    ).split_documents(docs)
    print(f"split into {len(splits)} chunks")
    embeddings = OllamaEmbeddings(
        model="nomic-embed-text", base_url="http://ollama:11434"
    )
    Chroma.from_documents(
        documents=splits,
        embedding=embeddings,
        persist_directory=DB_DIR,
        collection_name="s5-docs",
    )
    print(f"indexed into {DB_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
