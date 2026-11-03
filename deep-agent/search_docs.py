"""CLI: retrieve S5 runbook excerpts for a query (for Pi/harness use).

Usage: search_docs.py "<question>" [k]
Prints top-k excerpts with source tags. Local only, no model calls.
Retriever comes from the shared retrieval factory so index/query
embeddings stay consistent (S5_* / OLLAMA_URL overrides honored).
"""

import sys

import cli

cli.bootstrap()

import retrieval  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        return cli.usage_error('usage: search_docs.py "<question>" [k]')
    try:
        k = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    except ValueError:
        return cli.usage_error("k must be an integer")
    try:
        retriever = retrieval.get_retriever(k)
    except ValueError:
        return cli.usage_error("k must be a positive integer")
    except Exception as exc:
        print(f"error: cannot open index: {exc}", file=sys.stderr)
        return 1
    try:
        hits = retriever.invoke(sys.argv[1])
    except Exception as exc:
        print(f"error: doc search failed: {exc}", file=sys.stderr)
        return 1
    if not hits:
        print("no doc hits (index may be missing or stale;"
              " re-run deep-agent/build_index.py)")
        return 0
    for hit in hits:
        meta = hit.metadata
        print(f"--- {meta.get('repo')}/{meta.get('source')} ---")
        print(hit.page_content[:1200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
