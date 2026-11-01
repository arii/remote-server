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
            f.write("trailing invalid json\n")

        entries = list(pi_logs._iter_session_entries(file_path))
        self.assertEqual(len(entries), 4)
        self.assertEqual(entries[0], (1, {"type": "session", "id": "s1"}))
        self.assertEqual(entries[1], (2, None))
        self.assertEqual(entries[2], (3, {"role": "user", "content": "hello"}))
        self.assertEqual(entries[3], (4, None))

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

    def test_parse_session_file_trailing_invalid_lines(self):
        file_path = os.path.join(self.temp_dir.name, "session_trailing.jsonl")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session", "id": "sess-123", "cwd": "/srv/repos/tech-dancer", "timestamp": "2026-01-01T00:00:00Z"}) + "\n")
            f.write(json.dumps({"role": "user", "content": "Fix bug"}) + "\n")
            f.write("invalid json at EOF\n")

        parsed = pi_logs._parse_session_file(file_path)
        self.assertEqual(parsed["total_entries"], 3)
        self.assertEqual(len(parsed["turns"]), 1)
        self.assertEqual(parsed["turns"][0]["role"], "user")
        self.assertEqual(parsed["turns"][0]["text"], "Fix bug")

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

    def test_render_file_trailing_invalid_lines(self):
        file_path = os.path.join(self.temp_dir.name, "render_trailing.jsonl")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"type": "session", "id": "sess-456", "cwd": "/srv/repos/tech-dancer", "timestamp": "2026-01-01T12:00:00Z"}) + "\n")
            f.write(json.dumps({"role": "user", "content": "Hello world"}) + "\n")
            f.write("malformed line at EOF\n")

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            pi_logs._render_file(file_path)
            output = mock_stdout.getvalue()

        self.assertIn("(3 entries in file; showing last 1 rendered messages; full JSONL stays on disk.)", output)

    def test_render_file_missing_error(self):
        missing = os.path.join(self.temp_dir.name, "missing.jsonl")
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            pi_logs._render_file(missing)
            output = mock_stdout.getvalue()

        self.assertIn("=== Session: missing.jsonl ===", output)
        self.assertIn("Could not read session:", output)


if __name__ == "__main__":
    unittest.main()
