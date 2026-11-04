"""deep-agent/cli.py helper tests + CLI contract pins for entrypoints.

Proves the cli.py rewire changed no behavior: exact stdout/stderr/exit
codes for every argv-error and Sorry path, helper unit tests, and the
stdlib-only import contract (no langchain needed for cli itself).

System python runs everything except the venv-gated cases (entrypoints
that import langchain at module top); those run under
deep-agent/.venv/bin/python like test_chat_agent.py.
Run: python3 tests/test_cli.py
"""
import base64
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import cli
import deep_chat
import eval_ask
import eval_chat

DEEP_AGENT = os.path.join(os.path.dirname(__file__), "..", "deep-agent")

try:
    import langchain  # noqa: F401
    import dotenv  # noqa: F401
    import deepagents  # noqa: F401
    HAS_AGENT_DEPS = True
except ImportError:
    HAS_AGENT_DEPS = False


def b64(text):
    return base64.b64encode(text.encode()).decode()


def run_script(name, *args, env=None):
    script = os.path.join(DEEP_AGENT, name)
    return subprocess.run(
        [sys.executable, script, *args],
        capture_output=True, text=True, timeout=120, env=env,
    )


class TestBootstrap(unittest.TestCase):
    def test_adds_own_dir_once(self):
        want = os.path.dirname(os.path.abspath(cli.__file__))
        # Full-suite isolation: other modules (e.g. build_index)
        # insert the same entry at import time depending on
        # listdir order, so start from a clean slate.
        saved = list(sys.path)

        def restore():
            sys.path[:] = saved

        self.addCleanup(restore)
        sys.path[:] = [p for p in sys.path if p != want]
        cli.bootstrap()
        cli.bootstrap()
        self.assertEqual(sys.path.count(want), 1)

    def test_own_dir_is_deep_agent(self):
        want = os.path.dirname(os.path.abspath(cli.__file__))
        self.assertTrue(os.path.isfile(os.path.join(want, "s5tools.py")))
        self.assertTrue(os.path.isfile(os.path.join(want, "ledger.py")))


class TestUsageError(unittest.TestCase):
    def test_single_line(self):
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            code = cli.usage_error("usage: foo.py <x>")
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue(), "usage: foo.py <x>\n")
        self.assertEqual(out.getvalue(), "")

    def test_multi_line_in_order(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = cli.usage_error("line one", "line two", "line three")
        self.assertEqual(code, 2)
        self.assertEqual(
            err.getvalue(), "line one\nline two\nline three\n")


class TestSorry(unittest.TestCase):
    def test_stdout_exit_1(self):
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            code = cli.sorry("Sorry, nothing found.")
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "Sorry, nothing found.\n")
        self.assertEqual(err.getvalue(), "")


class TestB64Decode(unittest.TestCase):
    def test_round_trip(self):
        self.assertEqual(cli.b64decode(b64("hello there")), "hello there")

    def test_non_alphabet_chars_ignored_like_b64decode(self):
        # "!!!" has no alphabet chars -> b"" -> ""; callers still
        # reject it downstream as an empty payload.
        self.assertEqual(cli.b64decode("!!!"), "")

    def test_bad_padding_is_none(self):
        self.assertIsNone(cli.b64decode("a"))

    def test_non_utf8_is_none(self):
        raw = base64.b64encode(b"\xff\xfe\x00bad").decode()
        self.assertIsNone(cli.b64decode(raw))

    def test_non_str_is_none(self):
        self.assertIsNone(cli.b64decode(None))
        self.assertIsNone(cli.b64decode(b64("x").encode()))
        self.assertIsNone(cli.b64decode(123))


class TestSearchDocsArgv(unittest.TestCase):
    def test_no_args_usage(self):
        p = run_script("search_docs.py")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(
            p.stderr, 'usage: search_docs.py "<question>" [k] [repo]\n')
        self.assertEqual(p.stdout, "")

    def test_bad_k(self):
        p = run_script("search_docs.py", "some question", "x")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "k must be an integer\n")

    def test_zero_k(self):
        p = run_script("search_docs.py", "some question", "0")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "k must be a positive integer\n")

    def test_bad_repo(self):
        p = run_script("search_docs.py", "some question", "3", "??")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "repo must look like owner/name\n")


class TestLedgerArgv(unittest.TestCase):
    def test_bad_n(self):
        p = run_script("ledger.py", "abc")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "n must be an integer\n")
        self.assertEqual(p.stdout, "")


