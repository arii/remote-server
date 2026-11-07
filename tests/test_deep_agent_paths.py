"""Checkout-derived deep-agent paths (#540).

Four deep-agent scripts used to hardcode the production checkout path
for their .env and chroma_db, so worktree runs loaded the production
.env and read/wrote the production index. All four now resolve via
cli.repo_root() (derived from __file__, S5_CHECKOUT override).

System python, no venv: nothing imported here needs langchain.
Run: python3 tests/test_deep_agent_paths.py
"""
import ast
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import cli
import retrieval

DEEP_AGENT = os.path.dirname(os.path.abspath(retrieval.__file__))
FORBIDDEN = "/srv/remote-server"
# s5tools.CHECKOUTS is a repo->path mapping default, explicitly out of
# scope for #540 (Non-Goals); every other deep-agent module must be
# literal-free so worktree runs stay hermetic.
ALLOWLIST = {"s5tools.py"}


def derived_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))


class TestNoHardcodedCheckout(unittest.TestCase):
    def test_no_production_literal(self):
        offenders = []
        for name in sorted(os.listdir(DEEP_AGENT)):
            if not name.endswith(".py") or name in ALLOWLIST:
                continue
            with open(os.path.join(DEEP_AGENT, name)) as fh:
                for lineno, line in enumerate(fh, 1):
                    if FORBIDDEN in line:
                        offenders.append(f"{name}:{lineno}")
        self.assertEqual(offenders, [])

    def test_dotenv_calls_use_helper(self):
        for name in ("resolve_conflicts.py", "triage_fanout.py"):
            with open(os.path.join(DEEP_AGENT, name)) as fh:
                tree = ast.parse(fh.read())
            calls = [
                node for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "load_dotenv"
            ]
            self.assertTrue(calls, f"{name}: no load_dotenv call")
            for call in calls:
                for arg in call.args:
                    self.assertNotIsInstance(
                        arg, ast.Constant,
                        f"{name}: hardcoded dotenv path")

    def test_chroma_dirs_use_helper(self):
        with open(os.path.join(DEEP_AGENT, "retrieval.py")) as fh:
            tree = ast.parse(fh.read())
        targets = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", "") == "DB_DIR"
                    for t in node.targets)
        ]
        self.assertEqual(len(targets), 1)
        self.assertNotIsInstance(targets[0].value, ast.Constant,
                                 "retrieval.DB_DIR is hardcoded")
        # ask_investigator no longer builds Chroma inline (#1113):
        # its search rides retrieval.search_td, so no chroma path
        # literal may remain (comments excluded — the exclusion-list
        # note still names the dir). Paths come from helpers.
        with open(os.path.join(DEEP_AGENT, "ask_investigator.py")) as fh:
            tree = ast.parse(fh.read())
        literals = [
            node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
        ]
        for lit in literals:
            lowered = lit.lower()
            for banned in ("chroma_db", "chroma.sqlite3",
                           "persist_directory", "s5-docs"):
                self.assertNotIn(
                    banned, lowered,
                    f"hardcoded chroma literal: {lit!r}")


class TestRepoRoot(unittest.TestCase):
    def test_derives_from_checkout(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("S5_CHECKOUT", None)
            self.assertEqual(cli.repo_root(), derived_root())

    def test_override_wins(self):
        with mock.patch.dict(os.environ, {"S5_CHECKOUT": "/tmp/fake-wt"}):
            self.assertEqual(cli.repo_root(), "/tmp/fake-wt")

    def test_blank_override_ignored(self):
        for blank in ("", "   "):
            with mock.patch.dict(os.environ, {"S5_CHECKOUT": blank}):
                self.assertEqual(cli.repo_root(), derived_root())


class TestRetrievalDbDir(unittest.TestCase):
    def _db_dir(self, env):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); "
             "import retrieval; print(retrieval.DB_DIR)" % DEEP_AGENT],
            capture_output=True, text=True, timeout=120, env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.strip()

    def test_follows_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, S5_CHECKOUT=tmp)
            self.assertEqual(
                self._db_dir(env),
                os.path.join(tmp, "deep-agent", "chroma_db"))

    def test_follows_checkout(self):
        env = dict(os.environ)
        env.pop("S5_CHECKOUT", None)
        self.assertEqual(
            self._db_dir(env), os.path.join(DEEP_AGENT, "chroma_db"))


if __name__ == "__main__":
    unittest.main()
