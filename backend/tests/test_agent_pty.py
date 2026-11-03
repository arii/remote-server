"""Tests for the PTY-backed interactive agent runner (stdlib unittest only).

No real agent binary runs here: lifecycle tests inject /bin/cat via the
argv seam (manager.spawn's argv, or a patched provider_argv on the
router path).
"""

import asyncio
import json
import os
import re
import tempfile
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from backend.exec import agent_pty

SID_RE = re.compile(r"^[0-9a-f]{12}$")
FAKE_ARGV = ["/bin/cat"]
SPAWN_BODY = {"provider": "muse", "repo": "testrepo",
              "intent": "agent-spawn", "confirmed": True}


def _ledger_entries(path):
    try:
        with open(path) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


class ProviderAllowlistTest(unittest.TestCase):
    def test_excluded_providers_not_allowlisted(self):
        for name in ("copilot", "gh-copilot", "codex", "gemini",
                     "claude", "aider", "opencode"):
            self.assertNotIn(name, agent_pty.PROVIDERS)

    def test_provider_argv_rejects_unknown(self):
        with self.assertRaises(ValueError):
            agent_pty.provider_argv("copilot", "/srv/remote-server")

    def test_provider_argv_missing_binary(self):
        run = mock.Mock(
            return_value=mock.Mock(returncode=1, stdout=""))
        with mock.patch.object(agent_pty.subprocess, "run", run):
            with self.assertRaises(RuntimeError):
                agent_pty.provider_argv("muse", "/srv/remote-server")

    def test_provider_argv_resolves_without_flags(self):
        run = mock.Mock(return_value=mock.Mock(
            returncode=0, stdout="/root/.local/bin/muse\n"))
        with mock.patch.object(agent_pty.subprocess, "run", run):
            argv = agent_pty.provider_argv("muse", "/srv/remote-server")
        # via compose exec into terminal (binaries live there, not in
        # buttons); provider itself gets a bare argv, no flags.
        self.assertIn("terminal", argv)
        self.assertEqual(argv[-1], "muse")
        self.assertIn("/srv/remote-server", argv)


class SpawnValidationTest(unittest.TestCase):
    def setUp(self):
        from backend.server import app

        self.client = TestClient(app)

    def test_spawn_requires_confirm(self):
        resp = self.client.post("/api/agent/spawn", json={
            "provider": "muse", "repo": "remote-server"})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/agent/spawn", json={
            "provider": "muse", "repo": "remote-server",
            "intent": "wrong", "confirmed": True})
        self.assertEqual(resp.status_code, 400)

    def test_spawn_rejects_unknown_provider(self):
        resp = self.client.post("/api/agent/spawn", json={
            "provider": "copilot", "repo": "remote-server",
            "intent": "agent-spawn", "confirmed": True})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "unknown provider")

    def test_spawn_rejects_unknown_repo(self):
        resp = self.client.post("/api/agent/spawn", json={
            "provider": "muse", "repo": "no-such-repo",
            "intent": "agent-spawn", "confirmed": True})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json()["detail"], "unknown repo")

    def test_abort_unknown_session(self):
        resp = self.client.post("/api/agent/abort",
                                json={"session_id": "aaaaaaaaaaaa"})
        self.assertEqual(resp.status_code, 404)

    def test_providers_lists_allowlist(self):
        resp = self.client.get("/api/agent/providers")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["providers"],
                         ["muse", "agy", "pi"])

    def test_ws_unknown_session(self):
        from starlette.websockets import WebSocketDisconnect

        with self.assertRaises(WebSocketDisconnect):
            with self.client.websocket_connect(
                    "/api/agent/ws/aaaaaaaaaaaa"):
                pass


class _AgentTmpMixin:
    """Ledger + cwd in tmp dirs; fake provider; leak-free sessions."""

    def _agent_setup(self):
        import backend.api.routers.agent as agent_router

        self._tmp = tempfile.TemporaryDirectory()
        self._ledger_path = os.path.join(self._tmp.name, "ledger.jsonl")
        self._ledger_old = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = self._ledger_path
        self._cwd = os.path.join(self._tmp.name, "repo")
        os.mkdir(self._cwd)
        self._p1 = mock.patch.object(
            agent_router, "_repo_target",
            return_value=("testrepo", self._cwd))
        self._p2 = mock.patch.object(
            agent_pty, "provider_argv", return_value=list(FAKE_ARGV))
        self._p1.start()
        self._p2.start()
        self._router = agent_router

    def _agent_teardown(self):
        for sid in list(agent_pty.manager._sessions):
            try:
                asyncio.run(agent_pty.manager.abort(sid))
            except (KeyError, RuntimeError):
                pass
        self._p1.stop()
        self._p2.stop()
        if self._ledger_old is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._ledger_old
        self._tmp.cleanup()

    def _wait_gone(self, sid, timeout=10.0):
        end = time.time() + timeout
        while time.time() < end:
            if agent_pty.manager.get(sid) is None:
                return True
            time.sleep(0.05)
        return agent_pty.manager.get(sid) is None


