"""retrieval factory tests. The retriever-construction tests need the
deep-agent venv (langchain_chroma import); the readiness tests are
stdlib-only. No model calls: the retriever is constructed, never
invoked. Run: deep-agent/.venv/bin/python tests/test_retrieval.py"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import retrieval

try:
    import langchain_chroma  # noqa: F401

    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


class TestRetrieval(unittest.TestCase):
    def test_bad_k(self):
        for bad in (0, -1, "3", True, None):
            with self.assertRaises(ValueError):
                retrieval.get_retriever(bad)

    @unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
    def test_get_retriever_cached(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "chroma.sqlite3"), "wb") as f:
                f.write(b"fake-index-bytes")
            os.environ["S5_CHROMA_DIR"] = d
            try:
                retrieval._retrievers.clear()
                first = retrieval.get_retriever(2)
                self.assertIs(first, retrieval.get_retriever(2))
                self.assertIsNot(first, retrieval.get_retriever(3))
            finally:
                del os.environ["S5_CHROMA_DIR"]
                retrieval._retrievers.clear()

    def test_format_hits(self):
        hits = [
            SimpleNamespace(
                metadata={"repo": "r", "source": "s"}, page_content="x" * 900
            ),
            SimpleNamespace(metadata={}, page_content=""),
        ]
        out = retrieval.format_hits(hits, limit=100)
        self.assertIn("[r/s]", out)
        self.assertIn("x" * 100, out)
        self.assertNotIn("x" * 101, out)
        self.assertEqual(retrieval.format_hits([]), "no doc hits")
        self.assertEqual(retrieval.format_hits(None), "no doc hits")


class TestNormalizeRepo(unittest.TestCase):
    """Stdlib-only: blank means all repos, bare names gain arii/."""

    def test_unfiltered(self):
        self.assertIsNone(retrieval.normalize_repo(None))
        self.assertIsNone(retrieval.normalize_repo(""))
        self.assertIsNone(retrieval.normalize_repo("   "))

    def test_bare_name_gains_owner(self):
        self.assertEqual(
            retrieval.normalize_repo("portfolio"), "arii/portfolio")
        self.assertEqual(
            retrieval.normalize_repo("  tech-dancer "),
            "arii/tech-dancer")

    def test_full_name_passthrough(self):
        self.assertEqual(
            retrieval.normalize_repo("arii/hrm"), "arii/hrm")

    def test_bad_filters_raise(self):
        for bad in ("arii/", "/x", "a/b/c", "has space/x",
                    "arii/portfolio/", 123, True, ["x"]):
            with self.assertRaises(ValueError, msg=repr(bad)):
                retrieval.normalize_repo(bad)


class TestRepoFilter(unittest.TestCase):
    """get_retriever(repo=...) passes a Chroma metadata filter and
    caches per (k, repo). Stdlib-only (fake Chroma module)."""

    def _ready_dir(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "chroma.sqlite3"), "wb") as f:
            f.write(b"fake-index-bytes")
        os.environ["S5_CHROMA_DIR"] = tmp.name
        self.addCleanup(os.environ.pop, "S5_CHROMA_DIR", None)

    def test_filter_kwargs_and_cache(self):
        import types
        from unittest import mock

        seen = []

        class FakeChroma:
            def __init__(self, **kwargs):
                pass

            def as_retriever(self, search_kwargs=None):
                marker = object()
                seen.append((marker, dict(search_kwargs or {})))
                return marker

        mod = types.ModuleType("langchain_chroma")
        mod.Chroma = FakeChroma
        self._ready_dir()
        retrieval._retrievers.clear()
        self.addCleanup(retrieval._retrievers.clear)
        real_embeddings = retrieval.get_embeddings
        retrieval.get_embeddings = lambda: object()
        self.addCleanup(
            setattr, retrieval, "get_embeddings", real_embeddings)
        with mock.patch.dict(sys.modules, {"langchain_chroma": mod}):
            unfiltered = retrieval.get_retriever(3)
            self.assertIs(unfiltered, retrieval.get_retriever(3))
            self.assertIs(
                unfiltered, retrieval.get_retriever(3, repo=""))
            scoped = retrieval.get_retriever(3, repo="portfolio")
            self.assertIs(
                scoped,
                retrieval.get_retriever(3, repo="arii/portfolio"))
            self.assertIsNot(scoped, unfiltered)
            other_k = retrieval.get_retriever(2, repo="portfolio")
            self.assertIsNot(other_k, scoped)
        by_marker = dict(seen)
        self.assertEqual(by_marker[unfiltered], {"k": 3})
        self.assertEqual(
            by_marker[scoped],
            {"k": 3, "filter": {"repo": "arii/portfolio"}})
        self.assertEqual(
            by_marker[other_k],
            {"k": 2, "filter": {"repo": "arii/portfolio"}})

    def test_bad_repo_raises_before_ready_check(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["S5_CHROMA_DIR"] = d
            try:
                with self.assertRaises(ValueError):
                    retrieval.get_retriever(3, repo="??")
            finally:
                del os.environ["S5_CHROMA_DIR"]


class TestIndexReadiness(unittest.TestCase):
    """index_status/require_ready/get_retriever guard. Stdlib-only."""

    def _status_in(self, d, files):
        for name, content in files.items():
            with open(os.path.join(d, name), "wb") as f:
                f.write(content)
        os.environ["S5_CHROMA_DIR"] = d
        try:
            return retrieval.index_status()
        finally:
            del os.environ["S5_CHROMA_DIR"]

    def test_status_transitions(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self._status_in(d, {}), "missing")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                self._status_in(
                    d, {"chroma.sqlite3": b"fake-index-bytes"}),
                "ready")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                self._status_in(d, {"chroma.sqlite3": b""}),
                "missing")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                self._status_in(d, {".index_building": b"started"}),
                "warming")
        with tempfile.TemporaryDirectory() as d:
            # Build marker wins over a partial-looking sqlite.
            self.assertEqual(
                self._status_in(d, {
                    "chroma.sqlite3": b"partial",
                    ".index_building": b"started",
                }),
                "warming")
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                self._status_in(d, {".index_failed": b"boom"}),
                "failed")
        with tempfile.TemporaryDirectory() as d:
            # Stale failure beside a usable index still serves.
            self.assertEqual(
                self._status_in(d, {
                    "chroma.sqlite3": b"fake-index-bytes",
                    ".index_failed": b"stale",
                }),
                "ready")

    def test_require_ready_messages(self):
        with tempfile.TemporaryDirectory() as d:
            os.environ["S5_CHROMA_DIR"] = d
            try:
                with self.assertRaisesRegex(
                        retrieval.IndexNotReadyError, "missing"):
                    retrieval.require_ready()
                with open(os.path.join(
                        d, ".index_building"), "wb") as f:
                    f.write(b"started")
                with self.assertRaisesRegex(
                        retrieval.IndexNotReadyError,
                        "warming, retry shortly"):
                    retrieval.require_ready()
                os.remove(os.path.join(d, ".index_building"))
                with open(os.path.join(
                        d, ".index_failed"), "wb") as f:
                    f.write(b"boom")
                with self.assertRaisesRegex(
                        retrieval.IndexNotReadyError, "build failed"):
                    retrieval.require_ready()
                with open(os.path.join(
                        d, "chroma.sqlite3"), "wb") as f:
                    f.write(b"fake-index-bytes")
                os.remove(os.path.join(d, ".index_failed"))
                self.assertIsNone(retrieval.require_ready())
            finally:
                del os.environ["S5_CHROMA_DIR"]

    def test_get_retriever_refuses_warming(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(
                    d, ".index_building"), "wb") as f:
                f.write(b"started")
            os.environ["S5_CHROMA_DIR"] = d
            try:
                with self.assertRaisesRegex(
                        retrieval.IndexNotReadyError,
                        "warming, retry shortly"):
                    retrieval.get_retriever(3)
            finally:
                del os.environ["S5_CHROMA_DIR"]


class TestTdQuery(unittest.TestCase):
    """td rag query path: JSON parsing, errors, formatting."""

    def _run(self, stdout="", returncode=0, stderr=""):
        import subprocess
        from unittest import mock

        proc = subprocess.CompletedProcess(
            args=["td"], returncode=returncode, stdout=stdout,
            stderr=stderr)
        return mock.patch("subprocess.run", return_value=proc)

    def test_query_td_repo_parses_hits(self):
        import json

        payload = {"status": "success", "hits": [
            {"content": "hello", "filepath": "a.py", "start_line": 3,
             "score": 1.5}]}
        with self._run(stdout=json.dumps(payload)):
            hits = retrieval.query_td_repo("q", "/tmp")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["filepath"], "a.py")

    def test_query_td_repo_raises_on_nonjson(self):
        with self._run(stdout="not json"):
            with self.assertRaises(RuntimeError):
                retrieval.query_td_repo("q", "/tmp")

    def test_query_td_repo_raises_on_exit(self):
        with self._run(stdout="", returncode=1, stderr="boom"):
            with self.assertRaises(RuntimeError):
                retrieval.query_td_repo("q", "/tmp")

    def test_search_td_formats_and_degrades(self):
        import json

        payload = {"status": "success", "hits": [
            {"content": "x" * 50, "filepath": "a.py",
             "start_line": 3}]}
        with self._run(stdout=json.dumps(payload)):
            out = retrieval.search_td("q", "/tmp")
        self.assertIn("[a.py:3]", out)
        with self._run(stdout="garbage"):
            out = retrieval.search_td("q", "/tmp")
        self.assertIn("doc search unavailable", out)



if __name__ == "__main__":
    unittest.main()
