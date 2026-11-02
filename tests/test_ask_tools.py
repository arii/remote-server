#!/usr/bin/env python3
"""Focused tests for ask_investigator read-only tools.

Regression cover: grep_repo must work with POSIX grep only (the eval
failed live when the tool shelled to `rg`, which is not installed).
"""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

_spec = importlib.util.spec_from_file_location(
    "ask_investigator",
    os.path.join(os.path.dirname(__file__), "..", "deep-agent",
                 "ask_investigator.py"),
)
ai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ai)


class TestAskTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prev_checkout = ai._checkout
        ai._checkout = self.tmp.name
        self.addCleanup(setattr, ai, "_checkout", self.prev_checkout)
        with open(os.path.join(self.tmp.name, "a.txt"), "w") as f:
            f.write("hello needle world\nsecond line\n")
        gitdir = os.path.join(self.tmp.name, ".git")
        os.makedirs(gitdir)
        with open(os.path.join(gitdir, "x.txt"), "w") as f:
            f.write("needle in gitdir\n")

    def test_grep_finds_literal(self):
        out = ai.grep_repo.invoke({"pattern": "needle"})
        self.assertIn("a.txt", out)
        self.assertIn("hello needle world", out)

    def test_grep_excludes_git(self):
        out = ai.grep_repo.invoke({"pattern": "needle"})
        self.assertNotIn(".git", out)

    def test_grep_no_match(self):
        self.assertEqual(
            ai.grep_repo.invoke({"pattern": "zzz-no-such-text"}),
            "no matches",
        )

    def test_grep_rejects_bad_pattern(self):
        self.assertIn("REJECTED", ai.grep_repo.invoke({"pattern": ""}))
        self.assertIn(
            "REJECTED", ai.grep_repo.invoke({"pattern": "x" * 121}))

    def test_read_file_ok_and_missing(self):
        self.assertIn("hello", ai.read_repo_file.invoke({"path": "a.txt"}))
        self.assertIn(
            "not found",
            ai.read_repo_file.invoke({"path": "nope.txt"}),
        )

    def test_read_file_rejects_escape(self):
        self.assertIn(
            "REJECTED",
            ai.read_repo_file.invoke({"path": "../escape.txt"}),
        )

    def test_pr_state_rejects_bad_number(self):
        # Non-strings die at schema validation (no bool->1 coercion).
        for bad in [0, -3, True]:
            with self.assertRaises(Exception, msg=f"number={bad!r}"):
                ai.pr_state.invoke({"number": bad})
        # Bad strings reach the tool and are rejected there.
        for bad in ["15a", "", "0"]:
            self.assertIn(
                "REJECTED", ai.pr_state.invoke({"number": bad}),
                f"number={bad!r}",
            )

    def test_pr_state_success_and_failures(self):
        ok = subprocess.CompletedProcess(
            args=["gh"], returncode=0,
            stdout='{state: OPEN, title: x}\n', stderr="")
        with mock.patch.object(ai.subprocess, "run", return_value=ok):
            out = ai.pr_state.invoke({"number": "153"})
        self.assertIn("OPEN", out)
        err = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="not found")
        with mock.patch.object(ai.subprocess, "run", return_value=err):
            self.assertIn(
                "gh error", ai.pr_state.invoke({"number": "999999"}))
        with mock.patch.object(
                ai.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("gh", 60)):
            self.assertIn(
                "gh failed", ai.pr_state.invoke({"number": "153"}))
        with mock.patch.object(
                ai.subprocess, "run", side_effect=OSError("no gh")):
            self.assertIn(
                "gh failed", ai.pr_state.invoke({"number": "153"}))

    def test_search_docs_rejects_bad_query(self):
        # Validation runs before the retriever is touched: no model.
        for bad in ["", "   ", "x" * 501]:
            self.assertEqual(
                ai.search_docs.invoke({"query": bad}),
                "REJECTED: bad query",
            )

    def test_search_docs_formats_hits(self):
        hits = [
            SimpleNamespace(metadata={"repo": "r", "source": "s"},
                            page_content="how to run"),
            SimpleNamespace(metadata={}, page_content=""),
        ]
        retriever = mock.Mock()
        retriever.invoke.return_value = hits
        with mock.patch.object(ai, "_retriever_for",
                               return_value=retriever):
            out = ai.search_docs.invoke({"query": "run server"})
        self.assertIn("[r/s] how to run", out)
        retriever.invoke.assert_called_once_with("run server")

    def test_search_docs_unavailable(self):
        retriever = mock.Mock()
        retriever.invoke.side_effect = RuntimeError("no chroma")
        with mock.patch.object(ai, "_retriever_for",
                               return_value=retriever):
            self.assertIn(
                "doc search unavailable",
                ai.search_docs.invoke({"query": "run server"}),
            )

    def test_grep_skips_index_and_log_noise(self):
        # Regression: code-locate failed 4/5 live because .boomtick /
        # chroma_db / ledger noise flooded grep's -m 20 window and
        # buried server.py. The shared helper must exclude them.
        for dirname in (".boomtick", "chroma_db", ".claude",
                        "__pycache__", "node_modules", ".venv"):
            d = os.path.join(self.tmp.name, dirname)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "noise.txt"), "w") as f:
                f.write("needle noise\n" * 30)
        with open(os.path.join(self.tmp.name, "ledger.jsonl"), "w") as f:
            f.write("needle noise\n" * 30)
        with open(os.path.join(self.tmp.name, "s.py"), "w") as f:
            f.write("needle here\n")
        out = ai.grep_repo.invoke({"pattern": "needle"})
        self.assertIn("s.py", out)
        for noisy in (".boomtick", "chroma_db", ".claude", "__pycache__",
                      "node_modules", ".venv", "ledger.jsonl"):
            self.assertNotIn(noisy, out)

    def test_read_file_truncates(self):
        big = os.path.join(self.tmp.name, "big.txt")
        with open(big, "w") as f:
            f.write("z" * 4000)
        out = ai.read_repo_file.invoke({"path": "big.txt"})
        self.assertEqual(len(out), 3000)


if __name__ == "__main__":
    unittest.main()