class ManagerLifecycleTest(_AgentTmpMixin, unittest.TestCase):
    """Direct manager lifecycle through the argv seam."""

    def setUp(self):
        self._agent_setup()

    def tearDown(self):
        self._agent_teardown()

    def test_spawn_write_read_abort(self):
        async def _flow():
            sid = await agent_pty.manager.spawn(
                "muse", "testrepo", self._cwd, argv=list(FAKE_ARGV))
            self.assertTrue(SID_RE.match(sid))
            info = agent_pty.manager.get(sid)
            self.assertEqual(info["provider"], "muse")
            self.assertFalse(info["done"])
            await agent_pty.manager.write(sid, "hello\n")
            seen = ""
            end = time.time() + 10
            while "hello" not in seen and time.time() < end:
                chunk = await agent_pty.manager.read(sid, timeout=2)
                if chunk is None:
                    break
                seen += chunk
            self.assertIn("hello", seen)
            code = await agent_pty.manager.abort(sid)
            self.assertIsInstance(code, int)
            return sid

        sid = asyncio.run(_flow())
        self.assertIsNone(agent_pty.manager.get(sid))
        entries = [e for e in _ledger_entries(self._ledger_path)
                   if e.get("kind") == "agent"
                   and e.get("session_id") == sid]
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["provider"], "muse")
        self.assertEqual(entry["repo"], "testrepo")
        self.assertIsInstance(entry["exit"], int)
        self.assertIn("hello", entry["tail"])

    def test_abort_unknown_raises(self):
        with self.assertRaises(KeyError):
            asyncio.run(agent_pty.manager.abort("bbbbbbbbbbbb"))

    def test_write_after_abort_raises(self):
        async def _flow():
            sid = await agent_pty.manager.spawn(
                "muse", "testrepo", self._cwd, argv=list(FAKE_ARGV))
            await agent_pty.manager.abort(sid)
            with self.assertRaises(KeyError):
                await agent_pty.manager.write(sid, "x\n")
            with self.assertRaises(KeyError):
                await agent_pty.manager.read(sid, timeout=1)

        asyncio.run(_flow())


class RouterLifecycleTest(_AgentTmpMixin, unittest.TestCase):
    """Router spawn/WS/abort with a fake provider binary."""

    def setUp(self):
        self._agent_setup()
        from backend.server import app

        self.client = TestClient(app)

    def tearDown(self):
        self._agent_teardown()

    def test_spawn_missing_binary_is_500(self):
        with mock.patch.object(
                agent_pty, "provider_argv",
                side_effect=RuntimeError("provider not installed")):
            resp = self.client.post("/api/agent/spawn", json=SPAWN_BODY)
        self.assertEqual(resp.status_code, 500)

    def test_spawn_abort_routes(self):
        resp = self.client.post("/api/agent/spawn", json=SPAWN_BODY)
        self.assertEqual(resp.status_code, 200)
        sid = resp.json()["session_id"]
        self.assertTrue(SID_RE.match(sid))
        resp = self.client.post("/api/agent/abort",
                                json={"session_id": sid})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["exit"], int)
        resp = self.client.post("/api/agent/abort",
                                json={"session_id": sid})
        self.assertEqual(resp.status_code, 404)
        entries = [e for e in _ledger_entries(self._ledger_path)
                   if e.get("kind") == "agent"
                   and e.get("session_id") == sid]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["provider"], "muse")

    def test_ws_roundtrip_and_close_reaps(self):
        resp = self.client.post("/api/agent/spawn", json=SPAWN_BODY)
        self.assertEqual(resp.status_code, 200)
        sid = resp.json()["session_id"]
        with self.client.websocket_connect(
                f"/api/agent/ws/{sid}") as ws:
            ws.send_text("ping-42\n")
            seen = ""
            end = time.time() + 10
            while "ping-42" not in seen and time.time() < end:
                seen += ws.receive_text()
            self.assertIn("ping-42", seen)
        # Close reaps: the session is gone and ledgered exactly once.
        self.assertTrue(self._wait_gone(sid))
        entries = [e for e in _ledger_entries(self._ledger_path)
                   if e.get("kind") == "agent"
                   and e.get("session_id") == sid]
        self.assertEqual(len(entries), 1)
        self.assertIn("ping-42", entries[0]["tail"])


if __name__ == "__main__":
    unittest.main()
