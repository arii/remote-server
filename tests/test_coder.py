"""coder slice tests. Part A (system python, stdlib-only import):
argv parsing, run_check allowlist, system prompt builder. Part B
(deep-agent venv): FilesystemBackend root enforcement + tool wiring.
Run: python3 tests/test_coder.py  (A only under system python)
     deep-agent/.venv/bin/python tests/test_coder.py  (A + B)"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import coder

try:
    from deepagents.backends import FilesystemBackend  # noqa: F401
    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


class TestArgv(unittest.TestCase):
    def test_spawner_shape(self):
        args = coder.parse_args(
            ["-p", "--system-prompt", "CTX", "--model", "m1", "do it"])
        self.assertTrue(args.p)
        self.assertEqual(args.system_prompt, "CTX")
        self.assertEqual(args.model, "m1")
        self.assertEqual(args.brief, "do it")
        self.assertIsNone(args.worktree)

    def test_direct_shape(self):
        args = coder.parse_args(
            ["--worktree", "/tmp/wt", "--thread", "t1", "fix typo"])
        self.assertEqual(args.worktree, "/tmp/wt")
        self.assertEqual(args.thread, "t1")

    def test_brief_required(self):
        with self.assertRaises(SystemExit):
            coder.parse_args(["-p"])


class TestAllowlist(unittest.TestCase):
    def test_allowed_prefixes(self):
        self.assertTrue(coder.check_allowed(
            ["python3", "-m", "pytest", "tests/", "-q"]))
        self.assertTrue(coder.check_allowed(
            ["pnpm", "exec", "vitest", "run", "x.test.ts"]))
        self.assertTrue(coder.check_allowed(
            ["git", "diff", "--stat"]))
        self.assertTrue(coder.check_allowed(["npx", "tsc", "--noEmit"]))

    def test_rejected(self):
        self.assertFalse(coder.check_allowed([]))
        self.assertFalse(coder.check_allowed(["rm", "-rf", "."]))
        self.assertFalse(coder.check_allowed(["git", "push"]))
        self.assertFalse(coder.check_allowed(["curl", "http://x"]))
        self.assertFalse(coder.check_allowed(
            ["python3", "-c", "import os"]))
        # Prefix tricks must not smuggle through.
        self.assertFalse(coder.check_allowed(
            ["python3", "-m", "pytester"]))
        self.assertFalse(coder.check_allowed(["pytestx"]))


class TestSystemPrompt(unittest.TestCase):
    def test_rules_always_present(self):
        sys_prompt = coder.build_system("/wt", "")
        self.assertIn("/wt", sys_prompt)
        self.assertIn("Do NOT commit", sys_prompt)
        self.assertIn("run_check", sys_prompt)

    def test_context_prepended(self):
        sys_prompt = coder.build_system("/wt", "CONTRACT")
        self.assertTrue(sys_prompt.startswith("CONTRACT"))
        self.assertIn("Do NOT commit", sys_prompt)


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestBackendRoot(unittest.TestCase):
    def test_write_read_round_trip(self):
        import tempfile

        from deepagents.backends import FilesystemBackend

        with tempfile.TemporaryDirectory() as d:
            backend = FilesystemBackend(root_dir=d, virtual_mode=True)
            backend.write("/notes.txt", "hello")
            self.assertEqual(
                backend.read("/notes.txt").file_data["content"], "hello")
            self.assertTrue(os.path.isfile(
                os.path.join(d, "notes.txt")))

    def test_escapes_rejected(self):
        import tempfile

        from deepagents.backends import FilesystemBackend

        with tempfile.TemporaryDirectory() as d:
            backend = FilesystemBackend(root_dir=d, virtual_mode=True)
            for bad in ("/../escape.txt", "/sub/../../escape.txt"):
                with self.assertRaises(Exception, msg=bad):
                    backend.write(bad, "x")
                with self.assertRaises(Exception, msg=bad):
                    backend.read(bad)
            # Absolute paths resolve virtually: contained, never host.
            backend.write("/tmp/coder-escape-probe.txt", "x")
            self.assertTrue(os.path.isfile(os.path.join(
                d, "tmp", "coder-escape-probe.txt")))
            self.assertFalse(os.path.exists(
                "/tmp/coder-escape-probe.txt"))


if __name__ == "__main__":
    unittest.main()
