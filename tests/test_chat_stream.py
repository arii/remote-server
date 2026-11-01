import json
import threading
import time
import unittest
from buttons.server import _stream_ollama_or_pi, Handler
from http.server import HTTPServer
import urllib.request


class TestChatStream(unittest.TestCase):
    def test_chat_stream_generator_phases(self):
        abort_event = threading.Event()
        events = list(_stream_ollama_or_pi("test-session", "remote-server", "git status please", abort_event))
        event_types = [e[0] for e in events]

        # Ensure core phase events are emitted
        self.assertIn("phase", event_types)
        self.assertIn("done", event_types)

        phases = [e[1].get("phase") for e in events if e[0] == "phase"]
        self.assertIn("prefill", phases)
        self.assertIn("tool_call", phases)
        self.assertIn("generating", phases)
        self.assertIn("done", phases)

    def test_chat_stream_abort(self):
        abort_event = threading.Event()
        # Abort immediately
        abort_event.set()
        events = list(_stream_ollama_or_pi("test-session-abort", "remote-server", "hello", abort_event))
        done_events = [e for e in events if e[0] == "done"]
        self.assertTrue(len(done_events) > 0)
        self.assertTrue(done_events[0][1].get("aborted"))

    def test_chat_endpoints_via_http(self):
        server = HTTPServer(("127.0.0.1", 0), Handler)
        port = server.server_port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        try:
            # 1. Missing prompt returns 400
            req = urllib.request.Request(f"http://127.0.0.1:{port}/buttons/api/chat/stream?session_id=s1&prompt=")
            try:
                urllib.request.urlopen(req)
                self.fail("Expected HTTP 400")
            except urllib.error.HTTPError as exc:
                self.assertEqual(exc.code, 400)

            # 2. Valid prompt streams SSE text/event-stream
            req = urllib.request.Request(f"http://127.0.0.1:{port}/buttons/api/chat/stream?session_id=s2&prompt=hello")
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/event-stream", resp.headers.get("Content-Type", ""))
                first_chunk = resp.readline().decode("utf-8")
                self.assertTrue(first_chunk.startswith("event:") or first_chunk.startswith("data:"))

            # 3. Abort endpoint test
            abort_req = urllib.request.Request(
                f"http://127.0.0.1:{port}/buttons/api/chat/abort",
                data=json.dumps({"session_id": "s-nonexistent"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(abort_req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data.get("ok"))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