class TestRunsArgv(unittest.TestCase):
    def test_unknown_ref_sorry(self):
        p = run_script("runs.py", "definitely-no-such-run-xyz")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(
            p.stdout, "Sorry, no run found for "
            "'definitely-no-such-run-xyz'.\n")
        self.assertEqual(p.stderr, "")


class TestDeepChatArgv(unittest.TestCase):
    def test_usage_exact(self):
        p = run_script("deep_chat.py")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(
            p.stderr,
            "usage: deep_chat.py owner/repo <thread-id> <b64-prompt>"
            " [--prefer=ollama|nebius]\n"
            "   or: deep_chat.py owner/repo <thread-id> --resume "
            "approve|reject [b64-message]\n"
            "   or: deep_chat.py owner/repo --supervise\n"
            "   or: deep_chat.py --validate-proposal <b64-json>\n")
        self.assertEqual(p.stdout, "")

    def test_resume_bad_verdict_text(self):
        p = run_script("deep_chat.py", "arii/remote-server", "t1",
                       "--resume", "maybe")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "bad verdict 'maybe': "
                         "want 'approve'/'reject'\n")

    def test_resume_bad_message(self):
        p = run_script("deep_chat.py", "arii/remote-server", "t1",
                       "--resume", "approve", "a")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "bad base64 message\n")

    def test_turn_no_model_sorry(self):
        out = io.StringIO()
        with mock.patch.object(deep_chat, "build_model",
                               side_effect=RuntimeError("ollama down")), \
                redirect_stdout(out):
            code = deep_chat._cmd_turn(
                "arii/remote-server", "t-sorry", b64("hi"))
        self.assertEqual(code, 1)
        self.assertEqual(
            out.getvalue(),
            "Sorry, no model is available right now: ollama down\n")

    def test_resume_no_model_sorry(self):
        out = io.StringIO()
        with mock.patch.object(deep_chat, "build_model",
                               side_effect=RuntimeError("ollama down")), \
                redirect_stdout(out):
            code = deep_chat._cmd_resume(
                "arii/remote-server", "t-sorry", "approve", None)
        self.assertEqual(code, 1)
        self.assertEqual(
            out.getvalue(),
            "Sorry, no model is available right now: ollama down\n")


class TestEvalArgv(unittest.TestCase):
    def test_eval_ask_json_needs_path(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = eval_ask.main(["--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue(), "error: --json needs a path\n")

    def test_eval_chat_json_needs_path(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = eval_chat.main(["--json"])
        self.assertEqual(code, 2)
        self.assertEqual(err.getvalue(), "error: --json needs a path\n")


@unittest.skipUnless(HAS_AGENT_DEPS, "needs deep-agent venv")
class TestVenvEntrypointArgv(unittest.TestCase):
    def test_chat_agent_usage_exact(self):
        p = run_script("chat_agent.py")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(
            p.stderr,
            "usage: chat_agent.py owner/repo <session-id> <b64-prompt>"
            " [b64-history]\n")
        self.assertEqual(p.stdout, "")

    def test_chat_agent_no_model_sorry(self):
        import chat_agent

        argv = ["chat_agent.py", "arii/remote-server", "s1", b64("hi")]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(chat_agent.model_router, "resolve_llm",
                                  side_effect=RuntimeError("ollama down")), \
                redirect_stdout(out):
            code = chat_agent.main()
        self.assertEqual(code, 1)
        self.assertEqual(
            out.getvalue(),
            "Sorry, no model is available right now: ollama down\n")

    def test_ask_investigator_usage_exact(self):
        p = run_script("ask_investigator.py")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(
            p.stderr,
            "usage: ask_investigator.py owner/repo <b64-instruction>\n")
        self.assertEqual(p.stdout, "")

    def test_ask_investigator_bad_instruction(self):
        p = run_script("ask_investigator.py", "arii/remote-server",
                       b64("x" * 2001))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "bad instruction\n")

    def test_ask_investigator_bad_base64(self):
        p = run_script("ask_investigator.py", "arii/remote-server", "a")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "bad base64 instruction\n")

    def test_resolve_conflicts_usage(self):
        p = run_script("resolve_conflicts.py")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(
            p.stderr,
            "usage: resolve_conflicts.py <worktree-path> [base-ref]\n")

    def test_triage_explainer_usage(self):
        p = run_script("triage_explainer.py")
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "usage: triage_explainer.py owner/repo\n")

    def test_triage_fanout_bad_repos(self):
        env = dict(os.environ, NEBIUS_API_KEY="dummy")
        p = run_script("triage_fanout.py", "nope", env=env)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(p.stderr, "bad repo(s): ['nope']\n")


if __name__ == "__main__":
    unittest.main()
