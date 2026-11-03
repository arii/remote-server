#!/usr/bin/env python3
"""Tests for terminal/bin/muse-resume-offer (issue #408).

Black-box: run the script against a fixture session store and assert it
offers exactly the interrupted-by-restart session (terminated=0, updated
before container boot, recent, undeclined) as "name|uuid" — and stays
silent exit 0 on every other input (shell start must never break).
"""

import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OFFER = REPO_ROOT / "terminal" / "bin" / "muse-resume-offer"


def make_store(tmp, sessions):
    """Create a fixture muse store; sessions are
    (session_id, workspace_root, age_seconds, logged_name, terminated)."""
    muse_dir = Path(tmp) / "muse"
    muse_dir.mkdir()
    db = muse_dir / "session-index.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY,"
        " workspace_root TEXT, updated_at_us INTEGER,"
        " logged_session_name TEXT, latest_segment_terminated INTEGER)"
    )
    now_us = int(time.time() * 1_000_000)
    for sid, ws, age, name, term in sessions:
        us = now_us - int(age * 1_000_000)
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (sid, ws, us, name, term),
        )
    conn.commit()
    conn.close()
    return tmp


def run_offer(tmp, *args, boot_ago=60, declined=None, extra_env=None):
    env = dict(
        os.environ,
        XDG_DATA_HOME=tmp,
        MUSE_BOOT_EPOCH=str(int(time.time()) - boot_ago),
        MUSE_DECLINED_DIR=declined or os.path.join(tmp, "declined"),
        **(extra_env or {}),
    )
    proc = subprocess.run(
        [str(OFFER), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    return proc.returncode, proc.stdout, proc.stderr


class ResumeOfferTest(unittest.TestCase):
    def test_offers_interrupted_session_as_name_pipe_uuid(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [("uuid-1", "/srv/remote-server", 750, "hollow-canes", 0)],
            )
            code, out, _ = run_offer(tmp, "/srv/remote-server")
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "hollow-canes|uuid-1")

    def test_silent_when_terminated_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [("uuid-1", "/srv/remote-server", 750, "hollow-canes", 1)],
            )
            code, out, _ = run_offer(tmp, "/srv/remote-server")
            self.assertEqual(code, 0)
            self.assertEqual(out, "")

    def test_silent_when_updated_after_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [("uuid-1", "/srv/remote-server", 30, "fresh-shell", 0)],
            )
            code, out, _ = run_offer(
                tmp, "/srv/remote-server", boot_ago=3600
            )
            self.assertEqual(code, 0)
            self.assertEqual(out, "")

    def test_silent_when_ancient(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [("uuid-1", "/srv/remote-server", 8 * 86400, "old-one", 0)],
            )
            code, out, _ = run_offer(
                tmp, "/srv/remote-server", boot_ago=60
            )
            self.assertEqual(code, 0)
            self.assertEqual(out, "")

    def test_silent_when_declined(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [("uuid-1", "/srv/remote-server", 750, "hollow-canes", 0)],
            )
            declined = os.path.join(tmp, "declined")
            os.makedirs(declined)
            Path(declined, "uuid-1").touch()
            code, out, _ = run_offer(tmp, "/srv/remote-server")
            self.assertEqual(code, 0)
            self.assertEqual(out, "")

    def test_prefers_workspace_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [
                    ("uuid-ws", "/srv/remote-server", 750, "ws-one", 0),
                    ("uuid-x", "/other", 60, "other-one", 0),
                ],
            )
            code, out, _ = run_offer(tmp, "/srv/remote-server")
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), "ws-one|uuid-ws")

    def test_silent_on_missing_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, _ = run_offer(tmp, "/srv/remote-server")
            self.assertEqual(code, 0)
            self.assertEqual(out, "")

    def test_opt_out_env_skips(self):
        with tempfile.TemporaryDirectory() as tmp:
            make_store(
                tmp,
                [("uuid-1", "/srv/remote-server", 750, "hollow-canes", 0)],
            )
            code, out, _ = run_offer(
                tmp, "/srv/remote-server", extra_env={"MUSE_NO_AUTORESUME": "1"}
            )
            self.assertEqual(code, 0)
            self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
