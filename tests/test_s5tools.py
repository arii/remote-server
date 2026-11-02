import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import s5tools


class TestS5Tools(unittest.TestCase):
    def test_valid_repo(self):
        self.assertTrue(s5tools.valid_repo("arii/tech-dancer"))
        self.assertTrue(s5tools.valid_repo("a/b.c-d_e"))
        for bad in ("", "noslash", "a/b c", "a/", None, 42):
            self.assertFalse(s5tools.valid_repo(bad))
        # Shape-valid but sneaky: split-take-last neutralizes traversal.
        self.assertTrue(s5tools.valid_repo("../x"))

    def test_valid_number(self):
        self.assertEqual(s5tools.valid_number("153"), 153)
        self.assertEqual(s5tools.valid_number(153), 153)
        for bad in ("0", "-3", "1.5", "12x", "", None, True, 0, -1):
            self.assertIsNone(s5tools.valid_number(bad))

    def test_checkout_for(self):
        self.assertEqual(
            s5tools.checkout_for("arii/remote-server"), "/srv/remote-server"
        )
        self.assertEqual(
            s5tools.checkout_for("arii/tech-dancer"), "/srv/repos/tech-dancer"
        )
        with self.assertRaises(ValueError):
            s5tools.checkout_for("nope")
        # Traversal neutralized: only the post-slash part is used.
        self.assertEqual(s5tools.checkout_for("../escape"), "/srv/repos/escape")

    def test_run_ok_and_failure(self):
        out = s5tools.run([sys.executable, "-c", "print('hi')"])
        self.assertEqual(out, "hi")
        with self.assertRaises(RuntimeError) as ctx:
            s5tools.run([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertIn("rc=3", str(ctx.exception))
        with self.assertRaises(ValueError):
            s5tools.run("not-a-list")
        with self.assertRaises(ValueError):
            s5tools.run([])

    def test_run_json(self):
        self.assertEqual(
            s5tools.run_json([sys.executable, "-c", "print('{\"a\": 1}')"]),
            {"a": 1},
        )
        with self.assertRaises(RuntimeError):
            s5tools.run_json([sys.executable, "-c", "print('nope')"])

    def test_gh_json(self):
        completed = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout='{"x": true}', stderr=""
        )
        with mock.patch.object(subprocess, "run", return_value=completed):
            self.assertEqual(s5tools.gh_json(["pr", "view", "1"]), {"x": True})
        failed = subprocess.CompletedProcess(
            args=["gh"], returncode=1, stdout="", stderr="boom"
        )
        with mock.patch.object(subprocess, "run", return_value=failed):
            with self.assertRaises(RuntimeError) as ctx:
                s5tools.gh_json(["pr", "view", "1"])
        self.assertIn("boom", str(ctx.exception))

    def test_read_repo_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "a.txt")
            with open(p, "w", encoding="utf-8") as f:
                f.write("hello")
            self.assertEqual(s5tools.read_repo_file(d, "a.txt"), "hello")
            self.assertTrue(
                s5tools.read_repo_file(d, "../../etc/passwd").startswith("REJECTED")
            )
            self.assertTrue(s5tools.read_repo_file(d, "missing").startswith("not found"))

    def test_list_dir(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "b.txt"), "w").close()
            os.mkdir(os.path.join(d, "a_dir"))
            self.assertEqual(s5tools.list_dir(d), "a_dir\nb.txt")
            self.assertTrue(s5tools.list_dir(d, "..").startswith("REJECTED"))
            self.assertTrue(s5tools.list_dir(d, "nope").startswith("list failed"))

    def test_grep_repo(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "a.txt"), "w", encoding="utf-8") as f:
                f.write("needle here\n")
            out = s5tools.grep_repo(d, "needle")
            self.assertIn("needle", out)
            self.assertEqual(s5tools.grep_repo(d, "absent-xyz"), "no matches")
            self.assertTrue(s5tools.grep_repo(d, "x" * 121).startswith("REJECTED"))

    def test_grep_repo_skips_generated_dirs(self):
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, ".boomtick"))
            with open(os.path.join(d, ".boomtick", "n.json"), "w") as f:
                f.write("needle noise\n" * 30)
            with open(os.path.join(d, "ledger.jsonl"), "w") as f:
                f.write("needle noise\n" * 30)
            with open(os.path.join(d, "s.py"), "w") as f:
                f.write("needle here\n")
            out = s5tools.grep_repo(d, "needle")
            self.assertIn("s.py", out)
            self.assertNotIn("boomtick", out)
            self.assertNotIn("ledger.jsonl", out)

    def test_grep_repo_skips_all_index_and_cache_dirs(self):
        # Every excluded dir on its own: one flood (>20 matches) per
        # dir must never bury the single real hit. This is the exact
        # shape of the code-locate 4/5 live failure (grep-noise).
        import tempfile

        for dirname in (".git", "node_modules", ".venv", "__pycache__",
                        ".boomtick", "chroma_db", ".claude"):
            with tempfile.TemporaryDirectory() as d:
                os.mkdir(os.path.join(d, dirname))
                with open(os.path.join(d, dirname, "n.txt"), "w") as f:
                    f.write("needle noise\n" * 30)
                with open(os.path.join(d, "real.py"), "w") as f:
                    f.write("needle here\n")
                out = s5tools.grep_repo(d, "needle")
                self.assertIn("real.py", out, dirname)
                self.assertNotIn(dirname.strip("."), out, dirname)


if __name__ == "__main__":
    unittest.main()
