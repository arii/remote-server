"""@-mention file attach: listing endpoint + prompt injection (#653)."""

import base64
import inspect
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import chat
    from backend.api.routers import repo as repo_router
    from backend.core.repos import list_repo_files

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False


class FakeStdout(io.StringIO):
    def read(self, n=-1):
        return super().read(n)


class FakeProc:
    def __init__(self, text="", code=0):
        self.stdout = FakeStdout(text)
        self.returncode = code
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass


def _fixture_repos_dir(tmp):
    """Checkout fixture with listable, skippable, and pinnable files."""
    root = Path(tmp) / "demo"
    (root / ".git").mkdir(parents=True)
    (root / "README.md").write_text("hello readme\n")
    (root / "sub").mkdir()
    (root / "sub" / "a.py").write_text("x = 1\n")
    (root / "sub" / "b.py").write_text("y = 2\n")
    (root / "sub" / "c.py").write_text("z = 3\n")
    (root / "sub" / "d.py").write_text("w = 4\n")
    (root / ".hidden").write_text("no\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "x.js").write_text("no\n")
    (root / "sub" / "__pycache__").mkdir()
    (root / "sub" / "__pycache__" / "c.pyc").write_text("no\n")
    (root / "ledger").mkdir()
    (root / "ledger" / "l.jsonl").write_text("no\n")
    (root / "traces").mkdir()
    (root / "traces" / "t.jsonl").write_text("no\n")
    (root / "big.txt").write_text("y" * 9000)
    (root / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    return root


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class RepoFilesTest(unittest.TestCase):
    def _list(self, tmp, repo="demo", prefix="", **kwargs):
        with mock.patch("backend.core.repos.get_repos_dir",
                        return_value=Path(tmp)):
            return list_repo_files(repo, prefix, **kwargs)

    def test_lists_files_sorted_relative(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture_repos_dir(tmp)
            body = self._list(tmp)
        self.assertEqual(body["repo"], "demo")
        self.assertEqual(body["prefix"], "")
        self.assertIn("README.md", body["files"])
        self.assertIn("sub/a.py", body["files"])
        self.assertEqual(body["files"], sorted(body["files"]))
        self.assertFalse(body["truncated"])

    def test_skips_dotfiles_caches_and_machine_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture_repos_dir(tmp)
            body = self._list(tmp)
        joined = "\n".join(body["files"])
        self.assertNotIn(".hidden", joined)
        self.assertNotIn("node_modules", joined)
        self.assertNotIn("__pycache__", joined)
        self.assertNotIn("ledger/", joined)
        self.assertNotIn("traces/", joined)
        self.assertNotIn(".git/", joined)

    def test_prefix_filters_and_strips_leading_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture_repos_dir(tmp)
            body = self._list(tmp, prefix="sub/")
            slashed = self._list(tmp, prefix="/sub/")
        self.assertEqual(body["files"],
                         ["sub/a.py", "sub/b.py", "sub/c.py", "sub/d.py"])
        self.assertEqual(slashed["files"], body["files"])
        self.assertEqual(slashed["prefix"], "sub/")

    def test_cap_reports_truncation_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture_repos_dir(tmp)
            over = self._list(tmp, limit=2)
            exact = self._list(tmp, limit=7)
        self.assertEqual(len(over["files"]), 2)
        self.assertTrue(over["truncated"])
        self.assertFalse(exact["truncated"])
        self.assertEqual(len(exact["files"]), 7)

    def test_dotdot_prefix_matches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture_repos_dir(tmp)
            body = self._list(tmp, prefix="../demo")
        self.assertEqual(body["files"], [])
        self.assertFalse(body["truncated"])

    def test_bad_name_and_missing_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            _fixture_repos_dir(tmp)
            with self.assertRaises(ValueError):
                self._list(tmp, repo="a/b/c")
            with self.assertRaises(FileNotFoundError):
                self._list(tmp, repo="ghost")

    def test_files_endpoint_shape_and_errors(self):
        app = FastAPI()
        app.include_router(repo_router.router)
        client = TestClient(app)
        with tempfile.TemporaryDirectory() as tmp:
            _fixture_repos_dir(tmp)
            with mock.patch("backend.core.repos.get_repos_dir",
                            return_value=Path(tmp)):
                resp = client.get("/api/repo/files",
                                  params={"repo": "demo", "prefix": "sub/"})
                self.assertEqual(resp.status_code, 200)
                body = resp.json()
                self.assertEqual(body["repo"], "demo")
                self.assertEqual(body["files"],
                                 ["sub/a.py", "sub/b.py",
                                  "sub/c.py", "sub/d.py"])
                self.assertFalse(body["truncated"])
                resp = client.get("/api/repo/files",
                                  params={"repo": "a/b/c"})
                self.assertEqual(resp.status_code, 400)
                resp = client.get("/api/repo/files",
                                  params={"repo": "ghost"})
                self.assertEqual(resp.status_code, 404)


class ParseMentionsTest(unittest.TestCase):
    def test_basic_and_multiples_deduped_in_order(self):
        self.assertEqual(chat.parse_mentions("see @a.py and @b/c.md"),
                         ["a.py", "b/c.md"])
        self.assertEqual(chat.parse_mentions("@a.py then @a.py again"),
                         ["a.py"])

    def test_midword_at_never_matches(self):
        self.assertEqual(chat.parse_mentions("mail user@example.com ok"),
                         [])
        self.assertEqual(chat.parse_mentions("a@b"), [])
        self.assertEqual(chat.parse_mentions("@@z"), [])

    def test_trailing_punctuation_and_leading_slash(self):
        self.assertEqual(chat.parse_mentions("end @u.py."), ["u.py"])
        self.assertEqual(chat.parse_mentions("(@p), ok"), ["p"])
        self.assertEqual(chat.parse_mentions("x @/lead.py"), ["lead.py"])

    def test_bare_at_matches_nothing(self):
        self.assertEqual(chat.parse_mentions("@"), [])
        self.assertEqual(chat.parse_mentions("hi @ there"), [])
        self.assertEqual(chat.parse_mentions(""), [])


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class InjectMentionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _fixture_repos_dir(self.tmp)
        self._patcher = mock.patch(
            "backend.core.repos.get_repos_dir", return_value=Path(self.tmp))
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    def test_no_mentions_returns_plain_capped_prompt(self):
        self.assertEqual(chat.inject_mentions("plain prompt", "demo"),
                         "plain prompt")
        self.assertEqual(chat.inject_mentions(
            "x" * 4000, "demo"), "x" * chat.CHAT_MAX_PROMPT)

    def test_content_block_prepended_before_user_text(self):
        out = chat.inject_mentions("look at @sub/a.py please", "demo")
        self.assertTrue(out.startswith("--- @sub/a.py ---\nx = 1\n"))
        self.assertTrue(out.endswith("\n\nlook at @sub/a.py please"))

    def test_missing_file_yields_visible_line_turn_proceeds(self):
        out = chat.inject_mentions("look at @nope.txt please", "demo")
        self.assertIn("[@nope.txt: not found]", out)
        self.assertIn("look at @nope.txt please", out)

    def test_escape_and_directory_rejected_visibly(self):
        out = chat.inject_mentions("x @../evil y", "demo")
        self.assertIn("[@../evil: invalid path]", out)
        out = chat.inject_mentions("x @sub y", "demo")
        self.assertIn("[@sub: not a file]", out)

    def test_binary_file_reads_unreadable(self):
        out = chat.inject_mentions("x @blob.bin y", "demo")
        self.assertIn("[@blob.bin: unreadable]", out)

    def test_per_file_cap_marks_truncation(self):
        from backend.core.repos import get_checkout_path
        text, reason = chat.read_mention_file(
            get_checkout_path("demo"), "big.txt")
        self.assertIsNone(reason)
        assert text is not None
        self.assertIn("[truncated: showing first 8192 of 9000 bytes]",
                      text)
        # The 8KB pin then also trips the turn budget (attachments
        # first, tail cut with a marker).
        out = chat.inject_mentions("x @big.txt y", "demo")
        self.assertLessEqual(len(out), chat.CHAT_MAX_PROMPT)
        self.assertTrue(out.startswith("--- @big.txt ---"))
        self.assertIn("truncated to fit 3000 chars", out)

    def test_max_files_with_skipped_summary(self):
        out = chat.inject_mentions(
            "@sub/a.py @sub/b.py @sub/c.py @sub/d.py go", "demo")
        self.assertIn("--- @sub/a.py ---", out)
        self.assertIn("--- @sub/c.py ---", out)
        self.assertNotIn("--- @sub/d.py ---", out)
        self.assertIn("[+1 more @-mention(s) skipped: "
                      "max 3 files per send]", out)

    def test_total_budget_marks_tail_cut(self):
        out = chat.inject_mentions(
            "look at @big.txt " + "please " * 800, "demo")
        self.assertLessEqual(len(out), chat.CHAT_MAX_PROMPT)
        self.assertIn("truncated to fit 3000 chars: attachments first",
                      out)

    def test_unknown_repo_returns_plain_prompt(self):
        self.assertEqual(chat.inject_mentions("see @a.py", "ghost"),
                         "see @a.py")


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class MentionRouteTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self.tmpdir, "ledger.jsonl")
        app = FastAPI()
        app.include_router(chat.router)
        self.client = TestClient(app)
        self._orig_repo = chat.get_repo_info
        self._orig_spawn = chat.spawn_chat_process
        self._orig_venv = chat.deep_agent_python
        self._orig_script = chat.deep_agent_script
        chat.get_repo_info = lambda repo: {"gh": "arii/demo",
                                           "td_cwd": "/srv/repos/demo"}
        chat.deep_agent_python = lambda: os.path.abspath(chat.__file__)
        chat.deep_agent_script = lambda name: os.path.abspath(chat.__file__)
        self.fixture = tempfile.mkdtemp()
        self.root = _fixture_repos_dir(self.fixture)
        self._patcher = mock.patch(
            "backend.core.repos.get_repos_dir",
            return_value=Path(self.fixture))
        self._patcher.start()

    def tearDown(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger
        chat.get_repo_info = self._orig_repo
        chat.spawn_chat_process = self._orig_spawn
        chat.deep_agent_python = self._orig_venv
        chat.deep_agent_script = self._orig_script
        self._patcher.stop()

    def _sent_prompt(self, argv):
        return base64.b64decode(argv[4]).decode()

    def test_post_resolves_mentions_into_agent_prompt(self):
        argvs = []
        chat.spawn_chat_process = lambda cmd: (
            argvs.append(cmd), FakeProc("ok"))[1]
        resp = self.client.post(
            "/api/chat/stream",
            json={"prompt": "look at @sub/a.py please", "repo": "demo",
                  "session_id": "mention00001"})
        self.assertEqual(resp.status_code, 200)
        sent = self._sent_prompt(argvs[-1])
        self.assertTrue(sent.startswith("--- @sub/a.py ---\nx = 1\n"))
        self.assertIn("look at @sub/a.py please", sent)

    def test_get_leaves_mentions_literal(self):
        argvs = []
        chat.spawn_chat_process = lambda cmd: (
            argvs.append(cmd), FakeProc("ok"))[1]
        resp = self.client.get(
            "/api/chat/stream",
            params={"prompt": "look at @sub/a.py please", "repo": "demo",
                    "session_id": "mention00002"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._sent_prompt(argvs[-1]),
                         "look at @sub/a.py please")

    def test_post_keeps_user_words_in_ledger(self):
        chat.spawn_chat_process = lambda cmd: FakeProc("ok")
        resp = self.client.post(
            "/api/chat/stream",
            json={"prompt": "look at @sub/a.py please", "repo": "demo",
                  "session_id": "mention00003"})
        self.assertEqual(resp.status_code, 200)
        with open(os.path.join(self.tmpdir, "ledger.jsonl")) as handle:
            lines = handle.read()
        self.assertIn("look at @sub/a.py please", lines)
        self.assertNotIn("--- @sub/a.py ---", lines)

    def test_route_hint_takes_no_prompt_params(self):
        params = list(inspect.signature(chat.chat_route_hint).parameters)
        self.assertEqual(params, ["prefer"])


if __name__ == "__main__":
    unittest.main()
