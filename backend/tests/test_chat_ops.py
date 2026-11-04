"""Tests for the chat + ops routers (stdlib unittest only)."""

import io
import json
import os
import tempfile
import threading
import unittest

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from backend.api.routers import chat, ops

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
    def __init__(self, text="", block=None, code=0, err=None):
        self.stdout = FakeStdout(text, block)
        if err is not None:
            self.stderr = io.StringIO(err)
        self.returncode = code
        self.terminated = False
        self._block = block

    def wait(self):
        if self._block is not None:
            self._block.wait(timeout=10)
        return self.returncode

    def terminate(self):
        self.terminated = True
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
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self.tmp, "ledger.jsonl")
        self.client = make_client()
        self._orig_repo = chat.get_repo_info
        self._orig_spawn = chat.spawn_chat_process
        self._orig_validate = chat.validate_action_request
        self._orig_resume_spawn = chat.run_resume_spawn
        self._orig_venv = chat.deep_agent_python
        self._orig_script = chat.deep_agent_script
        self._orig_takeover = ops.run_takeover
        self._orig_wf = ops.fetch_workflow_health
        chat.get_repo_info = lambda repo: {"gh": "arii/remote-server",
                                           "td_cwd": "/srv/remote-server"}
        chat.deep_agent_python = lambda: os.path.abspath(chat.__file__)
        chat.deep_agent_script = lambda name: os.path.abspath(chat.__file__)
        chat.pending_resumes.clear()
        from backend.core import get_repo_status as _real_status
        self._orig_status = _real_status
        import backend.core as _core
        _core.get_repo_status = lambda name: {"repo": name}
        import backend.api.routers.ops as _opsmod
        _opsmod.get_repo_status = lambda name: {"repo": name}

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
        ops.run_takeover = self._orig_takeover
        ops.fetch_workflow_health = self._orig_wf
        import backend.core as _core
        _core.get_repo_status = self._orig_status
        import backend.api.routers.ops as _opsmod
        del _opsmod.get_repo_status

    def _sse_events(self, resp):
        events = []
        cur = None
        data = []
        for line in resp.text.splitlines():
            if line.startswith("event:"):
                cur = line[len("event:"):].strip()
                data = []
            elif line.startswith("data:"):
                data.append(line[len("data:"):].strip())
            elif line == "" and cur is not None:
                events.append((cur, json.loads("".join(data))))
                cur = None
        return events

    def test_chat_stream_post_to_completion(self):
        chat.spawn_chat_process = lambda cmd: FakeProc("hello world")
        resp = self.client.post("/api/chat/stream",
                               json={"prompt": "read the logs",
                                     "repo": "remote-server",
                                     "session_id": "abc123def456"})
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
        resp = self.client.get("/api/chat/stream",
                               params={"prompt": "hi there",
                                       "repo": "remote-server",
                                       "session_id": "get123get123"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("cache-control"), "no-cache")
        self.assertEqual(resp.headers.get("x-accel-buffering"), "no")
        events = self._sse_events(resp)
        self.assertTrue(any(e == "done" for e, _ in events))
        resp = self.client.post("/api/chat/stream",
                               json={"prompt": "   ",
                                     "repo": "remote-server"})
        events = self._sse_events(resp)
        self.assertEqual(events[0][0], "error")
        self.assertEqual(events[0][1], {"error": "empty prompt"})

    def test_chat_stream_prefer_passes_to_argv(self):
        argvs = []

        def _spawn(cmd):
            argvs.append(cmd)
            return FakeProc("x")

        chat.spawn_chat_process = _spawn
        resp = self.client.post(
            "/api/chat/stream",
            json={"prompt": "hi", "repo": "remote-server",
                  "session_id": "prefer000001", "prefer": "local"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--prefer=ollama", argvs[-1])
        resp = self.client.post(
            "/api/chat/stream",
            json={"prompt": "hi", "repo": "remote-server",
                  "session_id": "prefer000002", "prefer": "cloud"})
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
            params={"prompt": "hi", "repo": "remote-server",
                    "session_id": "prefer000003", "prefer": "cloud"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("--prefer=nebius", argvs[-1])
        # Auto (and omitted prefer) pass no flag: today's ladder.
        resp = self.client.get(
            "/api/chat/stream",
            params={"prompt": "hi", "repo": "remote-server",
                    "session_id": "prefer000004"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(any(a.startswith("--prefer=")
                             for a in argvs[-1]))

    def test_chat_stream_bad_prefer_errors(self):
        chat.spawn_chat_process = lambda cmd: FakeProc("x")
        resp = self.client.post(
            "/api/chat/stream",
            json={"prompt": "hi", "repo": "remote-server",
                  "prefer": "Muse"})
        events = self._sse_events(resp)
        self.assertEqual(events[0][0], "error")
        self.assertEqual(events[0][1], {"error": "bad prefer"})

    def test_route_hint_resolves_local_free(self):
        orig = chat.model_router.resolve_llm
        seen = {}

        def fake_resolve(prefer=None):
            seen["prefer"] = prefer
            return {"provider": "ollama", "model": "qwen3:8b",
                    "base_url": "http://ollama:11434"}

        chat.model_router.resolve_llm = fake_resolve
        chat.spawn_chat_process = lambda cmd: (_ for _ in ()).throw(
            AssertionError("hint must not spawn"))
        try:
            resp = self.client.get("/api/chat/route-hint",
                                   params={"prefer": "local"})
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
            return {"provider": "nebius",
                    "model": "zai-org/GLM-5.3-Flash",
                    "base_url": "https://example.invalid"}

        chat.model_router.resolve_llm = fake_resolve
        try:
            resp = self.client.get("/api/chat/route-hint",
                                   params={"prefer": "cloud"})
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
            return {"provider": "ollama", "model": "qwen3:8b",
                    "base_url": "http://ollama:11434"}

        chat.model_router.resolve_llm = fake_resolve
        try:
            resp = self.client.get("/api/chat/route-hint")
        finally:
            chat.model_router.resolve_llm = orig
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(seen["prefer"])
        self.assertTrue(resp.json()["ok"])
        resp = self.client.get("/api/chat/route-hint",
                               params={"prefer": "bogus"})
        self.assertEqual(resp.status_code, 400)

    def test_route_hint_failure_degrades(self):
        orig = chat.model_router.resolve_llm

        def fake_resolve(prefer=None):
            raise RuntimeError("Ollama unreachable at http://x")

        chat.model_router.resolve_llm = fake_resolve
        try:
            resp = self.client.get("/api/chat/route-hint",
                                   params={"prefer": "local"})
        finally:
            chat.model_router.resolve_llm = orig
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["ok"])
        self.assertIn("Ollama unreachable", body["error"])

    def test_chat_abort_flow(self):
        block = threading.Event()
        procs = []
        chat.spawn_chat_process = lambda cmd: procs.append(
            FakeProc("partial", block)) or procs[-1]
        result = {}

        def _stream():
            with self.client.stream(
                    "POST", "/api/chat/stream",
                    json={"prompt": "keep going",
                          "repo": "remote-server",
                          "session_id": "abortme00001"}) as resp:
                result["status"] = resp.status_code
                chunks = []
                for chunk in resp.iter_text():
                    chunks.append(chunk)
                    if len(chunks) == 1:
                        # First flush arrived: abort while blocked.
                        abort = self.client.post(
                            "/api/chat/abort",
                            json={"session_id": "abortme00001"})
                        result["abort"] = abort.json()
                        block.set()
                result["text"] = "".join(chunks)

        t = threading.Thread(target=_stream, daemon=True)
        t.start()
        t.join(timeout=30)
        self.assertFalse(t.is_alive())
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["abort"],
                         {"ok": True, "session_id": "abortme00001"})
        self.assertTrue(procs[0].terminated)
        events = self._sse_events(type("R", (), {"text": result["text"]})())
        done = [p for e, p in events if e == "done"][-1]
        self.assertTrue(done["aborted"])
        phases = [p["phase"] for e, p in events if e == "phase"]
        self.assertIn("aborted", phases)

    def test_chat_abort_validation(self):
        resp = self.client.post("/api/chat/abort", json={"session_id": ""})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post("/api/chat/abort",
                               json={"session_id": "missing00000"})
        self.assertEqual(resp.status_code, 404)

    def test_chat_disconnect_terminates_child(self):
        proc = FakeProc("hello world")
        chat.spawn_chat_process = lambda argv: proc
        gen = chat._chat_events("hi", "remote-server", "disc00000001")
        self.assertIn("prefill", next(gen))
        self.assertIn("generating", next(gen))
        self.assertIn("token", next(gen))
        with self.assertRaises(GeneratorExit):
            gen.throw(GeneratorExit)
        self.assertTrue(proc.terminated)
        self.assertNotIn("disc00000001", chat.chat_sessions)

    def _interrupt_text(self, value):
        blob = json.dumps({"thread_id": "sess00000001",
                           "interrupts": [{"id": "x", "value": value}]})
        return f"Looking into it…\nINTERRUPT: {blob}\n"

    def test_chat_stream_exit_codes(self):
        chat.spawn_chat_process = lambda argv: FakeProc("nope", code=2)
        resp = self.client.post("/api/chat/stream",
                               json={"prompt": "hi", "repo": "remote-server",
                                     "session_id": "exit20000001"})
        events = self._sse_events(resp)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("misconfigured", events[-1][1]["error"])
        with open(os.environ["BUTTONS_LEDGER_PATH"]) as fh:
            logged = [json.loads(line) for line in fh if line.strip()]
        failed = [e for e in logged if e.get("event") == "chat failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("misconfigured", failed[0]["error"])
        chat.deep_agent_python = lambda: "/nonexistent/python"
        resp = self.client.post("/api/chat/stream",
                               json={"prompt": "hi", "repo": "remote-server",
                                     "session_id": "exit20000002"})
        events = self._sse_events(resp)
        self.assertEqual(events[0][1], {"error": "agent runtime not installed"})

    def test_chat_stream_approval_and_autoreject(self):
        value = {"action_requests": [
            {"args": {"kind": "td", "command": "td hi"}}]}
        chat.spawn_chat_process = lambda argv: FakeProc(
            self._interrupt_text(value), code=3)
        chat.validate_action_request = lambda args: (
            True, "PROPOSE td\nCOMMAND: td hi\nEND PROPOSE")
        resp = self.client.post("/api/chat/stream",
                               json={"prompt": "do the thing",
                                     "repo": "remote-server",
                                     "session_id": "sess00000001"})
        events = self._sse_events(resp)
        approvals = [p for e, p in events if e == "approval"]
        self.assertEqual(len(approvals), 1)
        self.assertEqual(approvals[0]["session_id"], "sess00000001")
        self.assertEqual(approvals[0]["proposals"],
                         ["PROPOSE td\nCOMMAND: td hi\nEND PROPOSE"])
        done = [p for e, p in events if e == "done"][-1]
        self.assertTrue(done["approval_pending"])
        self.assertNotIn("INTERRUPT", done["text"])
        self.assertTrue(chat.pending_resumes["sess00000001"]["pending"])
        # Invalid proposal: auto-rejected, resume spawned, no approval kept.
        chat.validate_action_request = lambda args: (
            False, "REJECTED: nope")
        calls = []
        chat.run_resume_spawn = lambda *a: calls.append(a) or (0, "ok")
        resp = self.client.post("/api/chat/stream",
                               json={"prompt": "do the thing",
                                     "repo": "remote-server",
                                     "session_id": "sess00000002"})
        events = self._sse_events(resp)
        self.assertEqual(events[-1][0], "error")
        self.assertIn("REJECTED", events[-1][1]["error"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2], "reject")
        self.assertNotIn("sess00000002", chat.pending_resumes)

    def test_chat_resume_flow(self):
        chat.pending_resumes["rsm000000001"] = {
            "gh": "arii/remote-server", "repo": "remote-server",
            "value": {"kind": "td"}, "proposals": ["P"],
            "pending": True, "busy": False}
        seen = []
        chat.run_resume_spawn = lambda *a: seen.append(a) or (0, "All done.")
        resp = self.client.post("/api/chat/resume",
                               json={"session_id": "rsm000000001",
                                     "decision": "approve"})
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertTrue(body["resumed"])
        self.assertEqual(body["text"], "All done.")
        self.assertNotIn("approval", body)
        # Double-tap: second POST is a 409, not a second spawn.
        resp = self.client.post("/api/chat/resume",
                               json={"session_id": "rsm000000001",
                                     "decision": "approve"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(len(seen), 1)
        # Unknown session, bad shape.
        resp = self.client.post("/api/chat/resume",
                               json={"session_id": "never0000000",
                                     "decision": "approve"})
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post("/api/chat/resume",
                               json={"session_id": "rsm000000001",
                                     "decision": "maybe"})
        self.assertEqual(resp.status_code, 400)

    def test_chat_resume_unreadable_reinterrupt_autorejects(self):
        chat.pending_resumes["rsm000000003"] = {
            "gh": "arii/remote-server", "repo": "remote-server",
            "value": {"kind": "td"}, "proposals": ["P"],
            "pending": True, "busy": False}
        weird = 'INTERRUPT: {"thread_id": "t", "interrupts": [{"value": "?"}]}\n'
        calls = []
        outs = [(3, weird), (0, "stood down.")]
        chat.run_resume_spawn = lambda *a: calls.append(a) or outs.pop(0)
        resp = self.client.post("/api/chat/resume",
                               json={"session_id": "rsm000000003",
                                     "decision": "approve"})
        self.assertEqual(resp.status_code, 202)
        body = resp.json()
        self.assertEqual(body["text"], "stood down.")
        self.assertNotIn("approval", body)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][2], "reject")
        self.assertFalse(chat.pending_resumes["rsm000000003"]["pending"])

    def test_chat_resume_stale_and_weird_exits(self):
        chat.pending_resumes["rsm000000004"] = {
            "gh": "arii/remote-server", "repo": "remote-server",
            "value": {"kind": "td"}, "proposals": ["P"],
            "pending": True, "busy": False}
        chat.run_resume_spawn = lambda *a: (1, "")
        resp = self.client.post("/api/chat/resume",
                               json={"session_id": "rsm000000004",
                                     "decision": "approve"})
        self.assertEqual(resp.status_code, 409)
        chat.pending_resumes["rsm000000005"] = {
            "gh": "arii/remote-server", "repo": "remote-server",
            "value": {"kind": "td"}, "proposals": ["P"],
            "pending": True, "busy": False}
        chat.run_resume_spawn = lambda *a: (-15, "")
        resp = self.client.post("/api/chat/resume",
                               json={"session_id": "rsm000000005",
                                     "decision": "approve"})
        self.assertEqual(resp.status_code, 500)
        self.assertIn("exited -15", resp.json()["detail"])

    def test_chat_resume_reject_defaults_message(self):
        import base64
        chat.pending_resumes["rsm000000002"] = {
            "gh": "arii/remote-server", "repo": "remote-server",
            "value": {"kind": "td"}, "proposals": ["P"],
            "pending": True, "busy": False}
        seen = []
        chat.run_resume_spawn = lambda *a: seen.append(a) or (0, "Declined.")
        resp = self.client.post("/api/chat/resume",
                               json={"session_id": "rsm000000002",
                                     "decision": "reject"})
        self.assertEqual(resp.status_code, 202)
        sent = base64.b64decode(seen[0][3]).decode()
        self.assertEqual(sent, "Declined by reviewer.")

    def test_pi_progress(self):
        # No route can start a pi run since /api/ask + /api/suggest
        # were removed (#548), so seed the store directly to cover
        # the kept tail route the jobs view polls as a fallback.
        rid = "ab12cd34ef56"
        with chat.pi_runs_lock:
            chat.pi_runs[rid] = {"id": rid, "kind": "ask",
                                 "repo": "remote-server",
                                 "done": True,
                                 "result": {"reply": "answer"},
                                 "error": None}
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
        resp = self.client.post("/api/ask",
                               json={"repo": "remote-server",
                                     "instruction": "why slow?"})
        self.assertEqual(resp.status_code, 404)
        resp = self.client.post("/api/suggest",
                               json={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 404)

    def test_takeover(self):
        ops.run_takeover = lambda: "takeover ok\n"
        resp = self.client.post("/api/terminal/takeover", json={})
        self.assertEqual(resp.status_code, 400)
        resp = self.client.post(
            "/api/terminal/takeover",
            json={"intent": "terminal-takeover", "confirmed": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True,
                                       "output": "takeover ok\n"})

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
            ["exec", "-T", "terminal", "bash", "-c",
             "pkill -HUP -f '^zellij attach'; echo hup_exit=$?"])

    def test_workflow_health(self):
        ops.fetch_workflow_health = lambda repo, gh: {
            "repo": repo, "status": "healthy", "total_failing": 0,
            "workflows": []}
        resp = self.client.get("/api/workflow-health",
                               params={"repo": "remote-server"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "healthy")
        resp = self.client.get("/api/workflow-health",
                               params={"repo": "bad/repo!"})
        self.assertEqual(resp.status_code, 400)

    def test_workflow_health_fixture(self):
        fixture = {"failing_runs": [{"name": "CI", "branch": "main",
                                     "event": "push",
                                     "url": "https://example.invalid/1"}]}
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as fh:
            json.dump(fixture, fh)
            path = fh.name
        old = os.environ.get("BUTTONS_FIXTURE_BRIEFING")
        os.environ["BUTTONS_FIXTURE_BRIEFING"] = path
        try:
            resp = self.client.get("/api/workflow-health",
                                   params={"repo": "remote-server"})
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

    def test_clear(self):
        resp = self.client.post("/api/clear", json={})
        self.assertEqual(resp.status_code, 400)
        confirm = {"intent": "run-clear", "confirmed": True}
        resp = self.client.post("/api/clear", json=dict(confirm))
        self.assertEqual(resp.status_code, 404)
        import backend.api.routers.jobs as jobs
        resp = self.client.post(
            "/api/clear", json={"id": "nope", **confirm})
        self.assertEqual(resp.status_code, 404)
        # Seed the record shape the registry itself manages.
        with jobs.registry._lock:
            jobs.registry.runs["clearprobe00"] = {
                "id": "clearprobe00", "action": "x", "repo": "r",
                "done": True, "exit_code": 0, "output": "old",
                "seq": 9999, "started_at": 0, "finished_at": 0,
                "last_output_at": 0}
        try:
            resp = self.client.post("/api/clear", json=dict(confirm))
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json(), {"ok": True,
                                           "id": "clearprobe00"})
            self.assertEqual(
                jobs.registry.runs["clearprobe00"]["output"], "")
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

    TRACEBACK = ("Traceback (most recent call last):\n"
                 '  File "deep_chat.py", line 9, in <module>\n'
                 "RuntimeError: deep boom\n")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_ledger = os.environ.get("BUTTONS_LEDGER_PATH")
        os.environ["BUTTONS_LEDGER_PATH"] = os.path.join(
            self.tmp, "ledger.jsonl")
        self.client = make_client()
        self._orig_repo = chat.get_repo_info
        self._orig_spawn = chat.spawn_chat_process
        self._orig_venv = chat.deep_agent_python
        self._orig_script = chat.deep_agent_script
        chat.get_repo_info = lambda repo: {"gh": "arii/remote-server",
                                           "td_cwd": "/srv/remote-server"}
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
                cur = line[len("event:"):].strip()
                data = []
            elif line.startswith("data:"):
                data.append(line[len("data:"):].strip())
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
        child = ("import sys; [sys.stderr.write('e' * 1000 + '\\n') "
                 "for _ in range(300)]; sys.stderr.write('END-MARKER\\n'); "
                 "print('FINAL: ok'); sys.stdout.flush()")
        proc = subprocess.Popen(
            [sys.executable, "-u", "-c", child],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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
            "partial", code=1, err=self.TRACEBACK)
        resp = self.client.post(
            "/api/chat/stream",
            json={"prompt": "hi", "repo": "remote-server",
                  "session_id": "crash0000001"})
        events = self._sse_events(resp)
        tokens = "".join(p["token"] for e, p in events if e == "token")
        self.assertEqual(tokens, "partial")
        self.assertNotIn("Traceback", resp.text)
        finished = [e for e in self._ledger()
                    if e.get("event") == "chat finished"]
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0]["exit"], 1)
        self.assertIn("Traceback (most recent call last)",
                      finished[0]["stderr"])
        self.assertIn("RuntimeError: deep boom", finished[0]["stderr"])

    def test_chat_exit2_records_stderr_client_text_unchanged(self):
        chat.spawn_chat_process = lambda argv: FakeProc(
            "nope", code=2, err=self.TRACEBACK)
        resp = self.client.post(
            "/api/chat/stream",
            json={"prompt": "hi", "repo": "remote-server",
                  "session_id": "exit20000std"})
        events = self._sse_events(resp)
        self.assertEqual(events[-1][0], "error")
        self.assertEqual(events[-1][1]["error"],
                         "agent runtime misconfigured (exit 2)")
        self.assertNotIn("Traceback", resp.text)
        failed = [e for e in self._ledger()
                  if e.get("event") == "chat failed"]
        self.assertEqual(len(failed), 1)
        self.assertIn("RuntimeError: deep boom", failed[0]["stderr"])

    def test_chat_success_record_has_no_stderr(self):
        chat.spawn_chat_process = lambda argv: FakeProc(
            "hello", err="banner noise\n")
        resp = self.client.post(
            "/api/chat/stream",
            json={"prompt": "hi", "repo": "remote-server",
                  "session_id": "oksess000001"})
        self.assertEqual(resp.status_code, 200)
        finished = [e for e in self._ledger()
                    if e.get("event") == "chat finished"]
        self.assertEqual(len(finished), 1)
        self.assertNotIn("stderr", finished[0])


if __name__ == "__main__":
    unittest.main()
