import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from buttons import pi_logs


class TestPiLogs(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_iter_session_entries_empty_and_whitespace(self):
        file_path = os.path.join(self.temp_dir.name, "empty.jsonl")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("\n   \n\n")

        entries = list(pi_logs._iter_session_entries(file_path))
        self.assertEqual(entries, [])

    def test_iter_session_entries_skips_invalid_json_but_counts(self):
        file_path = os.path.join(self.temp_dir.name, "mixed.jsonl")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write('{"type": "session", "id": "s1"}\n')
            f.write("not valid json\n")
            f.write('{"role": "user", "content": "hello"}\n')

        entries = list(pi_logs._iter_session_entries(file_path))
        self.assertEqual(len(entries), 2)
        total1, obj1 = entries[0]
        total2, obj2 = entries[1]
        self.assertEqual(total1, 1)
        self.assertEqual(obj1.get("type"), "session")
        self.assertEqual(total2, 3)
        self.assertEqual(obj2.get("content"), "hello")

    def test_parse_session_file_success(self):
        file_path = os.path.join(self.temp_dir.name, "session_test.jsonl")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session", "id": "sess-123", "cwd": "/srv/repos/tech-dancer", "timestamp": "2026-01-01T00:00:00Z"}) + "\n")
            f.write(json.dumps({"role": "user", "content": "Fix bug"}) + "\n")
            f.write(json.dumps({"role": "assistant", "content": "Done"}) + "\n")

        parsed = pi_logs._parse_session_file(file_path)
        self.assertEqual(parsed["id"], "sess-123")
        self.assertEqual(parsed["cwd"], "/srv/repos/tech-dancer")
        self.assertEqual(parsed["repo"], "tech-dancer")
        self.assertEqual(parsed["started"], "2026-01-01T00:00:00Z")
        self.assertEqual(parsed["total_entries"], 3)
        self.assertEqual(len(parsed["turns"]), 2)
        self.assertEqual(parsed["turns"][0]["role"], "user")
        self.assertEqual(parsed["turns"][0]["text"], "Fix bug")
        self.assertEqual(parsed["turns"][1]["role"], "assistant")
        self.assertEqual(parsed["turns"][1]["text"], "Done")

    def test_parse_session_file_not_found(self):
        missing = os.path.join(self.temp_dir.name, "missing.jsonl")
        res = pi_logs._parse_session_file(missing)
        self.assertIn("error", res)
        self.assertEqual(res["turns"], [])

    def test_render_file_output(self):
        file_path = os.path.join(self.temp_dir.name, "render_test.jsonl")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session", "id": "sess-456", "cwd": "/srv/repos/tech-dancer", "timestamp": "2026-01-01T12:00:00Z"}) + "\n")
            f.write(json.dumps({"role": "user", "content": "Hello world"}) + "\n")

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            pi_logs._render_file(file_path)
            output = mock_stdout.getvalue()

        self.assertIn("=== Session: render_test.jsonl ===", output)
        self.assertIn("Session sess-456 — cwd /srv/repos/tech-dancer — started 2026-01-01T12:00:00Z", output)
        self.assertIn("(2 entries in file; showing last 1 rendered messages; full JSONL stays on disk.)", output)
        self.assertIn("user: Hello world", output)

    def test_render_file_missing_error(self):
        missing = os.path.join(self.temp_dir.name, "missing.jsonl")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            pi_logs._render_file(missing)
            output = mock_stdout.getvalue()

        self.assertIn("=== Session: missing.jsonl ===", output)
        self.assertIn("Could not read session:", output)


if __name__ == "__main__":
    unittest.main()
