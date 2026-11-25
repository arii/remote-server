import datetime
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from scripts import ledger_stamp
from scripts import queue_runner


class FakeRunner:
    LEDGER_AGENT = queue_runner.LEDGER_AGENT
    LEDGER_STALE_HOURS = queue_runner.LEDGER_STALE_HOURS
    ledger_age_hours = staticmethod(queue_runner.ledger_age_hours)

    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error

    def ledger_rows(self):
        if self.error:
            raise self.error
        return self.rows


NOW = datetime.datetime(2026, 11, 24, 17, 0, tzinfo=datetime.timezone.utc)


class StampCycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.stamps = Path(self.tmp.name)
        self.lines = []

    def tearDown(self):
        self.tmp.cleanup()

    def stamp(self, repo, number):
        return self.stamps / f"{repo.replace('/', '_')}_{number}"

    def test_a_failed_issue_list_skips_that_repo_and_keeps_its_stamps(self):
        old = self.stamp("arii/boomtick", 7)
        old.touch()
        os.utime(old, (1, 1))

        def listing(repo):
            if repo == "arii/boomtick":
                raise subprocess.CalledProcessError(1, ["gh"])
            return [{"number": 12}]

        complete = ledger_stamp.stamp_cycle(
            FakeRunner(),
            ["arii/boomtick", "arii/remote-server"],
            self.stamps,
            self.lines.append,
            list_issues=listing,
            now=NOW,
        )
        self.assertFalse(complete)
        self.assertTrue(
            self.stamp("arii/remote-server", 12).exists(),
            "the other repo is still stamped",
        )
        self.assertEqual(
            old.stat().st_mtime,
            1,
            "the failed repo's stamp is neither refreshed nor removed",
        )
        self.assertTrue(
            any(
                "arii/boomtick issue list failed (CalledProcessError" in line
                for line in self.lines
            )
        )

    def test_an_unreadable_ledger_writes_no_stamps(self):
        complete = ledger_stamp.stamp_cycle(
            FakeRunner(error=RuntimeError("sheet down")),
            ["arii/remote-server"],
            self.stamps,
            self.lines.append,
            list_issues=lambda repo: [{"number": 1}],
            now=NOW,
        )
        self.assertFalse(complete)
        self.assertEqual(list(self.stamps.iterdir()), [])
        self.assertIn(
            "ledger unreadable (RuntimeError: sheet down), no stamps written",
            self.lines,
        )

    def test_a_fresh_foreign_act_claim_removes_the_stamp(self):
        link = "https://github.com/arii/remote-server/issues/3"
        rows = [
            {
                "Link": link,
                "Status": "Act",
                "Agent / Source": "Devin",
                "Timestamp": "2026-11-24 16:30:00",
            }
        ]
        self.stamp("arii/remote-server", 3).touch()
        complete = ledger_stamp.stamp_cycle(
            FakeRunner(rows),
            ["arii/remote-server"],
            self.stamps,
            self.lines.append,
            list_issues=lambda repo: [{"number": 3}, {"number": 4}],
            now=NOW,
        )
        self.assertTrue(complete)
        self.assertFalse(self.stamp("arii/remote-server", 3).exists())
        self.assertTrue(self.stamp("arii/remote-server", 4).exists())
        self.assertIn("arii/remote-server#3 held: fresh Act claim by Devin", self.lines)

    def test_a_malformed_issue_skips_only_that_repo(self):
        def listing(repo):
            return (
                [{"title": "no number"}] if repo == "arii/boomtick" else [{"number": 5}]
            )

        complete = ledger_stamp.stamp_cycle(
            FakeRunner(),
            ["arii/boomtick", "arii/remote-server"],
            self.stamps,
            self.lines.append,
            list_issues=listing,
            now=NOW,
        )
        self.assertFalse(complete)
        self.assertTrue(self.stamp("arii/remote-server", 5).exists())
        self.assertTrue(
            any(
                "arii/boomtick issue list failed (KeyError: 'number')" in line
                for line in self.lines
            )
        )


class LoadRunnerTest(unittest.TestCase):
    def test_loads_the_real_queue_runner_from_this_directory(self):
        here = Path(ledger_stamp.__file__).resolve().parent
        loaded = ledger_stamp.load_runner(here / "queue_runner.py")
        self.assertEqual(loaded.LEDGER_AGENT, queue_runner.LEDGER_AGENT)
        self.assertTrue(callable(loaded.ledger_rows))

    def test_refuses_a_runner_in_another_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as stop:
                ledger_stamp.load_runner(Path(tmp) / "queue_runner.py")
        self.assertIn("same scripts/ directory", str(stop.exception))


class RunForeverTest(unittest.TestCase):
    def test_gh_failures_never_end_the_daemon_and_leave_the_heartbeat_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamps = Path(tmp) / "ledger-ok"
            stamps.mkdir()
            heartbeat = Path(tmp) / "hb"
            lines = []
            failure = subprocess.CalledProcessError(1, ["gh", "issue", "list"])
            with mock.patch.object(
                ledger_stamp.subprocess, "check_output", side_effect=failure
            ):
                ledger_stamp.run_forever(
                    FakeRunner(),
                    ["arii/remote-server"],
                    stamps,
                    heartbeat,
                    lines.append,
                    sleep=lambda s: None,
                    cycles=3,
                )
            self.assertEqual(
                sum("issue list failed" in line for line in lines), 3, "every cycle ran"
            )
            self.assertFalse(heartbeat.exists())

    def test_an_unexpected_error_in_a_cycle_is_logged_and_the_loop_goes_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = Path(tmp) / "hb"
            lines = []
            with mock.patch.object(
                ledger_stamp, "stamp_cycle", side_effect=[KeyError("number"), True]
            ):
                ledger_stamp.run_forever(
                    FakeRunner(),
                    [],
                    Path(tmp),
                    heartbeat,
                    lines.append,
                    sleep=lambda s: None,
                    cycles=2,
                )
            self.assertIn(
                "cycle failed (KeyError: 'number'), retrying next cycle", lines
            )
            self.assertTrue(any(line.startswith("traceback ") for line in lines))
            self.assertTrue(
                heartbeat.exists(), "the next good cycle touches the heartbeat"
            )


class StatusWarningTest(unittest.TestCase):
    def test_status_names_a_missing_or_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp) / "hb"
            config = {"ledger_stamp_heartbeat": str(hb)}
            self.assertIn(
                "LEDGER-STAMP DOWN", queue_runner.ledger_stamp_warning(config)
            )
            hb.touch()
            now = time.time()
            self.assertEqual(queue_runner.ledger_stamp_warning(config, now=now), "")
            self.assertIn(
                "LEDGER-STAMP STALE",
                queue_runner.ledger_stamp_warning(config, now=now + 21 * 60),
            )
            self.assertEqual(
                queue_runner.ledger_stamp_warning({}, now=now),
                "",
                "off unless configured",
            )


if __name__ == "__main__":
    unittest.main()
