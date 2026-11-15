"""Turn engine tests (D2/D4): shared timeouts, heartbeat, kill ladder.

Covers the env contract of resolve_timeout, the kill-escalation
ladders (sync + async), the streamed chat heartbeat (visible
liveness, still abortable, timeout still enforced), chat/agent
parity on the shared resolver, and the single-source model name
(grep proves no second default in non-test code).
"""

import asyncio
import io
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from backend.exec import agent_turn
from backend.exec import turn_engine

try:
    from backend.api.routers import chat

    HAS_CHAT = True
except ImportError:  # pragma: no cover - fastapi missing
    HAS_CHAT = False


class ResolveTimeoutTests(unittest.TestCase):
    def test_positive_env_wins(self):
        with mock.patch.dict(os.environ, {"TE_TEST": "42"}):
            self.assertEqual(turn_engine.resolve_timeout(9, "TE_TEST"), 42)

    def test_unset_blank_garbage_nonpositive_fall_back(self):
        for label, env in [
            ("unset", {}),
            ("blank", {"TE_TEST": ""}),
            ("space", {"TE_TEST": "  "}),
            ("garbage", {"TE_TEST": "x1"}),
            ("zero", {"TE_TEST": "0"}),
            ("negative", {"TE_TEST": "-3"}),
        ]:
            with self.subTest(label=label):
                with mock.patch.dict(os.environ, env, clear=True):
                    self.assertEqual(turn_engine.resolve_timeout(9, "TE_TEST"), 9)

    def test_same_resolver_module_serves_both_runners(self):
        # Parity (D4): one env contract, one implementation — the
        # runners differ only in their default budget.
        from backend.api.routers import chat as chat_module

        self.assertIs(agent_turn.turn_engine, turn_engine)
        self.assertIs(chat_module.turn_engine, turn_engine)


class SyncStopProcTests(unittest.TestCase):
    """The sync kill ladder, exercised on fake children."""

    def _fake(self, ignore_term=False, exited=False):
        class P:
            def __init__(self):
                self.terminated = False
                self.killed = False

            def poll(self):
                if exited:
                    return 0
                # A SIGTERM-ignoring child keeps running until killed;
                # a normal child exits on terminate.
                if ignore_term:
                    return 0 if self.killed else None
                return 0 if (self.terminated or self.killed) else None

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                if ignore_term and not self.killed:
                    raise subprocess.TimeoutExpired("fake", timeout)
                return 0

        return P()

    def test_terminate_is_enough(self):
        proc = self._fake()
        turn_engine.sync_stop_proc(proc, grace=0.01)
        self.assertTrue(proc.terminated)
        self.assertFalse(proc.killed)

    def test_ignoring_child_escalates_to_kill(self):
        proc = self._fake(ignore_term=True)
        turn_engine.sync_stop_proc(proc, grace=0.01)
        self.assertTrue(proc.killed)

    def test_already_exited_is_a_noop(self):
        proc = self._fake(exited=True)
        turn_engine.sync_stop_proc(proc, grace=0.01)
        self.assertFalse(proc.terminated)


class AsyncStopProcTests(unittest.TestCase):
    def test_async_ladder_escalates(self):
        class P:
            def __init__(self):
                self.terminated = False
                self.killed = False

            def poll(self):
                return 0 if self.killed else None

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                if not self.killed:
                    raise subprocess.TimeoutExpired("fake", timeout)
                return 0

        proc = P()
        asyncio.run(turn_engine.async_stop_proc(proc, grace=0.01))
        self.assertTrue(proc.killed)


class _SilentProc:
    """A child whose stdout blocks forever (the lock-up shape)."""

    def __init__(self, block):
        self.stdout = io.StringIO()
        self._orig_read = self.stdout.read

        def blocking_read(n=-1):
            block.wait(timeout=10)
            return ""

        self.stdout.read = blocking_read
        self.returncode = 0
        self.terminated = False

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True


