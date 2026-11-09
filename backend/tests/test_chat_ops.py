"""Tests for the chat + ops routers (stdlib unittest only)."""

import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import chat, ops
    from backend import proposals

    HAS_CLIENT = True
except ImportError:
    HAS_CLIENT = False


class FakeStdout(io.StringIO):
    def __init__(self, text, block=None):
        super().__init__(text)
        self._block = block

    def read(self, n=-1):
        if self._block is not None:
            self._block.wait(timeout=10)
        return super().read(n)


class FakeProc:
    def __init__(self, text="", block=None, code=0, err=None, ignore_term=False):
        self.stdout = FakeStdout(text, block)
        if err is not None:
            self.stderr = io.StringIO(err)
        self.returncode = code
        self.terminated = False
        self.killed = False
        self._block = block
        self._ignore_term = ignore_term

    def poll(self):
        if self.killed:
            return self.returncode
        if self.terminated and not self._ignore_term:
            return self.returncode
        return None

    def wait(self, timeout=None):
        if self._ignore_term and not self.killed:
            raise subprocess.TimeoutExpired("fake", timeout)
        if self._block is not None:
            self._block.wait(timeout=10)
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self._ignore_term and self._block is not None:
            self._block.set()

    def kill(self):
        self.killed = True
        if self._block is not None:
            self._block.set()


