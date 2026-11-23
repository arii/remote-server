import contextlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from scripts import twt_failure_audit as sut
from scripts.run_root_tests import block_google_docs_requests


def setUpModule():
    unittest.enterModuleContext(block_google_docs_requests())


class FailureAuditTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = {"state_dir": str(root), "runtime_state_dir": str(root)}
        self.db = root / "catalog.sqlite3"
        self.seen = root / sut.SEEN_FILE
        with contextlib.closing(sqlite3.connect(self.db)) as db, db:
            db.executescript("""
                CREATE TABLE attempts (trace_id TEXT, ordinal INTEGER, data TEXT);
                CREATE TABLE traces (id TEXT, timestamp TEXT, data TEXT);
            """)
            attempts = [
                ("nvidia", True, 404, "non-retryable upstream HTTP 404"),
                ("cloudflare", True, 410, "non-retryable upstream HTTP 410"),
                ("groq", True, 429, "upstream HTTP 429"),
                ("groq", True, 429, "non-retryable upstream HTTP 429"),
                ("groq", True, 200, "success"),
                ("groq", False, None, "rate limited by gateway"),
                ("groq", True, None, "upstream connection error"),
            ]
            db.executemany(
                "INSERT INTO attempts VALUES ('trace', ?, ?)",
                [
                    (i, json.dumps(dict(provider=p, called=c, http_status=s, reason=r)))
                    for i, (p, c, s, r) in enumerate(attempts)
                ],
            )
            db.executemany(
                "INSERT INTO traces VALUES (?, 'timestamp', ?)",
                [
                    ("ok", '{"response":{"choices":[]}}'),
                    (
                        "failed",
                        '{"response":{"error":{"type":"no_provider_available"}}}',
                    ),
                ],
            )
        for job in ("owner_repo-7", "archive/owner_repo-8-archived"):
            traces = root / job / "state/traces"
            traces.mkdir(parents=True)
            examples = [
                {
                    "type": "tool_call",
                    "error": "BLOCKED: repeated unchanged read of private",
                },
                {
                    "type": "tool_call",
                    "error": "BLOCKED: Repair repeated unchanged tool call",
                },
                {
                    "type": "tool_call",
                    "error": "BLOCKED: Initial implementation made 24 inspection calls without editing",
                },
                {
                    "type": "coder_verdict",
                    "ok": False,
                    "reason": "no written files for implementation",
                },
                {
                    "type": "tool_call",
                    "name": "write_file",
                    "ok": True,
                    "result_preview": "outside_worktree_root: use worktree-relative paths under private",
                },
            ]
            (traces / "coder_thread.jsonl").write_text(
                "".join(json.dumps(event) + "\n" for event in examples) + '{"partial":',
                encoding="utf-8",
            )
        traces = root / "owner_repo-7/state/traces"
        false_positive = {
            "type": "tool_call",
            "name": "read_file",
            "ok": True,
            "result_preview": "outside_worktree_root: use worktree-relative paths",
        }
        (traces / "coder_read.jsonl").write_text(json.dumps(false_positive) + "\n")
        (traces / "reviewer_thread.jsonl").write_text(json.dumps(examples[0]) + "\n")

    def test_dry_run_groups_evidence_and_never_writes(self):
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            sut.audit(self.config, self.db)
        output = stdout.getvalue()
        for line in (
            "S5 failure audit: 8 causes",
            "TwT nvidia HTTP 404: 1 (new)",
            "TwT cloudflare HTTP 410: 1 (new)",
            "TwT groq HTTP 429: 2 (new)",
            "TwT groq upstream connection error: 1 (new)",
            "TwT no provider available: 1 (new)",
            "Coder repeat loop: 2 (new)",
            "Coder read-only run: 2 (new)",
            "Coder write outside worktree: 2 (new)",
        ):
            self.assertIn(line, output)
        self.assertNotIn("private", output)
        self.assertFalse(self.seen.exists())
        self.assertFalse(self.seen.with_suffix(".lock").exists())

    def test_successful_posts_are_persisted_and_failed_posts_retry(self):
        poster = mock.Mock(side_effect=[200, 503])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                sut.audit(self.config, self.db, poster=poster)
            self.assertEqual(json.loads(self.seen.read_text()), [sut.READ_ONLY])
            first, second = poster.call_args_list
            poster.side_effect = None
            poster.return_value = 200
            poster.reset_mock()
            sut.audit(self.config, self.db, poster=poster)
            self.assertEqual(poster.call_count, 7)
            self.assertEqual(poster.call_args_list[0], second)
            self.assertNotIn(first, poster.call_args_list)
            sut.audit(self.config, self.db, poster=poster)
            self.assertEqual(poster.call_count, 7)
        self.assertEqual(len(json.loads(self.seen.read_text())), 8)
        for call in [first, *poster.call_args_list]:
            agent, note = call.args
            self.assertEqual(agent, "S5-Local")
            self.assertEqual(call.kwargs, {"status": "Act", "link": ""})
            repo = "boomtick" if "TwT " in note else "remote-server"
            self.assertTrue(note.startswith(f"[issue] {repo}: S5 failure audit: "))
            self.assertEqual(
                [line.partition(":")[0] for line in note.splitlines()[1:]],
                ["Problem", "Out of scope", "Done when"],
            )
            self.assertNotIn("private", note)
