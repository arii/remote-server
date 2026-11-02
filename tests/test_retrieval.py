"""retrieval factory tests. Needs the deep-agent venv (langchain_chroma
import). No model calls: the retriever is constructed, never invoked.
Run: deep-agent/.venv/bin/python tests/test_retrieval.py"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import retrieval


class TestRetrieval(unittest.TestCase):
    def test_bad_k(self):
        for bad in (0, -1, "3", True, None):
            with self.assertRaises(ValueError):
                retrieval.get_retriever(bad)

    def test_get_retriever_cached(self):
        with tempfile.TemporaryDirectory() as d:
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


if __name__ == "__main__":
    unittest.main()