@unittest.skipUnless(HAS_CHAT, "fastapi not importable")
class HeartbeatStreamTests(unittest.TestCase):
    """D2: a slow local turn streams visible liveness, not a hang."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp, "ledger.jsonl")

    def tearDown(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def _sse_events(self, text):
        events = []
        for frame in [f for f in text.split("\n\n") if f.strip()]:
            name = None
            data = {}
            for line in frame.splitlines():
                if line.startswith("event: "):
                    name = line[len("event: ") :]
                elif line.startswith("data: "):
                    data = json.loads(line[len("data: ") :])
            events.append((name, data))
        return events

    def _stream_patches(self, proc):
        """Patch the early-exit gates _chat_events checks before the
        read loop: repo lookup, runtime paths, ladder probe."""
        import contextlib

        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch.object(
                chat,
                "get_repo_info",
                lambda name: {"gh": f"arii/{name}", "td_cwd": "/tmp"},
            )
        )
        stack.enter_context(
            mock.patch.object(
                chat, "deep_agent_python", lambda: os.path.abspath(chat.__file__)
            )
        )
        stack.enter_context(
            mock.patch.object(
                chat, "deep_agent_script", lambda name: os.path.abspath(chat.__file__)
            )
        )
        stack.enter_context(
            mock.patch.object(
                chat.model_router,
                "resolve_llm",
                lambda prefer=None: {"provider": "ollama"},
            )
        )
        stack.enter_context(
            mock.patch.object(chat, "spawn_chat_process", lambda argv: proc)
        )
        return stack

    def test_silent_child_streams_heartbeats_then_times_out(self):
        block = threading.Event()
        proc = _SilentProc(block)
        try:
            with (
                mock.patch.object(chat, "CHAT_TURN_TIMEOUT", 0.3),
                mock.patch.object(turn_engine, "HEARTBEAT_INTERVAL", 0.05),
            ):
                with self._stream_patches(proc):
                    text = "".join(
                        chat._chat_events("hi", "remote-server", "beat000001")
                    )
        finally:
            block.set()
        events = self._sse_events(text)
        names = [name for name, _ in events]
        self.assertIn("heartbeat", names)
        self.assertEqual(names[-1], "error")
        self.assertIn("timed out", events[-1][1]["error"])
        # Heartbeats carry the liveness payload the drawer renders.
        for name, data in events:
            if name == "heartbeat":
                self.assertIn("elapsed", data)
                self.assertGreaterEqual(data["budget_left"], 0)

    def test_abort_lands_within_one_heartbeat_slice(self):
        # The heartbeat slices re-check the abort flag each iteration:
        # an abort raised while the child is silent still kills the
        # child instead of waiting out the turn budget.
        block = threading.Event()
        proc = _SilentProc(block)

        def stream():
            for frame in chat._chat_events("hi", "remote-server", "abortbeat001"):
                if frame.startswith("event: heartbeat"):
                    # Mirror what the abort endpoint does: flag the
                    # session; the loop re-checks it every slice.
                    with chat.chat_sessions_lock:
                        sess = chat.chat_sessions.get("abortbeat001")
                        if sess is not None:
                            sess["aborted"] = True
                yield frame

        try:
            with (
                mock.patch.object(chat, "CHAT_TURN_TIMEOUT", 30),
                mock.patch.object(turn_engine, "HEARTBEAT_INTERVAL", 0.02),
            ):
                with self._stream_patches(proc):
                    text = "".join(stream())
        finally:
            block.set()
        events = self._sse_events(text)
        names = [name for name, _ in events]
        self.assertIn("heartbeat", names)
        # Aborted, not run to the 30s timeout: the final phase marks it.
        phases = [data.get("phase") for name, data in events if name == "phase"]
        self.assertIn("aborted", phases)
        self.assertTrue(proc.terminated)


class TurnTimeoutParityTests(unittest.TestCase):
    """D4: the chat and agent runners resolve budgets the same way."""

    def test_chat_turn_timeout_env_override(self):
        with mock.patch.dict(os.environ, {"CHAT_TURN_TIMEOUT": "901"}):
            self.assertEqual(chat.chat_turn_timeout(), 901)

    def test_chat_turn_timeout_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(chat.chat_turn_timeout(), chat.CHAT_TURN_TIMEOUT)

    def test_agent_turn_timeout_env_override(self):
        with mock.patch.dict(os.environ, {"AGENT_TURN_TIMEOUT": "77"}):
            self.assertEqual(agent_turn.turn_timeout(), 77)

    def test_agent_turn_timeout_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(agent_turn.turn_timeout(), agent_turn.TURN_TIMEOUT)

    def test_kill_grace_single_source(self):
        self.assertEqual(chat.CHAT_KILL_GRACE, turn_engine.KILL_GRACE)
        self.assertEqual(agent_turn.KILL_GRACE, turn_engine.KILL_GRACE)

    def test_agent_stop_proc_delegates_to_shared_ladder(self):
        # D4: the one-shot runner no longer hand-mirrors the SIGTERM
        # → SIGKILL escalation; it calls the shared async ladder.
        with mock.patch.object(
            turn_engine, "async_stop_proc", mock.AsyncMock()
        ) as ladder:
            asyncio.run(agent_turn._stop_proc(object()))
        ladder.assert_awaited_once()
        self.assertEqual(ladder.call_args.args[1], agent_turn.KILL_GRACE)

    def test_td_run_timeout_env_override(self):
        from backend import proposals

        with mock.patch.dict(os.environ, {"TD_RUN_TIMEOUT": "333"}):
            self.assertEqual(proposals.td_run_timeout(), 333)

    def test_td_run_timeout_falls_back_to_default(self):
        from backend import proposals

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(proposals.td_run_timeout(), 120)

    def test_td_run_resolves_timeout_at_call_time(self):
        # The default budget honors the env at call time, not at
        # import time — a deploy flips it without restarting imports.
        from backend.exec import engine

        seen = {}

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        class Runner:
            @staticmethod
            def run(argv, timeout=None, **kw):
                seen["timeout"] = timeout
                return FakeResult()

        engine.td_run("td list", runner=Runner)
        self.assertEqual(seen["timeout"], 120)
        with mock.patch.dict(os.environ, {"TD_RUN_TIMEOUT": "77"}):
            engine.td_run("td list", runner=Runner)
        self.assertEqual(seen["timeout"], 77)


class ModelNameSingleSourceTests(unittest.TestCase):
    """D2: qwen3:8b is defined exactly once in non-test code."""

    MODEL_LIT = re.compile(r"=\s*[\"'][^\"']*qwen3:[^\"']*[\"']")

    def _py_sources(self):
        for base in (Path("backend"), Path("deep-agent")):
            for path in base.rglob("*.py"):
                if "/tests/" in str(path):
                    continue  # test fixtures assert expected values
                yield path

    def test_no_second_default_assignment(self):
        offenders = [
            str(path)
            for path in self._py_sources()
            if path.name != "turn_engine.py"
            and any(
                self.MODEL_LIT.search(line)
                and not line.strip().startswith(("#", '"""', "'''"))
                for line in path.read_text().splitlines()
            )
        ]
        self.assertEqual(offenders, [], f"second model default in: {offenders}")

    def test_engine_default_is_qwen3_8b(self):
        self.assertEqual(turn_engine.DEFAULT_MODEL, "qwen3:8b")

    def test_ask_model_default_comes_from_turn_engine(self):
        import model_router

        self.assertEqual(
            model_router._default_ollama_model(), turn_engine.DEFAULT_MODEL
        )

    def test_briefing_narrator_default_comes_from_turn_engine(self):
        from backend.api.routers import briefing

        self.assertEqual(
            briefing.DEFAULT_NARRATOR_OLLAMA_MODEL, turn_engine.DEFAULT_MODEL
        )


