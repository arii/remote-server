#!/usr/bin/env python3
"""Tests for terminal/bin/muse-resume-hint (issue #377).

Black-box: run the script against a fixture session store and assert
the one-line output, recency, workspace preference, and silent exit 0
on any missing/corrupt input (shell start must never break).
"""

import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

HINT = Path("terminal/bin/muse-resume-hint")


def make_store(tmp, sessions, claims=()):
    """Create a fixture muse store; sessions are
    (session_id, workspace_root, age_seconds_or_None, logged_name)."""
    muse_dir = Path(tmp) / "muse"
    muse_dir.mkdir()
    db = muse_dir / "session-index.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY,"
        " workspace_root TEXT, updated_at_us INTEGER,"
        " logged_session_name TEXT)"
    )
    now_us = int(time.time() * 1_000_000)
    for sid, ws, age, name in sessions:
        us = None if age is None else now_us - int(age * 1_000_000)
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?)", (sid, ws, us, name)
        )
    conn.commit()
    conn.close()
    if claims:
        auth = muse_dir / "session-name-authority"
        auth.mkdir()
        ndb = sqlite3.connect(auth / "session-names.db")
        ndb.execute(
            "CREATE TABLE session_name_claims_v2"
            " (normalized_name TEXT, session_id TEXT)"
        )
        ndb.executemany(
            "INSERT INTO session_name_claims_v2 VALUES (?, ?)", claims
        )
        ndb.commit()
        ndb.close()
    return tmp


def run_hint(tmp, *args):
    env = dict(os.environ, XDG_DATA_HOME=tmp)
    proc = subprocess.run(
        [str(HINT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


class ResumeHintTest(unittest.TestCase):
    def test_names_latest_session_with_recency(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [
                    ("id-old", "/srv/remote-server", 7200, "old-sirius"),
                    ("id-new", "/srv/remote-server", 750, "hollow-canes"),
                ],
            )
            code, out, _ = run_hint(tmp, "/srv/remote-server")
            self.assertEqual(code, 0)
            self.assertEqual(out.count("\n"), 1)
            self.assertIn("hollow-canes", out)
            self.assertIn("12m ago", out)
            self.assertIn("muse resume", out)

    def test_prefers_current_workspace_then_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [
                    ("id-a", "/srv/remote-server", 3600, "ws-session"),
                    ("id-b", "/srv/repos/other", 60, "other-session"),
                ],
            )
            _, out, _ = run_hint(tmp, "/srv/remote-server")
            self.assertIn("ws-session", out)
            _, out, _ = run_hint(tmp, "/srv/repos/none")
            self.assertIn("other-session", out)

    def test_recency_buckets(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(tmp, [("id-a", "/w", 30, "n")])
            _, out, _ = run_hint(tmp, "/w")
            self.assertIn("just now", out)
        with tempfile.TemporaryDirectory() as tmp:
            make_store(tmp, [("id-a", "/w", 3 * 3600, "n")])
            _, out, _ = run_hint(tmp, "/w")
            self.assertIn("3h ago", out)
        with tempfile.TemporaryDirectory() as tmp:
            make_store(tmp, [("id-a", "/w", 2 * 86400, "n")])
            _, out, _ = run_hint(tmp, "/w")
            self.assertIn("2d ago", out)
        with tempfile.TemporaryDirectory() as tmp:
            make_store(tmp, [("id-a", "/w", None, "n")])
            _, out, _ = run_hint(tmp, "/w")
            self.assertIn("time unknown", out)

    def test_name_falls_back_to_claims_then_short_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [("abcdef1234567890", "/w", 60, None)],
                claims=[("claimed-name", "abcdef1234567890")],
            )
            _, out, _ = run_hint(tmp, "/w")
            self.assertIn("claimed-name", out)
        with tempfile.TemporaryDirectory() as tmp:
            make_store(tmp, [("abcdef1234567890", "/w", 60, None)])
            _, out, _ = run_hint(tmp, "/w")
            self.assertIn("abcdef12", out)

    def test_silent_exit_zero_without_store_or_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = run_hint(tmp, "/w")
            self.assertEqual((code, out, err), (0, "", ""))
        with tempfile.TemporaryDirectory() as tmp:
            make_store(tmp, [])
            code, out, err = run_hint(tmp, "/w")
            self.assertEqual((code, out, err), (0, "", ""))
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "muse"
            bad.mkdir()
            (bad / "session-index.db").write_text("not a database")
            code, out, err = run_hint(tmp, "/w")
            self.assertEqual((code, out, err), (0, "", ""))

    def test_dockerfile_wires_hint_into_bashrc(self):
        dockerfile = Path("terminal/Dockerfile").read_text()
        self.assertIn("muse-resume-hint", dockerfile)
        self.assertIn("/root/.bashrc", dockerfile)
