import json
import time
import unittest
from unittest.mock import patch, MagicMock

import buttons.server as server


class TestPiSessionListVisibility(unittest.TestCase):
    def test_pi_session_list_element_and_js(self):
        with open("buttons/server.py", "r", encoding="utf-8") as f:
            content = f.read()

        # 1. HTML container must be hidden by default
        self.assertIn('<div id="piSessionList" style="display:none;"></div>', content)

        # 2. Loading state does not set textContent inside piSessionListEl
        self.assertNotIn('piSessionListEl.textContent = "Loading sessions…";', content)
        self.assertIn('piSessionErrEl.textContent = "Loading sessions…";', content)

        # 3. renderSessionList hides the list element when no sessions match/exist
        self.assertIn('piSessionListEl.style.display = "none";', content)
        self.assertIn('piSessionListEl.style.display = "";', content)


class TestPiSessionListUnified(unittest.TestCase):
    """Unified list shape: pi-file + background + ledger rows,
    newest-first by mtime; empty set -> {"sessions": []}."""

    def _get(self, list_stdout, ledger_stdout=""):
        mock_handler = MagicMock()
        mock_handler.path = "/buttons/api/pi-sessions"
        sent = {}

        def mock_json(obj, code=200):
            sent["obj"] = obj
            sent["code"] = code

        mock_handler._json = mock_json

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            joined = cmd[-1] if isinstance(cmd, list) else ""
            if "--list-json" in joined:
                m.stdout = list_stdout
            else:
                m.stdout = ledger_stdout
            m.stderr = ""
            return m

        with patch("subprocess.run", side_effect=fake_run):
            server.Handler.do_GET(mock_handler)
        return sent

    def test_unified_rows_newest_first(self):
        now = time.time()
        pi_rows = {"sessions": [
            {"file": "20240101_aaa.jsonl", "mtime": now - 30,
             "cwd": "/srv/repos/remote-server", "repo": "remote-server",
             "title": "Fix login bug"},
        ]}
        ledger_line = ("     42\t" + json.dumps(
            {"script": "agent", "model": "model-x", "ts": now,
             "tool_calls": [{"name": "a", "args": {}},
                            {"name": "b", "args": {}}]}) + "\n")
        with server.pi_runs_lock:
            server.pi_runs.clear()
            server.pi_runs["1234567890ab"] = {
                "id": "1234567890ab", "kind": "ask",
                "repo": "remote-server", "done": True,
                "started_at": now - 10, "finished_at": now - 5,
                "instruction": "How does server work?",
                "result": None, "error": None}
        try:
            sent = self._get(json.dumps(pi_rows), ledger_line)
        finally:
            with server.pi_runs_lock:
                server.pi_runs.clear()
        sessions = sent["obj"]["sessions"]
        self.assertEqual(sent["code"], 200)
        self.assertEqual(len(sessions), 3)
        mtimes = [s["mtime"] for s in sessions]
        self.assertEqual(mtimes, sorted(mtimes, reverse=True))
        by_src = {}
        for s in sessions:
            by_src.setdefault(s.get("source", "pi-file"), []).append(s)
        pi = by_src["pi-file"][0]
        self.assertEqual(
            set(pi) >= {"file", "mtime", "cwd", "repo", "title"}, True)
        self.assertNotIn("source", pi)
        bg = by_src["background"][0]
        self.assertEqual(bg["file"], "bg-1234567890ab.jsonl")
        self.assertEqual(bg["id"], "bg-1234567890ab")
        self.assertEqual(bg["kind"], "ask")
        self.assertIsInstance(bg["done"], bool)
        led = by_src["ledger"][0]
        self.assertEqual(led["file"], "ledger-42.jsonl")
        self.assertEqual(led["id"], "ledger-42")
        self.assertEqual(led["kind"], "ledger")
        self.assertEqual(led["source"], "ledger")
        self.assertTrue(led["done"])

    def test_empty_set(self):
        sent = self._get(json.dumps({"sessions": []}), "")
        self.assertEqual(sent["obj"], {"sessions": []})


if __name__ == "__main__":
    unittest.main()
