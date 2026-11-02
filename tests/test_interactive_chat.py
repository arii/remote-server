import unittest
from unittest.mock import patch, MagicMock
import io
import json
import urllib.parse
from buttons import server


class TestInteractiveChat(unittest.TestCase):
    def setUp(self):
        # Reset chat sessions before each test
        with server.chat_sessions_lock:
            server.chat_sessions.clear()

    def test_ui_contains_chat_components(self):
        """Ensure UI HTML contains chat card, prompt inputs, phase badges, and buttons across tabs."""
        # Global topbar trigger
        self.assertIn('id="globalChatBtn"', server.HTML)

        # Actions tab chat card
        self.assertIn('id="actionsChatCard"', server.HTML)
        self.assertIn('id="actionsChatInput"', server.HTML)
        self.assertIn('id="actionsChatSendBtn"', server.HTML)
        self.assertIn('id="actionsChatAbortBtn"', server.HTML)
        self.assertIn('id="actionsPhaseBadge"', server.HTML)
        self.assertIn('id="actionsChatStream"', server.HTML)

# Pi tab chat card
        self.assertIn('id="piChatCard"', server.HTML)
        self.assertIn('id="piChatInput"', server.HTML)
        self.assertIn('id="piChatSendBtn"', server.HTML)
        self.assertIn('id="piChatAbortBtn"', server.HTML)
        self.assertIn('id="piPhaseBadge"', server.HTML)
        self.assertIn('id="piChatStream"', server.HTML)

        self.assertIn("initInteractiveChat", server.HTML)
        self.assertIn("code-copy-btn", server.HTML)

    def test_chat_abort_session_function(self):
        """Test _chat_abort_session updates session dictionary and terminates process."""
        mock_proc = MagicMock()
        with server.chat_sessions_lock:
            server.chat_sessions["test-session-123"] = {
                "id": "test-session-123",
                "aborted": False,
                "proc": mock_proc,
            }

        ok, err = server._chat_abort_session("test-session-123")
        self.assertTrue(ok)
        self.assertIsNone(err)
        mock_proc.terminate.assert_called_once()

        with server.chat_sessions_lock:
            self.assertTrue(server.chat_sessions["test-session-123"]["aborted"])

        # Non-existent session
        ok_missing, err_missing = server._chat_abort_session("non-existent")
        self.assertFalse(ok_missing)
        self.assertEqual(err_missing, "session not found")

    @patch("subprocess.Popen")
    @patch("buttons.server._discover_repos")
    def test_chat_stream_sse_flow(self, mock_discover, mock_popen):
        """Test SSE streaming flow emits prefill, tokens, and done events."""
        mock_discover.return_value = {
            "demo-repo": {
                "gh": "example/demo-repo",
                "td_cwd": "/srv/repos/demo-repo",
                "host_path": "/home/ari/repos/demo-repo",
            }
        }

        mock_proc = MagicMock()
        mock_proc.stdout.read.side_effect = ["Hello ", "world!", ""]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        handler = server.Handler.__new__(server.Handler)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        handler._handle_chat_stream(
            prompt="Tell me about this codebase",
            repo="demo-repo",
            session_id="sess-abc"
        )

        handler.send_response.assert_called_with(200)
        handler.send_header.assert_any_call("Content-Type", "text/event-stream")

        output = handler.wfile.getvalue().decode("utf-8")
        self.assertIn("event: phase", output)
        self.assertIn('"phase": "prefill"', output)
        self.assertIn("event: token", output)
        self.assertIn('"token": "Hello "', output)
        self.assertIn('"token": "world!"', output)
        self.assertIn("event: done", output)
        self.assertIn('"text": "Hello world!"', output)

    @patch("buttons.server._chat_abort_session")
    def test_post_abort_endpoint(self, mock_abort):
        mock_abort.return_value = (True, None)

        handler = server.Handler.__new__(server.Handler)
        handler.path = "/buttons/api/chat/abort"
        payload = json.dumps({"session_id": "test-abort-id"}).encode()
        handler.headers = {"Content-Length": str(len(payload))}
        handler.rfile = io.BytesIO(payload)

        json_out = []
        def fake_json(obj, code=200):
            json_out.append((obj, code))
        handler._json = fake_json

        handler.do_POST()
        self.assertEqual(len(json_out), 1)
        resp, code = json_out[0]
        self.assertEqual(code, 200)
        self.assertTrue(resp.get("ok"))
        self.assertEqual(resp.get("session_id"), "test-abort-id")


if __name__ == "__main__":
    unittest.main()
