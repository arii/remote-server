"""build_index pattern/skip/metadata tests + retrieval smoke (#668).

Collector + metadata tests are stdlib-only (build_index keeps its
langchain imports inside build()). The smoke test needs the
deep-agent venv AND a reachable Ollama (real nomic embeddings into
a temp Chroma dir — never the prod chroma_db); it skips otherwise.
Run: python3 tests/test_build_index.py (smoke needs the venv python)
"""
import os
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import build_index

try:
    import langchain_chroma  # noqa: F401
    import langchain_core.documents  # noqa: F401
    import langchain_text_splitters  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def _ollama_up():
    base = (
        os.environ.get("OLLAMA_URL") or "http://ollama:11434").rstrip("/")
    try:
        with urllib.request.urlopen(
                base + "/api/tags", timeout=5) as res:
            return res.status == 200
    except Exception:
        return False


def _write(root, relpath, content="x = 1\n"):
    path = os.path.join(root, relpath)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestCollectSourceFiles(unittest.TestCase):
    def _fixture_root(self, root):
        _write(root, "docs/guide.md", "# guide\n")
        _write(root, "AGENTS.md", "# agents\n")
        _write(root, "backend/app.py", "VALUE = 1\n")
        _write(root, "backend/tests/test_app.py", "t = 1\n")
        _write(root, "backend/__pycache__/app.py", "cached\n")
        _write(root, "deep-agent/top.py", "TOP = 1\n")
        _write(root, "deep-agent/nested/deep.py", "DEEP = 1\n")
        _write(root, "deep-agent/.venv/lib/x.py", "v = 1\n")
        _write(root, "deep-agent/traces/t.py", "t = 1\n")
        _write(root, "deep-agent/chroma_db/c.py", "c = 1\n")
        _write(root, "frontend/src/a.ts", "export const a = 1;\n")
        _write(root, "frontend/src/views/b.tsx", "export const b = 1;\n")
        _write(root, "frontend/src/c.test.ts", "test\n")
        _write(root, "frontend/src/views/d.test.tsx", "test\n")
        _write(root, "frontend/src/node_modules/pkg/f.ts", "f = 1\n")
        _write(root, "frontend/src/dist/g.ts", "g = 1\n")
        _write(root, "frontend/src/.hidden.ts", "h = 1\n")
        _write(root, "core_cli/cli.py", "CLI = 1\n")
        _write(root, "core_cli/fixtures/f.py", "f = 1\n")

    def test_docs_and_code_collected_with_tags(self):
        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            found = build_index.collect_source_files(root)
        self.assertEqual(
            found,
            [
                ("docs/guide.md", "doc", "markdown"),
                ("AGENTS.md", "doc", "markdown"),
                ("backend/app.py", "code", "python"),
                ("deep-agent/top.py", "code", "python"),
                ("frontend/src/a.ts", "code", "typescript"),
                ("frontend/src/views/b.tsx", "code", "typescript"),
                ("core_cli/cli.py", "code", "python"),
            ],
        )

    def test_excluded_dirs_never_descended(self):
        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            found = build_index.collect_source_files(root)
        rels = [rel for rel, _, _ in found]
        for rel in rels:
            parts = rel.split(os.sep)
            for skipped in build_index.SKIP_DIRS:
                self.assertNotIn(skipped, parts, rel)
            self.assertNotIn(".test.", os.path.basename(rel), rel)

    def test_missing_bases_tolerated(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "docs/only.md", "# only\n")
            found = build_index.collect_source_files(root)
        self.assertEqual(
            found, [("docs/only.md", "doc", "markdown")])

    def test_real_tree_exclusions_hold(self):
        root = os.path.join(
            os.path.dirname(__file__), "..")
        found = build_index.collect_source_files(root)
        self.assertTrue(found)
        kinds = {kind for _, kind, _ in found}
        self.assertEqual(kinds, {"doc", "code"})
        for rel, kind, language in found:
            parts = rel.split(os.sep)
            for skipped in build_index.SKIP_DIRS:
                self.assertNotIn(skipped, parts, rel)
            self.assertNotIn(".test.", os.path.basename(rel), rel)
            if kind == "code":
                self.assertIn(language, ("python", "typescript"))
            else:
                self.assertEqual(language, "markdown")
        rels = [rel for rel, _, _ in found]
        self.assertIn("backend/api/routers/jobs.py", rels)
        self.assertIn("deep-agent/build_index.py", rels)
        self.assertIn("frontend/src/views/JobsView.tsx", rels)


class TestDocMetadata(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(
            build_index.doc_metadata("backend/a.py", "code", "python"),
            {"source": "backend/a.py", "repo": "remote-server",
             "kind": "code", "language": "python"},
        )
        self.assertEqual(
            build_index.doc_metadata("docs/g.md", "doc", "markdown"),
            {"source": "docs/g.md", "repo": "remote-server",
             "kind": "doc", "language": "markdown"},
        )


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
@unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
class TestBuildSmoke(unittest.TestCase):
    COLLECTION = "s5-docs-smoke-668"

    def _fixture_root(self, root):
        _write(root, "docs/guide.md",
               "# Guide\n\nRun the server with docker compose up.\n")
        _write(root, "backend/other.py",
               '"""Unrelated helper."""\n\nOTHER = 1\n')
        _write(
            root,
            "backend/compare_endpoint.py",
            '"""Unified diff of two runs outputs."""\n'
            "\n"
            "def compare_runs(a, b):\n"
            '    """Return the unified diff of two run outputs."""\n'
            "    return (a, b)\n",
        )

    def test_code_query_returns_file_path(self):
        import retrieval
        from langchain_chroma import Chroma

        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            with tempfile.TemporaryDirectory() as db_dir:
                embeddings = retrieval.get_embeddings()
                n_docs, counts = build_index.build(
                    root, db_dir, self.COLLECTION, embeddings)
                self.assertEqual(n_docs, 3)
                self.assertEqual(counts["code"], 2)
                self.assertEqual(counts["doc"], 1)
                store = Chroma(
                    persist_directory=db_dir,
                    embedding_function=embeddings,
                    collection_name=self.COLLECTION,
                )
                hits = store.similarity_search(
                    "unified diff of two run outputs", k=3)
                sources = [
                    h.metadata.get("source") for h in hits]
                self.assertIn(
                    "backend/compare_endpoint.py", sources)
                code_hits = [
                    h for h in hits
                    if h.metadata.get("source")
                    == "backend/compare_endpoint.py"
                ]
                self.assertEqual(
                    code_hits[0].metadata.get("kind"), "code")
                self.assertEqual(
                    code_hits[0].metadata.get("language"), "python")

    def test_rebuild_twice_no_duplicates(self):
        import retrieval
        from langchain_chroma import Chroma

        with tempfile.TemporaryDirectory() as root:
            self._fixture_root(root)
            with tempfile.TemporaryDirectory() as db_dir:
                embeddings = retrieval.get_embeddings()
                build_index.build(
                    root, db_dir, self.COLLECTION, embeddings)
                store = Chroma(
                    persist_directory=db_dir,
                    embedding_function=embeddings,
                    collection_name=self.COLLECTION,
                )
                first = len(store.get()["ids"])
                self.assertGreater(first, 0)
                build_index.build(
                    root, db_dir, self.COLLECTION, embeddings)
                store = Chroma(
                    persist_directory=db_dir,
                    embedding_function=embeddings,
                    collection_name=self.COLLECTION,
                )
                self.assertEqual(len(store.get()["ids"]), first)


if __name__ == "__main__":
    unittest.main()
