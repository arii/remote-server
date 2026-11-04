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


class _FakeDocument:
    def __init__(self, page_content, metadata):
        self.page_content = page_content
        self.metadata = metadata


class _FakeSplitter:
    def __init__(self, chunk_size, chunk_overlap):
        pass

    def split_documents(self, docs):
        return list(docs)


class _FakeChroma:
    """Stand-in for langchain_chroma.Chroma (clear-path tests).

    delete_error: exception delete_collection() raises (None = ok).
    added: documents from_documents() received (None = never called).
    """

    delete_error = None
    added = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def delete_collection(self):
        if _FakeChroma.delete_error is not None:
            raise _FakeChroma.delete_error

    @classmethod
    def from_documents(
            cls, documents, embedding,
            persist_directory, collection_name):
        cls.added = list(documents)
        return cls()


_FAKE_MODULE_NAMES = (
    "langchain_chroma",
    "langchain_core",
    "langchain_core.documents",
    "langchain_text_splitters",
)


class TestClearFailure(unittest.TestCase):
    """A failed clear fails loud; only the fresh-index signal passes.

    Stdlib-only (#542): fake langchain modules stand in for the venv
    so build() runs on system python. Triggering a real locked
    chroma_db mid-run is nondeterministic; the raising fake is the
    deterministic repro.
    """

    def setUp(self):
        import types

        self._saved_modules = {
            name: sys.modules.get(name) for name in _FAKE_MODULE_NAMES
        }
        chroma_mod = types.ModuleType("langchain_chroma")
        chroma_mod.Chroma = _FakeChroma
        core_mod = types.ModuleType("langchain_core")
        docs_mod = types.ModuleType("langchain_core.documents")
        docs_mod.Document = _FakeDocument
        core_mod.documents = docs_mod
        split_mod = types.ModuleType("langchain_text_splitters")
        split_mod.RecursiveCharacterTextSplitter = _FakeSplitter
        sys.modules["langchain_chroma"] = chroma_mod
        sys.modules["langchain_core"] = core_mod
        sys.modules["langchain_core.documents"] = docs_mod
        sys.modules["langchain_text_splitters"] = split_mod
        _FakeChroma.delete_error = None
        _FakeChroma.added = None
        self.addCleanup(self._restore_modules)

    def _restore_modules(self):
        for name, mod in self._saved_modules.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod

    def _root(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        _write(tmp.name, "docs/guide.md", "# guide\n")
        return tmp.name

    def _build(self):
        return build_index.build(
            self._root(), "/nonexistent-db", "s5-docs", object())

    def test_lock_error_fails_loud_without_indexing(self):
        import sqlite3

        _FakeChroma.delete_error = sqlite3.OperationalError(
            "database is locked")
        with self.assertRaisesRegex(
                RuntimeError, "failed to clear collection"):
            self._build()
        self.assertIsNone(_FakeChroma.added)

    def test_permission_error_fails_loud_without_indexing(self):
        _FakeChroma.delete_error = PermissionError("denied")
        with self.assertRaisesRegex(
                RuntimeError, "refusing to index"):
            self._build()
        self.assertIsNone(_FakeChroma.added)

    def test_missing_collection_signal_still_passes(self):
        class NotFoundError(Exception):
            pass

        _FakeChroma.delete_error = NotFoundError(
            "Collection [s5-docs] does not exist")
        n_docs, counts = self._build()
        self.assertEqual(n_docs, 1)
        self.assertEqual(counts, {"doc": 1, "code": 0})
        self.assertIsNotNone(_FakeChroma.added)
        self.assertEqual(len(_FakeChroma.added), 1)

    def test_value_error_signal_still_passes(self):
        _FakeChroma.delete_error = ValueError(
            "Collection s5-docs does not exist.")
        n_docs, _ = self._build()
        self.assertEqual(n_docs, 1)
        self.assertIsNotNone(_FakeChroma.added)

    def test_main_returns_nonzero_with_stderr(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout

        import retrieval

        _FakeChroma.delete_error = OSError("lock stuck")
        real_embeddings = retrieval.get_embeddings
        retrieval.get_embeddings = lambda: object()
        self.addCleanup(
            setattr, retrieval, "get_embeddings", real_embeddings)
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = build_index.main()
        self.assertEqual(rc, 1)
        self.assertIn("failed to clear collection", err.getvalue())
        self.assertIsNone(_FakeChroma.added)


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
