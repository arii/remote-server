"""Tests for headless provider-CLI turns (stdlib unittest only).

No real agent binary runs here: run_turn tests use /bin/echo and
/bin/sleep via the argv seam, and router tests patch turn_argv to
return /bin/echo.
"""

import asyncio
import json
import os
import re
import tempfile
import unittest
from unittest import mock

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import agent

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False

from backend.exec import agent_turn

SID_RE = re.compile(r"^[0-9a-f]{12}$")
TURN_BODY = {"provider": "muse", "repo": "testrepo",
             "prompt": "hello", "intent": "agent-spawn",
             "confirmed": True}


def _patch_repo(test):
    import backend.core as core

    test._orig = (core.validate_repo_name, core.get_repo_status,
                  core.resolve_repo_path)
    core.validate_repo_name = lambda name: name
    core.get_repo_status = lambda name: {"repo": name}
    core.resolve_repo_path = lambda name: __import__(
        "pathlib").Path("/tmp") / name
    test.addCleanup(setattr, core, "validate_repo_name",
                    test._orig[0])
    test.addCleanup(setattr, core, "get_repo_status", test._orig[1])
    test.addCleanup(setattr, core, "resolve_repo_path",
                    test._orig[2])


class TurnArgvTest(unittest.TestCase):
    def test_allowlist_is_headless_capable_only(self):
        self.assertEqual(set(agent_turn.PROVIDERS), {"muse", "agy"})

    def test_rejects_unknown(self):
        with self.assertRaises(ValueError):
            agent_turn.turn_argv("pi", "/srv/x", "s", "hi", False)

    def test_muse_exec_session_argv(self):
        argv = agent_turn.turn_argv("muse", "/srv/x", "abc123", "hi",
                                   False)
        self.assertEqual(argv[-5:], ["muse", "exec", "--session-id",
                                      "abc123", "hi"])
        self.assertIn("terminal", argv)
        self.assertIn("-T", argv)

    def test_muse_followup_keeps_session(self):
        first = agent_turn.turn_argv("muse", "/srv/x", "abc123", "hi",
                                     False)
        second = agent_turn.turn_argv("muse", "/srv/x", "abc123",
                                      "again", True)
        self.assertEqual(first[-4:-1], second[-4:-1])

    def test_agy_first_turn_print(self):
        argv = agent_turn.turn_argv("agy", "/srv/x", "s", "hi", False)
        self.assertEqual(argv[-2:], ["-p", "hi"])
        self.assertNotIn("--continue", argv)

    def test_agy_followup_continues(self):
        argv = agent_turn.turn_argv("agy", "/srv/x", "s", "again",
                                   True)
        self.assertEqual(argv[-3:], ["--continue", "-p", "again"])

    def test_prompt_is_single_argv_element(self):
        argv = agent_turn.turn_argv("muse", "/srv/x", "s",
                                   "a; rm -rf /", False)
        self.assertIn("a; rm -rf /", argv)


class RunTurnTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self.tmp.name, "ledger.jsonl")
        self.addCleanup(self._restore_ledger)
        self.mgr = agent_turn.AgentTurnManager()

    def _restore_ledger(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def _collect(self, **kw):
        async def go():
            return [c async for c in self.mgr.run_turn(**kw)]
        return asyncio.run(go())

    def test_streams_stdout_chunks(self):
        chunks = self._collect(
            provider="muse", repo="r", cwd="/tmp",
            session_id="s1", prompt="hi", continued=False,
            argv=["/bin/echo", "hello"])
        self.assertEqual("".join(chunks), "hello\n")

    def test_result_records_exit_zero(self):
        self._collect(provider="muse", repo="r", cwd="/tmp",
                      session_id="s2", prompt="hi", continued=False,
                      argv=["/bin/echo", "hello"])
        result = self.mgr.pop_result("s2")
        self.assertEqual(result["exit"], 0)
        self.assertIsNone(self.mgr.pop_result("s2"))

    def test_result_records_failure_and_stderr(self):
        self._collect(provider="muse", repo="r", cwd="/tmp",
                      session_id="s3", prompt="hi", continued=False,
                      argv=["/bin/sh", "-c",
                             "echo oops >&2; exit 3"])
        result = self.mgr.pop_result("s3")
        self.assertEqual(result["exit"], 3)
        self.assertIn("oops", result["stderr"])

    def test_ledgers_completion(self):
        self._collect(provider="agy", repo="r", cwd="/tmp",
                      session_id="s4", prompt="hi", continued=True,
                      argv=["/bin/echo", "yo"])
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            entries = [json.loads(line) for line in fh
                       if line.strip()]
        kinds = [e.get("kind") for e in entries]
        self.assertIn("agent-turn", kinds)

    def test_abort_kills_inflight(self):
        async def go():
            task = asyncio.create_task(self._agen(
                session_id="s5", argv=["/bin/sleep", "30"]))
            await asyncio.sleep(0.3)
            killed = await self.mgr.abort("s5")
            await task
            return killed
        self.assertTrue(asyncio.run(go()))

    async def _agen(self, **kw):
        async for _ in self.mgr.run_turn(
                provider="muse", repo="r", cwd="/tmp",
                prompt="hi", continued=False, **kw):
            pass

    def test_abort_unknown_is_false(self):
        self.assertFalse(asyncio.run(self.mgr.abort("nope")))

    def test_hung_turn_times_out_and_reaps(self):
        with mock.patch.object(agent_turn, "TURN_TIMEOUT", 1), \
                mock.patch.object(agent_turn, "KILL_GRACE", 1):
            chunks = self._collect(
                provider="muse", repo="r", cwd="/tmp",
                session_id="s7", prompt="hi", continued=False,
                argv=["/bin/sleep", "30"])
        self.assertEqual(chunks, [])
        result = self.mgr.pop_result("s7")
        self.assertTrue(result["timed_out"])
        self.assertFalse(result["truncated"])

    def test_runaway_output_is_capped_and_killed(self):
        with mock.patch.object(agent_turn, "TURN_OUTPUT_MAX", 16):
            chunks = self._collect(
                provider="muse", repo="r", cwd="/tmp",
                session_id="s6", prompt="hi", continued=False,
                argv=["/bin/sh", "-c",
                     "yes ABCDEFGH | head -c 100000"])
        self.assertEqual(len("".join(chunks)), 16)
        result = self.mgr.pop_result("s6")
        self.assertTrue(result["truncated"])


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class AgentRouterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self.tmp.name, "ledger.jsonl")
        self.addCleanup(self._restore_ledger)
        _patch_repo(self)
        app = FastAPI()
        app.include_router(agent.router)
        self.client = TestClient(app)

    def _restore_ledger(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger

    def test_providers_lists_allowlist(self):
        resp = self.client.get("/api/agent/providers")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(set(resp.json()["providers"]),
                         {"muse", "agy"})

    def test_turn_rejects_unknown_provider(self):
        body = dict(TURN_BODY, provider="pi")
        resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 400)

    def test_turn_rejects_empty_prompt(self):
        body = dict(TURN_BODY, prompt="  ")
        resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 400)

    def test_new_conversation_requires_confirm(self):
        body = dict(TURN_BODY)
        body.pop("confirmed")
        resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 400)

    def test_turn_streams_token_and_done(self):
        with mock.patch.object(
                agent_turn, "turn_argv",
                return_value=["/bin/echo", "AGY-OK"]):
            resp = self.client.post("/api/agent/turn",
                                    json=TURN_BODY)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream",
                      resp.headers["content-type"])
        body = resp.text
        self.assertIn('event: token', body)
        self.assertIn('AGY-OK', body)
        done = [json.loads(line[6:]) for line in
                body.splitlines()
                if line.startswith("data: ")][-1]
        self.assertIn("AGY-OK", done["text"])
        self.assertTrue(SID_RE.match(done["session_id"]))

    def test_followup_turn_skips_confirm(self):
        body = dict(TURN_BODY, session_id="abc123def456",
                    prompt="again")
        body.pop("confirmed")
        body.pop("intent")
        with mock.patch.object(
                agent_turn, "turn_argv",
                return_value=["/bin/echo", "ok2"]) as argv_mock:
            resp = self.client.post("/api/agent/turn", json=body)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("ok2", resp.text)
        self.assertTrue(argv_mock.call_args[0][4])

    def test_turn_failure_sends_error_event(self):
        with mock.patch.object(
                agent_turn, "turn_argv",
                return_value=["/bin/sh", "-c",
                                "echo bad >&2; exit 2"]):
            resp = self.client.post("/api/agent/turn",
                                    json=TURN_BODY)
        self.assertIn("event: error", resp.text)
        self.assertIn("bad", resp.text)

    def test_truncated_turn_sends_done_with_flag(self):
        async def _fake(*args, **kwargs):
            yield "partial-"

        with mock.patch.object(agent_turn.manager, "run_turn",
                               _fake), \
                mock.patch.object(
                    agent_turn.manager, "pop_result",
                    return_value={"exit": -15, "stderr": "",
                                  "truncated": True}):
            resp = self.client.post("/api/agent/turn",
                                    json=TURN_BODY)
        self.assertIn("event: done", resp.text)
        self.assertIn('"truncated": true', resp.text)
        self.assertNotIn("event: error", resp.text)

    def test_timed_out_turn_sends_error_event(self):
        async def _fake(*args, **kwargs):
            yield "partial-"
            return
            yield  # pragma: no cover - make this an async generator

        with mock.patch.object(agent_turn.manager, "run_turn",
                               _fake), \
                mock.patch.object(
                    agent_turn.manager, "pop_result",
                    return_value={"exit": -15, "stderr": "",
                                  "truncated": False,
                                  "timed_out": True}):
            resp = self.client.post("/api/agent/turn",
                                    json=TURN_BODY)
        self.assertIn("event: error", resp.text)
        self.assertIn("timed out after", resp.text)
        self.assertNotIn("event: done", resp.text)

    def test_abort_requires_session(self):
        resp = self.client.post("/api/agent/abort", json={})
        self.assertEqual(resp.status_code, 400)

    def test_abort_idle_is_not_killed(self):
        resp = self.client.post("/api/agent/abort",
                                json={"session_id": "idle1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True,
                                       "killed": False})
