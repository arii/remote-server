"""CLI: retrieve S5 runbook excerpts for a query (for Pi/harness use).

Usage: search_docs.py "<question>" [k]
Prints top-k excerpts with source tags. Local only, no model calls.
"""

import sys

from langchain_chroma import Chroma
from langchain_ollama import OllamaEmbeddings

DB_DIR = "/srv/remote-server/deep-agent/chroma_db"


def main() -> int:
    if len(sys.argv) < 2:
        print('usage: search_docs.py "<question>" [k]', file=sys.stderr)
        return 2
    try:
        k = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    except ValueError:
        print("k must be an integer", file=sys.stderr)
        return 2
    store = Chroma(
        persist_directory=DB_DIR,
        embedding_function=OllamaEmbeddings(
            model="nomic-embed-text", base_url="http://ollama:11434"
        ),
        collection_name="s5-docs",
    )
    hits = store.as_retriever(search_kwargs={"k": k}).invoke(sys.argv[1])
    for hit in hits:
        meta = hit.metadata
        print(f"--- {meta.get('repo')}/{meta.get('source')} ---")
        print(hit.page_content[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
