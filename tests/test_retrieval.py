"""retrieval helper tests. Stdlib-only: the Chroma retriever is gone,
so the whole module imports without the deep-agent venv. No model
calls. Run: python3 tests/test_retrieval.py"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import retrieval


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


class TestIndexReadiness(unittest.TestCase):
    """index_status/require_ready guard. Stdlib-only."""

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

class TestIndexChunks(unittest.TestCase):
    """index_chunks(): embedding row count or None. Stdlib-only."""

    def _chunks_in(self, d, files):
        import sqlite3
        for name, content in files.items():
            path = os.path.join(d, name)
            if isinstance(content, int):
                with sqlite3.connect(path) as con:
                    con.execute(
                        "CREATE TABLE embeddings "
                        "(id INTEGER PRIMARY KEY)")
                    con.executemany(
                        "INSERT INTO embeddings (id) VALUES (?)",
                        [(i,) for i in range(content)])
                    con.commit()
            else:
                with open(path, "wb") as f:
                    f.write(content)
        os.environ["S5_CHROMA_DIR"] = d
        try:
            return retrieval.index_chunks()
        finally:
            del os.environ["S5_CHROMA_DIR"]

    def test_counts_embedding_rows(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                self._chunks_in(d, {"chroma.sqlite3": 3728}), 3728)

    def test_missing_or_garbage_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(self._chunks_in(d, {}))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(self._chunks_in(
                d, {"chroma.sqlite3": b"not-a-database"}))
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(self._chunks_in(
                d, {"other.sqlite3": 10}))

    def test_wrong_schema_is_none(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "chroma.sqlite3")
            with sqlite3.connect(path) as con:
                con.execute("CREATE TABLE other (id INTEGER)")
                con.commit()
            os.environ["S5_CHROMA_DIR"] = d
            try:
                self.assertIsNone(retrieval.index_chunks())
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
