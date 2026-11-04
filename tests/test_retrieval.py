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


if __name__ == "__main__":
    unittest.main()