class ValidationSingleSourceTests(unittest.TestCase):
    """D4: the git-shape validators exist exactly once."""

    def _py_sources(self):
        for base in (Path("backend"), Path("deep-agent")):
            for path in base.rglob("*.py"):
                yield path

    def test_no_mirrored_branch_regex(self):
        offenders = [
            str(path)
            for path in self._py_sources()
            if "/tests/" not in str(path)
            and path.name != "validate.py"
            and re.search(r"BRANCH_RE\s*=\s*re\.compile", path.read_text())
        ]
        self.assertEqual(offenders, [], f"mirrored branch regex in: {offenders}")

    def test_no_mirrored_pr_number_logic(self):
        offenders = [
            str(path)
            for path in self._py_sources()
            if "/tests/" not in str(path)
            and path.name != "validate.py"
            and "not isinstance(value, bool) and value > 0" in path.read_text()
        ]
        self.assertEqual(offenders, [], f"mirrored PR-number logic in: {offenders}")

    def test_runners_delegate_to_the_shared_module(self):
        import backend.proposals as proposals
        import deep_chat
        from backend.exec.validate import valid_branch, valid_pr_number

        for checker, fn in (
            (deep_chat._valid_branch, valid_branch),
            (proposals._valid_branch, valid_branch),
        ):
            self.assertEqual(fn.__module__, "backend.exec.validate")
            # Behavior parity through the delegates.
            self.assertEqual(bool(checker("feat/runner-core")), True)
            self.assertEqual(bool(checker("bad..name")), False)
        for checker, fn in (
            (deep_chat._valid_pr_number, valid_pr_number),
            (proposals._valid_pr_number, valid_pr_number),
        ):
            self.assertEqual(fn.__module__, "backend.exec.validate")
            self.assertEqual(bool(checker(7)), True)
            self.assertEqual(bool(checker(0)), False)

    def test_valid_branch_contract(self):
        from backend.exec.validate import valid_branch

        for good in ("main", "feat/runner-core", "a.b.c", "release/2026-11-13"):
            self.assertTrue(valid_branch(good), good)
        for bad in ("", None, "a..b", "/leading", ".hidden", "-dash", "x" * 121, 7):
            self.assertFalse(valid_branch(bad), repr(bad))

    def test_valid_pr_number_contract(self):
        from backend.exec.validate import valid_pr_number

        self.assertTrue(valid_pr_number(1))
        self.assertTrue(valid_pr_number(1305))
        for bad in (0, -1, "7", True, False, None, 1.0):
            self.assertFalse(valid_pr_number(bad), repr(bad))


if __name__ == "__main__":
    unittest.main()