def make_client():
    app = FastAPI()
    app.include_router(chat.router)
    app.include_router(ops.router)
    return TestClient(app)


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class ChatOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp, "ledger.jsonl")
        self.client = make_client()
        self._orig_repo = chat.get_repo_info
        self._orig_spawn = chat.spawn_chat_process
        self._orig_validate = chat.validate_action_request
        self._orig_resume_spawn = chat.run_resume_spawn
        self._orig_venv = chat.deep_agent_python
        self._orig_script = chat.deep_agent_script
        self._orig_takeover = ops.run_takeover
        self._orig_wf = ops.fetch_workflow_health
        self._orig_gh_issue = proposals.run_gh_create_issue
        self._orig_td_run = proposals.run_td_command
        self._orig_dispatch = proposals.run_dispatch
        self._orig_pr_comment = proposals.run_gh_pr_comment
        self._orig_pr_review = proposals.run_gh_pr_review
        chat.get_repo_info = lambda repo: {
            "gh": "arii/remote-server",
            "td_cwd": "/srv/remote-server",
        }
        chat.deep_agent_python = lambda: os.path.abspath(chat.__file__)
        chat.deep_agent_script = lambda name: os.path.abspath(chat.__file__)
        chat.pending_resumes.clear()
        from backend.core.git_helpers import get_repo_status as _real_status

        self._orig_status = _real_status
        import backend.core.git_helpers as _core

        _core.get_repo_status = lambda name: {"repo": name}
        import backend.api.routers.ops as _opsmod

        _opsmod.get_repo_status = lambda name: {"repo": name}
        # In-memory run registry (#1151): the jobs singleton rehydrates
        # the persisted runs store when one exists, so a confirmed
        # clear with no id returns 200 (clears the newest stored run)
        # anywhere a server has run, while pristine CI sees an empty
        # registry and 404s. Swapping in a store-less registry makes
        # the suite hermetic and stops clear_run from persisting into
        # the real store mid-test. Supported unit-test pattern per
        # engine.RunRegistry (store_path=None).
        import backend.api.routers.jobs as _jobsmod
        from backend.exec.engine import RunRegistry

        self._orig_registry = _jobsmod.registry
        _jobsmod.registry = RunRegistry()

    def tearDown(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger
        chat.get_repo_info = self._orig_repo
        chat.spawn_chat_process = self._orig_spawn
        chat.validate_action_request = self._orig_validate
        chat.run_resume_spawn = self._orig_resume_spawn
        chat.deep_agent_python = self._orig_venv
        chat.deep_agent_script = self._orig_script
        chat.pending_resumes.clear()
        # Turn records persist for remount queries (#1015) instead of
        # popping at done — clear per test so finished turns never
        # leak across cases.
        chat.chat_sessions.clear()
        ops.run_takeover = self._orig_takeover
        ops.fetch_workflow_health = self._orig_wf
        proposals.run_gh_create_issue = self._orig_gh_issue
        proposals.run_td_command = self._orig_td_run
        proposals.run_dispatch = self._orig_dispatch
        proposals.run_gh_pr_comment = self._orig_pr_comment
        proposals.run_gh_pr_review = self._orig_pr_review
        import backend.core.git_helpers as _core

        _core.get_repo_status = self._orig_status
        import backend.api.routers.ops as _opsmod

        del _opsmod.get_repo_status
        import backend.api.routers.jobs as _jobsmod

        _jobsmod.registry = self._orig_registry

    def _sse_events(self, resp):
        events = []
        cur = None
        data = []
        for line in resp.text.splitlines():
            if line.startswith("event:"):
                cur = line[len("event:") :].strip()
                data = []
            elif line.startswith("data:"):
                data.append(line[len("data:") :].strip())
            elif line == "" and cur is not None:
                events.append((cur, json.loads("".join(data))))
                cur = None
        return events

    def test_chat_stream_post_to_completion(self):
        chat.spawn_chat_process = lambda cmd: FakeProc("hello world")
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "read the logs",
                "repo": "remote-server",
                "session_id": "abc123def456",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers["content-type"])
        events = self._sse_events(resp)
        kinds = [e for e, _ in events]
        self.assertIn("phase", kinds)
        self.assertIn("token", kinds)
        self.assertIn("done", kinds)
        phases = [p["phase"] for e, p in events if e == "phase"]
        self.assertEqual(phases[0], "prefill")
        self.assertIn("tool_call", phases)  # "read the logs" hints tools
        self.assertEqual(phases[-1], "done")
        done = [p for e, p in events if e == "done"][-1]
        self.assertEqual(done["text"], "hello world")
        self.assertFalse(done["aborted"])

    def test_chat_stream_get_and_empty_prompt(self):
        chat.spawn_chat_process = lambda cmd: FakeProc("x")
        resp = self.client.get(
            "/api/chat/stream",
            params={
                "prompt": "hi there",
                "repo": "remote-server",
                "session_id": "get123get123",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "no-cache")
        self.assertEqual(resp.headers.get("x-accel-buffering"), "no")
        events = self._sse_events(resp)
        self.assertTrue(any(e == "done" for e, _ in events))
        resp = self.client.post(
            "/api/chat/stream", json={"prompt": "   ", "repo": "remote-server"}
        )
        events = self._sse_events(resp)
        self.assertEqual(events[0][0], "error")
        self.assertEqual(events[0][1]["error"], "empty prompt")

    def test_chat_stream_prefer_passes_to_argv(self):
        argvs = []

        def _spawn(cmd):
            argvs.append(cmd)
            return FakeProc("x")

        chat.spawn_chat_process = _spawn
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "prefer000001",
                "prefer": "local",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--prefer=ollama", argvs[-1])
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "prefer000002",
                "prefer": "cloud",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--prefer=nebius", argvs[-1])

    def test_chat_stream_prefer_get_and_auto_default(self):
        argvs = []

        def _spawn(cmd):
            argvs.append(cmd)
            return FakeProc("x")

        chat.spawn_chat_process = _spawn
        resp = self.client.get(
            "/api/chat/stream",
            params={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "prefer000003",
                "prefer": "cloud",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--prefer=nebius", argvs[-1])
        # Auto (and omitted prefer) pass no flag: today's ladder.
        resp = self.client.get(
            "/api/chat/stream",
            params={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "prefer000004",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(any(a.startswith("--prefer=") for a in argvs[-1]))

    def test_chat_stream_bad_prefer_errors(self):
        chat.spawn_chat_process = lambda cmd: FakeProc("x")
        resp = self.client.post(
            "/api/chat/stream",
            json={"prompt": "hi", "repo": "remote-server", "prefer": "Muse"},
        )
        events = self._sse_events(resp)
        self.assertEqual(events[0][0], "error")
        self.assertEqual(events[0][1]["error"], "bad prefer")

    def test_route_hint_resolves_local_free(self):
        orig = chat.model_router.resolve_llm
        seen = {}

        def fake_resolve(prefer=None):
            seen["prefer"] = prefer
            return {
                "provider": "ollama",
                "model": "qwen3:8b",
                "base_url": "http://ollama:11434",
            }

        chat.model_router.resolve_llm = fake_resolve
        chat.spawn_chat_process = lambda cmd: (_ for _ in ()).throw(
            AssertionError("hint must not spawn")
        )
        try:
            resp = self.client.get("/api/chat/route-hint", params={"prefer": "local"})
        finally:
            chat.model_router.resolve_llm = orig
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(seen["prefer"], "ollama")
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["provider"], "ollama")
        self.assertEqual(body["model"], "qwen3:8b")
        self.assertFalse(body["metered"])
        self.assertIsNone(body["rate_in_per_1m"])
        self.assertIsNone(body["rate_out_per_1m"])

    def test_route_hint_cloud_carries_card_rate(self):
        orig = chat.model_router.resolve_llm

        def fake_resolve(prefer=None):
            self.assertEqual(prefer, "nebius")
            return {
                "provider": "nebius",
                "model": "zai-org/GLM-5.3-Flash",
                "base_url": "https://example.invalid",
            }

        chat.model_router.resolve_llm = fake_resolve
        try:
            resp = self.client.get("/api/chat/route-hint", params={"prefer": "cloud"})
        finally:
            chat.model_router.resolve_llm = orig
        body = resp.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["metered"])
        self.assertEqual(body["rate_in_per_1m"], 0.15)
        self.assertEqual(body["rate_out_per_1m"], 0.50)

    def test_route_hint_auto_default_and_bad_prefer(self):
        orig = chat.model_router.resolve_llm
        seen = {}

        def fake_resolve(prefer=None):
            seen["prefer"] = prefer
            return {
                "provider": "ollama",
                "model": "qwen3:8b",
                "base_url": "http://ollama:11434",
            }

        chat.model_router.resolve_llm = fake_resolve
        try:
            resp = self.client.get("/api/chat/route-hint")
        finally:
            chat.model_router.resolve_llm = orig
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(seen["prefer"])
        self.assertTrue(resp.json()["ok"])
        resp = self.client.get("/api/chat/route-hint", params={"prefer": "bogus"})
        self.assertEqual(resp.status_code, 400)

    def test_route_hint_failure_degrades(self):
        orig = chat.model_router.resolve_llm

        def fake_resolve(prefer=None):
            raise RuntimeError("Ollama unreachable at http://x")

        chat.model_router.resolve_llm = fake_resolve
        try:
            resp = self.client.get("/api/chat/route-hint", params={"prefer": "local"})
        finally:
            chat.model_router.resolve_llm = orig
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("Ollama unreachable", body["error"])

    def test_chat_abort_flow(self):
        block = threading.Event()
        procs = []
        chat.spawn_chat_process = lambda cmd: (
            procs.append(FakeProc("partial", block)) or procs[-1]
        )
        result = {}

        def _stream():
            with self.client.stream(
                "POST",
                "/api/chat/stream",
                json={
                    "prompt": "keep going",
                    "repo": "remote-server",
                    "session_id": "abortme00001",
                },
            ) as resp:
                result["status"] = resp.status_code
                chunks = []
                for chunk in resp.iter_text():
                    chunks.append(chunk)
                    if len(chunks) == 1:
                        # First flush arrived: abort while blocked.
                        abort = self.client.post(
                            "/api/chat/abort", json={"session_id": "abortme00001"}
                        )
                        result["abort"] = abort.json()
                        block.set()
                result["text"] = "".join(chunks)

        t = threading.Thread(target=_stream, daemon=True)
        t.start()
        t.join(timeout=30)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["abort"], {"ok": True, "session_id": "abortme00001"})
        self.assertTrue(procs[0].terminated)
        events = self._sse_events(type("R", (), {"text": result["text"]})())
        done = [p for e, p in events if e == "done"][-1]
        self.assertTrue(done["aborted"])
        phases = [p["phase"] for e, p in events if e == "phase"]
        self.assertIn("aborted", phases)

    def test_chat_abort_validation(self):
        resp = self.client.post("/api/chat/abort", json={"session_id": ""})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/chat/abort", json={"session_id": "missing00000"})
        self.assertEqual(resp.status_code, 404)

    def test_chat_disconnect_detaches_for_reattach(self):
        # #1015 replaces kill-on-disconnect: a page leave detaches the
        # turn so a remount re-attaches; the TTL sweep below owns the
        # #294 no-zombie invariant instead of the socket.
        proc = FakeProc("hello world")
        chat.spawn_chat_process = lambda argv: proc
        gen = chat._chat_events("hi", "remote-server", "disc00000001")
        self.assertIn("prefill", next(gen))
        self.assertIn("generating", next(gen))
        self.assertIn("token", next(gen))
        with self.assertRaises(GeneratorExit):
            gen.throw(GeneratorExit)
        self.assertFalse(proc.terminated)
        sess = chat.chat_sessions.get("disc00000001")
        self.assertIsNotNone(sess)
        self.assertFalse(sess["done"])
        self.assertIsNotNone(sess["detached_at"])
        self.assertEqual(sess["tokens"], ["hello world"])
        # Stale detached turns are reaped with their child stopped.
        with mock.patch.object(chat, "DETACHED_CHAT_TTL", -1):
            self.assertGreaterEqual(chat._sweep_chat_sessions(), 1)
        self.assertNotIn("disc00000001", chat.chat_sessions)
        self.assertTrue(proc.terminated)

    def test_chat_status_reports_live_done_and_missing(self):
        now = time.time()
        with chat.chat_sessions_lock:
            chat.chat_sessions["live00000001"] = {
                "id": "live00000001",
                "repo": "remote-server",
                "prompt": "hi",
                "started_at": now - 5,
                "aborted": False,
                "proc": None,
                "tokens": ["hel", "lo"],
                "phase": "generating",
                "done": False,
                "final": None,
                "detached_at": None,
                "completed_at": None,
            }
            chat.chat_sessions["done00000001"] = {
                "id": "done00000001",
                "repo": "remote-server",
                "prompt": "hi",
                "started_at": now - 10,
                "aborted": False,
                "proc": None,
                "tokens": ["hi"],
                "phase": "done",
                "done": True,
                "final": {
                    "kind": "done",
                    "text": "hi",
                    "aborted": False,
                    "elapsed": 1.5,
                },
                "detached_at": None,
                "completed_at": now,
            }
        live = chat.chat_session_status("live00000001")
        self.assertEqual(live["state"], "live")
        self.assertEqual(live["text"], "hello")
        self.assertEqual(live["phase"], "generating")
        done = chat.chat_session_status("done00000001")
        self.assertEqual(done["state"], "done")
        self.assertEqual(done["text"], "hi")
        try:
            chat.chat_session_status("missing000001")
            self.fail("expected 404")
        except Exception as exc:
            self.assertEqual(getattr(exc, "status_code", None), 404)

    def test_chat_reattach_replays_buffer_then_done(self):
        now = time.time()
        with chat.chat_sessions_lock:
            chat.chat_sessions["reatt0000001"] = {
                "id": "reatt0000001",
                "repo": "remote-server",
                "prompt": "hi",
                "started_at": now - 5,
                "aborted": False,
                "proc": None,
                "tokens": ["hel", "lo"],
                "phase": "done",
                "done": True,
                "final": {
                    "kind": "done",
                    "text": "hello",
                    "aborted": False,
                    "elapsed": 2.0,
                },
                "detached_at": None,
                "completed_at": now,
            }
        frames = list(chat._reattach_events("reatt0000001"))
        self.assertTrue(all(f.startswith("event: ") for f in frames))
        kinds = [f.splitlines()[0][len("event: ") :] for f in frames]
        self.assertEqual(kinds, ["token", "token", "done"])
        done = json.loads(frames[-1].split("data: ", 1)[1])
        self.assertEqual(done["text"], "hello")
        self.assertFalse(done["aborted"])

    def test_chat_stream_post_rejects_live_turn(self):
        with chat.chat_sessions_lock:
            chat.chat_sessions["busy00000001"] = {
                "id": "busy00000001",
                "repo": "remote-server",
                "prompt": "hi",
                "started_at": time.time(),
                "aborted": False,
                "proc": None,
                "tokens": [],
                "phase": "generating",
                "done": False,
                "final": None,
                "detached_at": None,
                "completed_at": None,
            }
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "another",
                "repo": "remote-server",
                "session_id": "busy00000001",
            },
        )
        self.assertEqual(resp.status_code, 409)

    def test_chat_stream_timeout_kills_hung_child(self):
        block = threading.Event()
        proc = FakeProc("hung", block)
        chat.spawn_chat_process = lambda argv: proc
        with mock.patch.object(chat, "CHAT_TURN_TIMEOUT", 0.2):
            text = "".join(chat._chat_events("hi", "remote-server", "timeout00001"))
        events = self._sse_events(type("R", (), {"text": text})())
        self.assertEqual(events[-1][0], "error")
        self.assertIn("timed out", events[-1][1]["error"])
        phases = [p["phase"] for e, p in events if e == "phase"]
        self.assertEqual(phases[-1], "error")
        self.assertTrue(proc.terminated)
        self.assertIsNotNone(proc.poll())
        # #1015: terminal turns stay queryable (no pop) until the TTL
        # sweep; the record carries the error final for remounts.
        sess = chat.chat_sessions.get("timeout00001")
        self.assertIsNotNone(sess)
        self.assertTrue(sess["done"])
        self.assertEqual(sess["final"]["kind"], "error")
        self.assertIn("timed out", sess["final"]["error"])
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            logged = [json.loads(line) for line in fh if line.strip()]
        failed = [e for e in logged if e.get("event") == "chat failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("timed out", failed[0]["error"])

    def test_chat_abort_reaps_sigterm_ignoring_child(self):
        block = threading.Event()
        proc = FakeProc("partial", block, ignore_term=True)
        with chat.chat_sessions_lock:
            chat.chat_sessions["reap00000001"] = {
                "id": "reap00000001",
                "aborted": False,
                "proc": proc,
            }
        try:
            ok, err = chat._abort_session("reap00000001")
        finally:
            with chat.chat_sessions_lock:
                chat.chat_sessions.pop("reap00000001", None)
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertTrue(proc.terminated)
        self.assertTrue(proc.killed)
        self.assertIsNotNone(proc.poll())

    def test_pending_resumes_sweep_evicts_stale(self):
        now = time.time()
        old = now - chat.PENDING_RESUME_TTL - 1

        def _entry(**kw):
            base = {
                "gh": "arii/remote-server",
                "repo": "remote-server",
                "value": {"kind": "td"},
                "proposals": ["P"],
                "pending": True,
                "busy": False,
                "created_at": now,
            }
            base.update(kw)
            return base

        chat.pending_resumes["stale0000001"] = _entry(created_at=old)
        chat.pending_resumes["fresh0000001"] = _entry()
        chat.pending_resumes["busy00000001"] = _entry(created_at=old, busy=True)
        legacy = _entry(created_at=old)
        del legacy["created_at"]
        chat.pending_resumes["legacy000001"] = legacy
        chat.spawn_chat_process = lambda argv: FakeProc("ok", code=0)
        "".join(chat._chat_events("hi", "remote-server", "sweep0000001"))
        self.assertNotIn("stale0000001", chat.pending_resumes)
        self.assertNotIn("legacy000001", chat.pending_resumes)
        self.assertIn("fresh0000001", chat.pending_resumes)
        self.assertIn("busy00000001", chat.pending_resumes)

    def test_chat_resume_stale_entry_is_unknown(self):
        chat.pending_resumes["stale0000002"] = {
            "gh": "arii/remote-server",
            "repo": "remote-server",
            "value": {"kind": "td"},
            "proposals": ["P"],
            "pending": True,
            "busy": False,
            "created_at": time.time() - chat.PENDING_RESUME_TTL - 1,
        }
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "stale0000002", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn("stale0000002", chat.pending_resumes)

    def _interrupt_text(self, value):
        blob = json.dumps(
            {"thread_id": "sess00000001", "interrupts": [{"id": "x", "value": value}]}
        )
        return f"Looking into it…\nINTERRUPT: {blob}\n"

    def test_chat_stream_exit_codes(self):
        chat.spawn_chat_process = lambda argv: FakeProc("nope", code=2)
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "exit20000001",
            },
        )
        events = self._sse_events(resp)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("misconfigured", events[-1][1]["error"])
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            logged = [json.loads(line) for line in fh if line.strip()]
        failed = [e for e in logged if e.get("event") == "chat failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("misconfigured", failed[0]["error"])
        chat.deep_agent_python = lambda: "/nonexistent/python"
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "exit20000002",
            },
        )
        events = self._sse_events(resp)
        self.assertEqual(events[0][1]["error"], "agent runtime not installed")

    def test_chat_stream_exit1_sorry_is_error(self):
        sorry = "Sorry, no model is available right now: boom"
        chat.spawn_chat_process = lambda argv: FakeProc(sorry, code=1)
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "exit10000001",
            },
        )
        events = self._sse_events(resp)
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"], sorry)
        phases = [p["phase"] for e, p in events if e == "phase"]
        self.assertEqual(phases[-1], "error")
        self.assertNotIn("Complete", resp.text)
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            logged = [json.loads(line) for line in fh if line.strip()]
        failed = [e for e in logged if e.get("event") == "chat failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"], sorry)
        self.assertEqual(failed[0]["exit"], 1)
        finished = [e for e in logged if e.get("event") == "chat finished"]
        self.assertEqual(finished, [])

    def test_chat_stream_exit1_empty_is_error(self):
        chat.spawn_chat_process = lambda argv: FakeProc("", code=1)
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "exit10000002",
            },
        )
        events = self._sse_events(resp)
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"], "chat failed (exit 1)")
        self.assertNotIn("Complete", resp.text)
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            logged = [json.loads(line) for line in fh if line.strip()]
        failed = [e for e in logged if e.get("event") == "chat failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["exit"], 1)

    def test_chat_stream_approval_and_autoreject(self):
        value = {"action_requests": [{"args": {"kind": "td", "command": "td hi"}}]}
        chat.spawn_chat_process = lambda argv: FakeProc(
            self._interrupt_text(value), code=3
        )
        chat.validate_action_request = lambda args: (
            True,
            "PROPOSE td\nCOMMAND: td hi\nEND PROPOSE",
        )
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "do the thing",
                "repo": "remote-server",
                "session_id": "sess00000001",
            },
        )
        events = self._sse_events(resp)
        approvals = [p for e, p in events if e == "approval"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["session_id"], "sess00000001")
        self.assertEqual(
            approvals[0]["proposals"], ["PROPOSE td\nCOMMAND: td hi\nEND PROPOSE"]
        )
        done = [p for e, p in events if e == "done"][-1]
        self.assertTrue(done["approval_pending"])
        self.assertNotIn("INTERRUPT", done["text"])
        self.assertTrue(chat.pending_resumes["sess00000001"]["pending"])
        # Invalid proposal: auto-rejected, resume spawned, no approval kept.
        chat.validate_action_request = lambda args: (False, "REJECTED: nope")
        calls = []
        chat.run_resume_spawn = lambda *a: calls.append(a) or (0, "ok")
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "do the thing",
                "repo": "remote-server",
                "session_id": "sess00000002",
            },
        )
        events = self._sse_events(resp)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("REJECTED", events[-1][1]["error"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "reject")
        self.assertNotIn("sess00000002", chat.pending_resumes)

    def test_chat_stream_strips_machine_lines_from_tokens_and_done(self):
        blob = json.dumps(
            {
                "thread_id": "strip0000001",
                "interrupts": [{"id": "x", "value": {"kind": "td"}}],
            }
        )
        full = (
            "0123456789abcdef\n"
            f"INTERRUPT: {blob}\n"
            "trace: /tmp/pi-trace-123.log\n"
            "Hello world, this is fine.\n"
            "Done."
        )
        chat.spawn_chat_process = lambda argv: FakeProc(full, code=0)
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "strip0000001",
            },
        )
        events = self._sse_events(resp)
        tokens = [p["token"] for e, p in events if e == "token"]
        self.assertTrue(tokens)
        for tok in tokens:
            self.assertNotIn("INTERRUPT:", tok)
            self.assertNotIn("trace:", tok)
        want = "0123456789abcdef\nHello world, this is fine.\nDone."
        self.assertEqual("".join(tokens), want)
        done = [p for e, p in events if e == "done"][-1]
        self.assertFalse(done["aborted"])
        self.assertEqual(done["text"], want)
        self.assertEqual(done["text"], chat.strip_agent_markers(full))

    def test_chat_stream_keeps_midline_machine_text(self):
        full = "say INTERRUPT: loudly\ntrace amounts ok"
        chat.spawn_chat_process = lambda argv: FakeProc(full, code=0)
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "strip0000002",
            },
        )
        events = self._sse_events(resp)
        tokens = "".join(p["token"] for e, p in events if e == "token")
        self.assertEqual(tokens, full)
        done = [p for e, p in events if e == "done"][-1]
        self.assertEqual(done["text"], full)

    def test_chat_stream_approval_tokens_stripped_interrupt_parses(self):
        value = {"action_requests": [{"args": {"kind": "td", "command": "td hi"}}]}
        chat.spawn_chat_process = lambda argv: FakeProc(
            self._interrupt_text(value), code=3
        )
        chat.validate_action_request = lambda args: (
            True,
            "PROPOSE td\nCOMMAND: td hi\nEND PROPOSE",
        )
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "do the thing",
                "repo": "remote-server",
                "session_id": "strip0000003",
            },
        )
        events = self._sse_events(resp)
        approvals = [p for e, p in events if e == "approval"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(
            approvals[0]["proposals"], ["PROPOSE td\nCOMMAND: td hi\nEND PROPOSE"]
        )
        for e, p in events:
            if e == "token":
                self.assertNotIn("INTERRUPT:", p["token"])
        done = [p for e, p in events if e == "done"][-1]
        self.assertTrue(done["approval_pending"])
        self.assertNotIn("INTERRUPT:", done["text"])

    def test_chat_stream_exit1_strips_machine_lines(self):
        sorry = "Sorry, no model is available right now: boom"
        chat.spawn_chat_process = lambda argv: FakeProc(
            f'{sorry}\nINTERRUPT: {{"x": 1}}\n', code=1
        )
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "exit1strip001",
            },
        )
        events = self._sse_events(resp)
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"], sorry)
        for e, p in events:
            if e == "token":
                self.assertNotIn("INTERRUPT:", p["token"])

    def test_chat_aborted_done_strips_machine_lines(self):
        sid = "abortstrip01"
        full = 'ok\nINTERRUPT: {"x": 1}\nmore'

        def _spawn(argv):
            proc = FakeProc(full, code=0)
            orig_read = proc.stdout.read
            state = {"n": 0}

            def _read(n=-1):
                state["n"] += 1
                if state["n"] == 2:
                    with chat.chat_sessions_lock:
                        sess = chat.chat_sessions.get(sid)
                        if sess is not None:
                            sess["aborted"] = True
                return orig_read(n)

            proc.stdout.read = _read
            return proc

        chat.spawn_chat_process = _spawn
        text = "".join(chat._chat_events("hi", "remote-server", sid))
        events = self._sse_events(type("R", (), {"text": text})())
        done = [p for e, p in events if e == "done"][-1]
        self.assertTrue(done["aborted"])
        # How many chunks land before the abort is noticed is timing
        # (reader thread); the contract is stripped partial text.
        self.assertNotIn("INTERRUPT:", done["text"])
        self.assertNotIn("trace:", done["text"])
        self.assertIn(done["text"], ("", "ok", "ok\nmore"))

    def test_token_scrubber_mirrors_strip_agent_markers(self):
        cases = [
            ('ab\nINTERRUPT: {"x": 1}\ncd', "ab\ncd"),
            ("trace: /tmp/a.log\nhello", "hello"),
            ("say INTERRUPT: now\ntrace ok here", "say INTERRUPT: now\ntrace ok here"),
            ("a\nINTERRUPT: one\nINTERRUPT: two\nb\n", "a\nb\n"),
            ("INTERRUPT: lonely", ""),
            ("", ""),
            ("x" * 50 + "\nend", "x" * 50 + "\nend"),
        ]
        for text, want in cases:
            for width in (1, 3, 16):
                sc = chat._TokenScrubber()
                parts = [
                    sc.feed(text[i : i + width]) for i in range(0, len(text), width)
                ]
                parts.append(sc.flush())
                self.assertEqual("".join(parts), want, f"text={text!r} width={width}")

    def test_token_scrubber_streams_kept_lines_live(self):
        sc = chat._TokenScrubber()
        self.assertEqual(sc.feed("x" * 16), "x" * 16)
        self.assertEqual(sc.flush(), "")

    def test_chat_resume_flow(self):
        chat.pending_resumes["rsm000000001"] = {
            "gh": "arii/remote-server",
            "repo": "remote-server",
            "value": {"kind": "td"},
            "proposals": ["P"],
            "pending": True,
            "busy": False,
            "created_at": time.time(),
        }
        seen = []
        chat.run_resume_spawn = lambda *a: seen.append(a) or (0, "All done.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "rsm000000001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertTrue(body["resumed"])
        self.assertEqual(body["text"], "All done.")
        self.assertNotIn("approval", body)
        # Double-tap: second POST is a 409, not a second spawn.
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "rsm000000001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(seen), 1)
        # Unknown session, bad shape.
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "never0000000", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post(
            "/api/chat/resume", json={"session_id": "rsm000000001", "decision": "maybe"}
        )
        self.assertEqual(resp.status_code, 400)

    def test_chat_resume_unreadable_reinterrupt_autorejects(self):
        chat.pending_resumes["rsm000000003"] = {
            "gh": "arii/remote-server",
            "repo": "remote-server",
            "value": {"kind": "td"},
            "proposals": ["P"],
            "pending": True,
            "busy": False,
            "created_at": time.time(),
        }
        weird = 'INTERRUPT: {"thread_id": "t", "interrupts": [{"value": "?"}]}\n'
        calls = []
        outs = [(3, weird), (0, "stood down.")]
        chat.run_resume_spawn = lambda *a: calls.append(a) or outs.pop(0)
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "rsm000000003", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["text"], "stood down.")
        self.assertNotIn("approval", body)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][2], "reject")
        self.assertFalse(chat.pending_resumes["rsm000000003"]["pending"])

    def test_chat_resume_stale_and_weird_exits(self):
        chat.pending_resumes["rsm000000004"] = {
            "gh": "arii/remote-server",
            "repo": "remote-server",
            "value": {"kind": "td"},
            "proposals": ["P"],
            "pending": True,
            "busy": False,
            "created_at": time.time(),
        }
        chat.run_resume_spawn = lambda *a: (1, "")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "rsm000000004", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 409)
        chat.pending_resumes["rsm000000005"] = {
            "gh": "arii/remote-server",
            "repo": "remote-server",
            "value": {"kind": "td"},
            "proposals": ["P"],
            "pending": True,
            "busy": False,
            "created_at": time.time(),
        }
        chat.run_resume_spawn = lambda *a: (-15, "")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "rsm000000005", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 500)
        self.assertIn("exited -15", resp.json()["detail"])

    def test_chat_resume_reject_defaults_message(self):
        import base64

        chat.pending_resumes["rsm000000002"] = {
            "gh": "arii/remote-server",
            "repo": "remote-server",
            "value": {"kind": "td"},
            "proposals": ["P"],
            "pending": True,
            "busy": False,
            "created_at": time.time(),
        }
        seen = []
        chat.run_resume_spawn = lambda *a: seen.append(a) or (0, "Declined.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "rsm000000002", "decision": "reject"},
        )
        self.assertEqual(resp.status_code, 202)
        sent = base64.b64decode(seen[0][3]).decode()
        self.assertEqual(sent, "Declined by reviewer.")

    def _seed_pending(self, sid, requests):
        chat.pending_resumes[sid] = {
            "gh": "arii/remote-server",
            "repo": "remote-server",
            "value": {"action_requests": [{"args": a} for a in requests]},
            "proposals": ["P"],
            "pending": True,
            "busy": False,
            "created_at": time.time(),
        }

    def _ledger_events(self):
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _executed_rows(self):
        return [
            e for e in self._ledger_events() if e.get("event") == "proposal executed"
        ]

    def test_chat_resume_approve_executes_issue_once(self):
        self._seed_pending(
            "execissue0001",
            [{"kind": "issue", "title": "Fix it", "body": "Details here"}],
        )
        seen = []
        proposals.run_gh_create_issue = lambda *a: (
            seen.append(a) or "https://github.com/arii/remote-server/issues/999"
        )
        chat.run_resume_spawn = lambda *a: (0, "All done.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execissue0001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(seen, [("arii/remote-server", "Fix it", "Details here")])
        self.assertIn("✓ issue filed", body["text"])
        self.assertIn("issues/999", body["text"])
        self.assertEqual(len(body["executions"]), 1)
        self.assertTrue(body["executions"][0]["ok"])
        self.assertEqual(body["executions"][0]["proposal_id"], "execissue0001:0")
        rows = self._executed_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal_kind"], "issue")
        self.assertIn("999", rows[0]["detail"])
        # Double-approve: 409, no second execution.
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execissue0001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(self._executed_rows()), 1)

    def test_chat_resume_approve_executes_comment_once(self):
        self._seed_pending(
            "execcomment001",
            [
                {
                    "kind": "comment",
                    "repo": "arii/remote-server",
                    "pr_number": 12,
                    "body": "Nice work.",
                }
            ],
        )
        seen = []
        proposals.run_gh_pr_comment = lambda *a: (
            seen.append(a)
            or "https://github.com/arii/remote-server/pull/12#issuecomment-1"
        )
        chat.run_resume_spawn = lambda *a: (0, "All done.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execcomment001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(seen, [("arii/remote-server", 12, "Nice work.")])
        self.assertIn("✓ comment posted", body["text"])
        self.assertIn("issuecomment-1", body["text"])
        self.assertEqual(len(body["executions"]), 1)
        self.assertTrue(body["executions"][0]["ok"])
        self.assertEqual(body["executions"][0]["proposal_id"], "execcomment001:0")
        rows = self._executed_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal_kind"], "comment")
        self.assertIn("issuecomment-1", rows[0]["detail"])
        # Double-approve: 409, no second execution.
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execcomment001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(self._executed_rows()), 1)

    def test_chat_resume_approve_executes_review(self):
        self._seed_pending(
            "execreview0001",
            [
                {
                    "kind": "review",
                    "repo": "arii/remote-server",
                    "pr_number": 12,
                    "review_event": "request-changes",
                    "body": "Please fix the leak.",
                }
            ],
        )
        seen = []
        proposals.run_gh_pr_review = lambda *a: (
            seen.append(a)
            or "https://github.com/arii/remote-server/pull/12#pullrequestreview-2"
        )
        chat.run_resume_spawn = lambda *a: (0, "All done.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execreview0001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(
            seen,
            [("arii/remote-server", 12, "request-changes", "Please fix the leak.")],
        )
        self.assertIn("✓ review submitted", body["text"])
        self.assertIn("pullrequestreview-2", body["text"])
        self.assertEqual(
            body["executions"][0]["target"], "arii/remote-server#12 request-changes"
        )
        rows = self._executed_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal_kind"], "review")

    def test_chat_resume_reject_runs_no_comment_review(self):
        self._seed_pending(
            "execrej000002",
            [
                {
                    "kind": "comment",
                    "repo": "arii/remote-server",
                    "pr_number": 12,
                    "body": "B",
                },
                {
                    "kind": "review",
                    "repo": "arii/remote-server",
                    "pr_number": 12,
                    "review_event": "comment",
                    "body": "B",
                },
            ],
        )
        seen = []
        proposals.run_gh_pr_comment = lambda *a: seen.append(a)
        proposals.run_gh_pr_review = lambda *a: seen.append(a)
        chat.run_resume_spawn = lambda *a: (0, "Declined.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execrej000002", "decision": "reject"},
        )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(seen, [])
        self.assertEqual(len(self._executed_rows()), 0)

    def test_chat_resume_approve_executes_td(self):
        self._seed_pending("exectd0000001", [{"kind": "td", "command": "td doctor"}])
        seen = []
        proposals.run_td_command = lambda *a: seen.append(a) or "all green"
        chat.run_resume_spawn = lambda *a: (0, "Checked.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "exectd0000001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(seen, [(["td", "doctor"], "remote-server")])
        self.assertIn("✓ td `td doctor`", body["text"])
        self.assertIn("all green", body["text"])
        self.assertEqual(body["executions"][0]["target"], "td doctor")
        rows = self._executed_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal_kind"], "td")

    def test_chat_resume_approve_executes_dispatch(self):
        self._seed_pending(
            "execdisp00001",
            [
                {
                    "kind": "dispatch",
                    "repo": "arii/remote-server",
                    "branch": "main",
                    "task": "Implement X",
                }
            ],
        )
        seen = []

        def _disp(*a):
            seen.append(a)
            return {
                "session_id": "sess_abc123",
                "state": "running",
                "worktree": "/tmp/wt-1",
                "branch": "wt-1",
            }

        proposals.run_dispatch = _disp
        chat.run_resume_spawn = lambda *a: (0, "Kicked off.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execdisp00001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(seen, [("arii/remote-server", "main", "Implement X")])
        self.assertIn("✓ dispatch started: session sess_abc123", body["text"])
        self.assertEqual(body["executions"][0]["target"], "arii/remote-server@main")
        rows = self._executed_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["proposal_kind"], "dispatch")
        self.assertIn("sess_abc123", rows[0]["detail"])

    def test_chat_resume_approve_batch_and_reject_runs_nothing(self):
        self._seed_pending(
            "execbatch0001",
            [
                {"kind": "issue", "title": "T", "body": "B"},
                {"kind": "td", "command": "td doctor"},
            ],
        )
        issue_seen = []
        td_seen = []
        proposals.run_gh_create_issue = lambda *a: issue_seen.append(a) or "http://x/1"
        proposals.run_td_command = lambda *a: td_seen.append(a) or "ok"
        chat.run_resume_spawn = lambda *a: (0, "Both queued.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execbatch0001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(len(issue_seen), 1)
        self.assertEqual(len(td_seen), 1)
        self.assertEqual(len(body["executions"]), 2)
        self.assertIn("✓ issue filed", body["text"])
        self.assertIn("✓ td `td doctor`", body["text"])
        self.assertEqual(len(self._executed_rows()), 2)
        # Reject path executes nothing.
        self._seed_pending(
            "execrej000001", [{"kind": "issue", "title": "T", "body": "B"}]
        )
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execrej000001", "decision": "reject"},
        )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(len(issue_seen), 1)
        self.assertEqual(len(self._executed_rows()), 2)

    def test_chat_resume_approve_execution_failure_surfaced(self):
        self._seed_pending(
            "execfail00001", [{"kind": "issue", "title": "T", "body": "B"}]
        )

        def _boom(*a):
            raise RuntimeError("gh exploded")

        proposals.run_gh_create_issue = _boom
        chat.run_resume_spawn = lambda *a: (0, "Tried.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execfail00001", "decision": "approve"},
        )
        # A spent approval still answers: failure is surfaced, not a 500.
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("✗ issue failed", body["text"])
        self.assertIn("gh exploded", body["text"])
        self.assertFalse(body["executions"][0]["ok"])
        rows = self._executed_rows()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["ok"])

    def test_chat_resume_followup_approval_executes_new_batch(self):
        self._seed_pending("execfol000001", [{"kind": "td", "command": "td doctor"}])
        td_seen = []
        issue_seen = []
        proposals.run_td_command = lambda *a: td_seen.append(a) or "ok"
        proposals.run_gh_create_issue = lambda *a: issue_seen.append(a) or "http://x/2"
        new_value = {
            "action_requests": [
                {"args": {"kind": "issue", "title": "T2", "body": "B2"}}
            ]
        }
        blob = json.dumps(
            {
                "thread_id": "execfol000001",
                "interrupts": [{"id": "x", "value": new_value}],
            }
        )
        chat.run_resume_spawn = lambda *a: (3, f"progress\nINTERRUPT: {blob}\n")
        chat.validate_action_request = lambda args: (
            True,
            "PROPOSE issue\nTITLE: T2\nEND PROPOSE",
        )
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execfol000001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertIn("approval", body)
        self.assertIn("✓ td `td doctor`", body["text"])
        self.assertEqual(len(td_seen), 1)
        # Second approve runs the follow-up batch, not the first again.
        chat.run_resume_spawn = lambda *a: (0, "Filed.")
        resp = self.client.post(
            "/api/chat/resume",
            json={"session_id": "execfol000001", "decision": "approve"},
        )
        self.assertEqual(resp.status_code, 202)
        self.assertEqual(len(td_seen), 1)
        self.assertEqual(len(issue_seen), 1)
        self.assertIn("http://x/2", resp.json()["text"])

    def test_proposals_skip_invalid_shapes(self):
        out = proposals.execute_proposals(
            [
                (0, {"kind": "issue", "title": "", "body": "b"}),
                (1, {"kind": "td", "command": "td rm -rf /"}),
                (2, {"kind": "td", "command": "td hi"}),
                (3, {"kind": "pigeon"}),
                (
                    4,
                    {
                        "kind": "dispatch",
                        "repo": "evil/x",
                        "branch": "main",
                        "task": "t",
                    },
                ),
            ],
            "v0000000001",
            "arii/remote-server",
            "remote-server",
        )
        self.assertEqual(len(out), 5)
        self.assertTrue(all(o["skipped"] for o in out))
        self.assertEqual(proposals.format_suffix(out), "")

    def test_proposals_comment_review_validation_matrix(self):
        good_comment = {
            "kind": "comment",
            "repo": "arii/remote-server",
            "pr_number": 12,
            "body": "Nice.",
        }
        good_review = {
            "kind": "review",
            "repo": "arii/remote-server",
            "pr_number": 12,
            "review_event": "comment",
            "body": "Nice.",
        }
        bads = []
        for field, bad in (
            ("repo", "noslash"),
            ("repo", "evil/x"),
            ("repo", ""),
            ("pr_number", 0),
            ("pr_number", -1),
            ("pr_number", "12"),
            ("pr_number", True),
            ("pr_number", None),
            ("body", ""),
            ("body", "   "),
            ("body", "x" * 4001),
        ):
            bads.append((len(bads), dict(good_comment, **{field: bad})))
            bads.append((len(bads), dict(good_review, **{field: bad})))
        bads.append((len(bads), dict(good_review, review_event="approve")))
        bads.append((len(bads), dict(good_review, review_event="APPROVE")))
        bads.append((len(bads), dict(good_review, review_event="")))
        out = proposals.execute_proposals(
            bads, "v0000000002", "arii/remote-server", "remote-server"
        )
        self.assertEqual(len(out), len(bads))
        self.assertTrue(all(o["skipped"] for o in out), out)
        self.assertEqual(proposals.format_suffix(out), "")
        self.assertTrue(all("skipped: " in o["detail"] for o in out))

    def test_proposals_pr_comment_argv(self):
        seen = {}

        def _fake_run_gh(gh, argv, timeout=60):
            seen["gh"] = gh
            seen["argv"] = argv
            return "https://github.com/arii/remote-server/pull/12#x\n"

        orig = proposals.run_gh
        proposals.run_gh = _fake_run_gh
        try:
            url = proposals.run_gh_pr_comment("arii/remote-server", 12, "Nice.")
        finally:
            proposals.run_gh = orig
        self.assertEqual(url, "https://github.com/arii/remote-server/pull/12#x")
        self.assertEqual(
            seen["argv"],
            ["pr", "comment", "12", "--repo", "arii/remote-server", "--body", "Nice."],
        )

    def test_proposals_pr_review_argv(self):
        seen = []

        def _fake_run_gh(gh, argv, timeout=60):
            seen.append(argv)
            return "https://github.com/arii/remote-server/pull/12#r\n"

        orig = proposals.run_gh
        proposals.run_gh = _fake_run_gh
        try:
            for event in ("comment", "request-changes"):
                proposals.run_gh_pr_review("arii/remote-server", 12, event, "B")
        finally:
            proposals.run_gh = orig
        self.assertEqual(len(seen), 2)
        for argv, event in zip(seen, ("comment", "request-changes")):
            self.assertEqual(
                argv,
                [
                    "pr",
                    "review",
                    "12",
                    "--repo",
                    "arii/remote-server",
                    f"--{event}",
                    "--body",
                    "B",
                ],
            )
            # --approve is unrepresentable: no event value can
            # produce it, and direct calls with one raise.
            self.assertNotIn("--approve", argv)
        with self.assertRaises(ValueError):
            proposals.run_gh_pr_review("arii/remote-server", 12, "approve", "B")

    def test_proposals_dispatch_runs_worktree_then_session(self):
        calls = []

        def _fake_td_json(argv, timeout):
            calls.append(argv)
            if argv[2] == "worktree":
                return {
                    "status": "success",
                    "worktreeId": "wt-1",
                    "path": "/tmp/wt-1",
                    "branch": "wt-1",
                }
            return {
                "status": "success",
                "session": {"sessionId": "sess_9", "state": "running"},
            }

        orig_json = proposals._run_td_json
        orig_co = proposals.get_checkout_path
        proposals._run_td_json = _fake_td_json
        proposals.get_checkout_path = lambda repo: "/srv/repos/x"
        try:
            got = proposals.run_dispatch("arii/remote-server", "main", "Do it")
        finally:
            proposals._run_td_json = orig_json
            proposals.get_checkout_path = orig_co
        self.assertEqual(got["session_id"], "sess_9")
        self.assertEqual(got["worktree"], "/tmp/wt-1")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][:4], ["td", "--json", "worktree", "create"])
        self.assertIn("--base", calls[0])
        self.assertIn("main", calls[0])
        self.assertEqual(calls[1][:4], ["td", "--json", "agent", "run"])
        self.assertIn("/tmp/wt-1", calls[1])
        self.assertIn("--detach", calls[1])
        self.assertIn("Do it", calls[1])

    def test_proposals_issue_create_argv(self):
        seen = {}

        def _fake_run_gh(gh, argv, timeout=60):
            seen["gh"] = gh
            seen["argv"] = argv
            return "https://github.com/arii/remote-server/issues/5\n"

        orig = proposals.run_gh
        proposals.run_gh = _fake_run_gh
        try:
            url = proposals.run_gh_create_issue("arii/remote-server", "T", "B")
        finally:
            proposals.run_gh = orig
        self.assertEqual(url, "https://github.com/arii/remote-server/issues/5")
        self.assertEqual(
            seen["argv"][:4], ["issue", "create", "--repo", "arii/remote-server"]
        )
        self.assertIn("T", seen["argv"])
        self.assertIn("B", seen["argv"])

    def test_pi_progress(self):
        # No route can start a pi run since /api/ask + /api/suggest
        # were removed (#548), so seed the store directly to cover
        # the kept tail route the jobs view polls as a fallback.
        rid = "ab12cd34ef56"
        with chat.pi_runs_lock:
            chat.pi_runs[rid] = {
                "id": rid,
                "kind": "ask",
                "repo": "remote-server",
                "done": True,
                "result": {"reply": "answer"},
                "error": None,
            }
        try:
            with open(chat._pi_progress_path(rid), "w") as f:
                f.write("working…\n")
            prog = self.client.get(f"/api/pi-progress/{rid}").json()
        finally:
            with chat.pi_runs_lock:
                chat.pi_runs.pop(rid, None)
            try:
                os.remove(chat._pi_progress_path(rid))
            except OSError:
                pass
        self.assertTrue(prog["done"])
        self.assertIn("working", prog["output"])
        self.assertEqual(prog["result"]["reply"], "answer")
        resp = self.client.get("/api/pi-progress/nothex")
        self.assertEqual(resp.status_code, 404)
        resp = self.client.get("/api/pi-progress/aaaaaaaaaaaa")
        self.assertEqual(resp.status_code, 404)
        # /api/ask + /api/suggest are gone (#548): no route serves them.
        resp = self.client.post(
            "/api/ask", json={"repo": "remote-server", "instruction": "why slow?"}
        )
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post("/api/suggest", json={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 404)

    def test_takeover(self):
        ops.run_takeover = lambda: "takeover ok\n"
        resp = self.client.post("/api/terminal/takeover", json={})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/api/terminal/takeover",
            json={"intent": "terminal-takeover", "confirmed": True},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(), {"ok": True, "matched": True, "output": "takeover ok\n"}
        )

    def test_takeover_no_client_matched(self):
        # hup_exit=1: pkill signaled nobody — must not report ok (#431).
        ops.run_takeover = lambda: "hup_exit=1\n"
        resp = self.client.post(
            "/api/terminal/takeover",
            json={"intent": "terminal-takeover", "confirmed": True},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(), {"ok": False, "matched": False, "output": "hup_exit=1\n"}
        )

    def test_takeover_client_matched(self):
        ops.run_takeover = lambda: "hup_exit=0\n"
        resp = self.client.post(
            "/api/terminal/takeover",
            json={"intent": "terminal-takeover", "confirmed": True},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(), {"ok": True, "matched": True, "output": "hup_exit=0\n"}
        )

    def test_takeover_hups_inside_terminal_container(self):
        import subprocess as sp

        seen = {}

        class P:
            stdout = "hup_exit=0\n"

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return P()

        real_run = sp.run
        sp.run = fake_run
        try:
            self.assertEqual(ops.run_takeover(), "hup_exit=0\n")
        finally:
            sp.run = real_run
        argv = seen["argv"]
        self.assertEqual(argv[:2], ["docker", "compose"])
        self.assertIn("-f", argv)
        self.assertEqual(
            argv[-6:],
            [
                "exec",
                "-T",
                "terminal",
                "bash",
                "-c",
                "pkill -HUP -f '^zellij attach'; echo hup_exit=$?",
            ],
        )

    def test_workflow_health(self):
        ops.fetch_workflow_health = lambda repo, gh: {
            "repo": repo,
            "status": "healthy",
            "total_failing": 0,
            "workflows": [],
        }
        resp = self.client.get("/api/workflow-health", params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "healthy")
        resp = self.client.get("/api/workflow-health", params={"repo": "bad/repo!"})
        self.assertEqual(resp.status_code, 400)

    def test_workflow_health_fixture(self):
        fixture = {
            "failing_runs": [
                {
                    "name": "CI",
                    "branch": "main",
                    "event": "push",
                    "url": "https://example.invalid/1",
                }
            ]
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump(fixture, fh)
            path = fh.name
        old = os.environ.get("BUTTONS_FIXTURE_BRIEFING")
        os.environ["BUTTONS_FIXTURE_BRIEFING"] = path
        try:
            with mock.patch.object(ops.logger, "warning") as warn:
                resp = self.client.get(
                    "/api/workflow-health", params={"repo": "remote-server"}
                )
        finally:
            if old is None:
                del os.environ["BUTTONS_FIXTURE_BRIEFING"]
            else:
                os.environ["BUTTONS_FIXTURE_BRIEFING"] = old
            os.remove(path)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "failing")
        self.assertEqual(body["total_failing"], 1)
        self.assertIn("diagnosed_cause", body["workflows"][0])
        # Fixture marker (#453): canned responses say so + name the
        # source file; every fixture serve logs at warning.
        self.assertTrue(body["fixture"])
        self.assertEqual(body["fixture_source"], path)
        self.assertTrue(warn.called)
        logged = " ".join(str(a) for call in warn.call_args_list for a in call[0])
        self.assertIn(path, logged)

    def _write_stray_briefing_fixture(self, payload):
        """A fixture-briefing.json next to the checkouts with no
        explicit env var: the stray-file setup (#453). Returns
        (stray_path, restore) where restore() resets the env."""
        stray = os.path.join(self.tmp, "fixture-briefing.json")
        with open(stray, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        olds = {
            key: os.environ.get(key)
            for key in (
                "BUTTONS_REPOS_DIR",
                "BUTTONS_FIXTURE_BRIEFING",
                "BUTTONS_FIXTURE_AUTO_DISCOVER",
            )
        }
        os.environ["BUTTONS_REPOS_DIR"] = self.tmp
        os.environ.pop("BUTTONS_FIXTURE_BRIEFING", None)
        os.environ.pop("BUTTONS_FIXTURE_AUTO_DISCOVER", None)

        def _restore():
            for key, val in olds.items():
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val
            ops._WORKFLOW_HEALTH_CACHE.clear()

        ops._WORKFLOW_HEALTH_CACHE.clear()
        return stray, _restore

    def test_workflow_health_stray_fixture_ignored(self):
        """A stray next-to-checkouts file alone cannot shadow live."""
        stray, restore = self._write_stray_briefing_fixture(
            {"failing_runs": [{"name": "STRAY"}]}
        )
        ops.fetch_workflow_health = lambda repo, gh: {
            "repo": repo,
            "status": "healthy",
            "total_failing": 0,
            "workflows": [],
        }
        try:
            with mock.patch.object(ops.logger, "warning") as warn:
                resp = self.client.get(
                    "/api/workflow-health", params={"repo": "remote-server"}
                )
        finally:
            restore()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "healthy")
        self.assertEqual(body["total_failing"], 0)
        self.assertFalse(body["fixture"])
        self.assertEqual(body["fixture_source"], "")
        logged = " ".join(str(a) for call in warn.call_args_list for a in call[0])
        self.assertIn("stray", logged)
        self.assertIn(stray, logged)

    def test_workflow_health_auto_discover_opt_in(self):
        _, restore = self._write_stray_briefing_fixture(
            {
                "failing_runs": [
                    {
                        "name": "CI",
                        "branch": "main",
                        "event": "push",
                        "url": "https://example.invalid/1",
                    }
                ]
            }
        )
        os.environ["BUTTONS_FIXTURE_AUTO_DISCOVER"] = "1"
        ops.fetch_workflow_health = mock.Mock(
            side_effect=AssertionError("live must not run")
        )
        try:
            resp = self.client.get(
                "/api/workflow-health", params={"repo": "remote-server"}
            )
        finally:
            restore()
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "failing")
        self.assertEqual(body["total_failing"], 1)
        self.assertTrue(body["fixture"])
        ops.fetch_workflow_health.assert_not_called()

    def test_clear(self):
        resp = self.client.post("/api/clear", json={})
        self.assertEqual(resp.status_code, 400)
        confirm = {"intent": "run-clear", "confirmed": True}
        resp = self.client.post("/api/clear", json=dict(confirm))
        self.assertEqual(resp.status_code, 404)
        import backend.api.routers.jobs as jobs

        resp = self.client.post("/api/clear", json={"id": "nope", **confirm})
        self.assertEqual(resp.status_code, 404)
        # Seed the record shape the registry itself manages.
        with jobs.registry._lock:
            jobs.registry.runs["clearprobe00"] = {
                "id": "clearprobe00",
                "action": "x",
                "repo": "r",
                "done": True,
                "exit_code": 0,
                "output": "old",
                "seq": 9999,
                "started_at": 0,
                "finished_at": 0,
                "last_output_at": 0,
            }
        try:
            resp = self.client.post("/api/clear", json=dict(confirm))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"ok": True, "id": "clearprobe00"})
            self.assertEqual(jobs.registry.runs["clearprobe00"]["output"], "")
        finally:
            with jobs.registry._lock:
                jobs.registry.runs.pop("clearprobe00", None)

    def test_health(self):
        resp = self.client.get("/api/health")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("disk", body)
        self.assertIn("memory", body)
        self.assertIn("containers", body)


