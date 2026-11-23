import contextlib
import argparse
from collections import Counter
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
        self.config["failure_audit_daily_post_cap"] = 10
        poster = mock.Mock(side_effect=[200, 503])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                sut.audit(self.config, self.db, poster=poster)
            self.assertEqual(sut.load_state(self.seen)["seen"], {sut.READ_ONLY: None})
            self.assertEqual(sut.load_state(self.seen)["posted"], 1)
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
        self.assertEqual(len(sut.load_state(self.seen)["seen"]), 8)
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

    def test_daily_cap_persists_across_runs_and_defers_by_descending_count(self):
        counts = Counter(
            {
                sut.REPEAT: 3,
                sut.READ_ONLY: 3,
                sut.OUTSIDE: 4,
                "TwT groq HTTP 429": 10,
                sut.NO_PROVIDER: 1,
            }
        )
        self.config["failure_audit_daily_post_cap"] = 2
        poster = mock.Mock(return_value=200)
        with (
            mock.patch.object(sut, "utc_day", return_value="2026-11-23") as day,
            mock.patch.object(sut, "twt_counts", return_value=counts),
            mock.patch.object(sut, "runtime_trace_counts", return_value={}),
            contextlib.redirect_stdout(io.StringIO()) as stdout,
        ):
            sut.audit(self.config, self.db)
            self.assertIn(
                "Eligible today (UTC): TwT groq HTTP 429, Coder write outside worktree",
                stdout.getvalue(),
            )
            self.assertFalse(self.seen.exists())
            sut.audit(self.config, self.db, poster=poster)
            sut.audit(self.config, self.db, poster=poster)
            self.assertEqual(poster.call_count, 2)
            counts.clear()
            day.return_value = "2026-11-24"
            sut.audit(self.config, self.db, poster=poster)
        self.assertEqual(
            [call.args[1] for call in poster.call_args_list],
            [
                sut.intake_note("TwT groq HTTP 429"),
                sut.intake_note(sut.OUTSIDE),
                sut.intake_note(sut.READ_ONLY),
                sut.intake_note(sut.REPEAT),
            ],
        )
        self.assertEqual(sut.load_state(self.seen)["pending"], {sut.NO_PROVIDER: 1})

    def test_mark_known_via_cli_migrates_state_and_skips_on_first_post(self):
        self.seen.write_text(json.dumps([sut.REPEAT]))
        config = self.seen.parent / "config.json"
        config.write_text(json.dumps(self.config))
        cause = "TwT groq HTTP 429"
        issue = "https://github.com/arii/boomtick/issues/1"
        with mock.patch(
            "sys.argv",
            ["audit", "--config", str(config), "--mark-known", f"{cause}={issue}"],
        ):
            sut.main()
        state = sut.load_state(self.seen)
        self.assertEqual(state["seen"], {sut.REPEAT: None, cause: issue})
        self.assertEqual(state["posted"], 0)
        poster = mock.Mock(return_value=200)
        with contextlib.redirect_stdout(io.StringIO()):
            sut.audit(self.config, self.db, poster=poster)
        self.assertEqual(poster.call_count, 3)
        self.assertNotIn(
            sut.intake_note(cause), [call.args[1] for call in poster.call_args_list]
        )
        for invalid in (
            f"TwT groq HTTP 999={issue}",
            f"{sut.REPEAT}={issue}",
            f"{cause}=https://example.invalid/issue",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(argparse.ArgumentTypeError),
            ):
                sut.known_cause(invalid)

    def test_submission_crossing_midnight_is_counted_on_acceptance_day(self):
        self.config["failure_audit_daily_post_cap"] = 2
        with (
            mock.patch.object(sut, "utc_day", return_value="2026-11-23") as day,
            contextlib.redirect_stdout(io.StringIO()),
        ):

            def accept(*_args, **_kwargs):
                day.return_value = "2026-11-24"
                return 200

            poster = mock.Mock(side_effect=accept)
            sut.audit(self.config, self.db, poster=poster)
            self.assertEqual(poster.call_count, 1)
            self.assertEqual(sut.load_state(self.seen)["day"], "2026-11-24")
            sut.audit(self.config, self.db, poster=poster)
            self.assertEqual(poster.call_count, 2)
            self.assertEqual(sut.load_state(self.seen)["posted"], 2)