@unittest.skipUnless(HAS_CLIENT, "fastapi.testclient not importable")
class ChatStderrTailTest(unittest.TestCase):
    """Spawned agent stderr is drained to a bounded tail (#534)."""

    TRACEBACK = (
        "Traceback (most recent call last):\n"
        '  File "deep_chat.py", line 9, in <module>\n'
        "RuntimeError: deep boom\n"
    )

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(self.tmp, "ledger.jsonl")
        self.client = make_client()
        self._orig_repo = chat.get_repo_info
        self._orig_spawn = chat.spawn_chat_process
        self._orig_venv = chat.deep_agent_python
        self._orig_script = chat.deep_agent_script
        chat.get_repo_info = lambda repo: {
            "gh": "arii/remote-server",
            "td_cwd": "/srv/remote-server",
        }
        chat.deep_agent_python = lambda: os.path.abspath(chat.__file__)
        chat.deep_agent_script = lambda name: os.path.abspath(chat.__file__)

    def tearDown(self):
        if self._orig_ledger is None:
            os.environ.pop("BUTTONS_LEDGER_PATH", None)
        else:
            os.environ["BUTTONS_LEDGER_PATH"] = self._orig_ledger
        chat.get_repo_info = self._orig_repo
        chat.spawn_chat_process = self._orig_spawn
        chat.deep_agent_python = self._orig_venv
        chat.deep_agent_script = self._orig_script

    def _ledger(self):
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def _sse_events(self, resp):
        events = []
        cur = None
        data = []
        for line in resp.text.splitlines():
            if line.startswith("event:"):
                cur = line[len("event:") :].strip()
                data = []
            elif line.startswith("data:"):
                data.append(line[len("data:") :].strip())
            elif line == "" and cur is not None:
                events.append((cur, json.loads("".join(data))))
                cur = None
        return events

    def test_spawn_pipes_stderr(self):
        import sys

        proc = chat.spawn_chat_process([sys.executable, "-c", "pass"])
        try:
            self.assertIsNotNone(proc.stderr)
            proc.wait(timeout=30)
        finally:
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass

    def test_stderr_flood_does_not_deadlock(self):
        import subprocess
        import sys

        child = (
            "import sys; [sys.stderr.write('e' * 1000 + '\\n') "
            "for _ in range(300)]; sys.stderr.write('END-MARKER\\n'); "
            "print('FINAL: ok'); sys.stdout.flush()"
        )
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", child],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            drained = chat.StderrTail(proc)
            out = proc.stdout.read()
            proc.wait(timeout=30)
            tail = drained.tail(timeout=30)
        finally:
            for pipe in (proc.stdout, proc.stderr):
                try:
                    if pipe is not None:
                        pipe.close()
                except Exception:
                    pass
        self.assertIn("FINAL: ok", out)
        self.assertLessEqual(len(tail), chat.SPAWN_STDERR_TAIL)
        self.assertIn("END-MARKER", tail)

    def test_chat_crash_records_stderr_tokens_clean(self):
        chat.spawn_chat_process = lambda argv: FakeProc(
            "partial", code=1, err=self.TRACEBACK
        )
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "crash0000001",
            },
        )
        events = self._sse_events(resp)
        tokens = "".join(p["token"] for e, p in events if e == "token")
        self.assertEqual(tokens, "partial")
        self.assertNotIn("Traceback", resp.text)
        # Exit 1 is a failure (#447): error event, no Complete checkmark.
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"], "partial")
        self.assertNotIn("Complete", resp.text)
        failed = [e for e in self._ledger() if e.get("event") == "chat failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["exit"], 1)
        self.assertIn("Traceback (most recent call last)", failed[0]["stderr"])
        self.assertIn("RuntimeError: deep boom", failed[0]["stderr"])

    def test_chat_exit2_records_stderr_client_text_unchanged(self):
        chat.spawn_chat_process = lambda argv: FakeProc(
            "nope", code=2, err=self.TRACEBACK
        )
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "exit20000std",
            },
        )
        events = self._sse_events(resp)
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"], "agent runtime misconfigured (exit 2)")
        self.assertNotIn("Traceback", resp.text)
        failed = [e for e in self._ledger() if e.get("event") == "chat failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("RuntimeError: deep boom", failed[0]["stderr"])

    def test_chat_success_record_has_no_stderr(self):
        chat.spawn_chat_process = lambda argv: FakeProc("hello", err="banner noise\n")
        resp = self.client.post(
            "/api/chat/stream",
            json={
                "prompt": "hi",
                "repo": "remote-server",
                "session_id": "oksess000001",
            },
        )
        self.assertEqual(resp.status_code, 200)
        finished = [e for e in self._ledger() if e.get("event") == "chat finished"]
        self.assertEqual(len(finished), 1)
        self.assertNotIn("stderr", finished[0])


if __name__ == "__main__":
    unittest.main()
