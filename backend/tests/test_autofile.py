"""Tests for repeated-failure autofile (issue #825).

Threshold crossing, marker-dedup skip, close-rearms, body shape,
collapse-rule parity, engine wiring, the CLI runner, and the daily
seed. gh and the ledger are stubbed: subprocess is never touched.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from backend import autofile
from backend.api.routers import jobs as jobs_mod
from backend.exec import engine
from backend import autofile_scan

NOW = 1_700_000_000.0
HOUR = 3600.0


def _webhook(detail="fetch failed: connection refused",
             repo="remote-server", ts=NOW, ok=False,
             observed=None):
    row = {"kind": "webhook", "repo": repo, "ok": ok,
           "advanced": False, "detail": detail, "ts": ts}
    if observed is not None:
        row["observed"] = observed
    return row


def _run(action="preflight", repo="remote-server", ts=NOW,
         exit_code=1, command="td --json preflight"):
    return {"kind": "run", "action": action, "repo": repo,
            "exit": exit_code, "elapsed_s": 1.2,
            "command": command, "ts": ts}


def _streak(n=3, make=_webhook, step=HOUR):
    return [make(ts=NOW - i * step) for i in range(n)]


class CollapseParityTest(unittest.TestCase):
    def test_error_field_wins_verbatim(self):
        entry = _run()
        entry["error"] = "boom: disk full\ntraceback..."
        self.assertEqual(autofile.entry_summary(entry),
                         "boom: disk full\ntraceback..."[:200])

    def test_plain_rules_mirror_history_utils(self):
        self.assertEqual(
            autofile.plain_language("fatal: fetch first", "run"),
            "Push was rejected - the remote has new commits")
        self.assertEqual(
            autofile.plain_language("dial: connection refused",
                                    "webhook"),
            "Network request failed - the service could not be "
            "reached")

    def test_summary_appends_outcome_clause(self):
        self.assertEqual(
            autofile.entry_summary(_run()),
            "preflight - td --json preflight")

    def test_ok_webhook_is_not_a_failure(self):
        row = _webhook(ok=True, detail="up to date")
        self.assertIsNone(autofile.failure_signature(row))

    def test_skipped_webhook_is_not_a_failure(self):
        row = _webhook(ok=True, detail="skipped: tree dirty - "
                                       "behind by 2 (local "
                                       "changes)")
        self.assertIsNone(autofile.failure_signature(row))

    def test_exit_zero_run_is_not_a_failure(self):
        self.assertIsNone(
            autofile.failure_signature(_run(exit_code=0)))

    def test_non_v1_kinds_ignored(self):
        row = {"kind": "chat", "repo": "remote-server",
               "ok": False, "detail": "x", "ts": NOW}
        self.assertIsNone(autofile.failure_signature(row))

    def test_unrepoed_rows_unfileable(self):
        row = _webhook()
        del row["repo"]
        self.assertIsNone(autofile.failure_signature(row))


class ThresholdTest(unittest.TestCase):
    def _scan(self, entries, marker=False):
        filed = []

        def reader(since, limit):
            self.assertGreater(limit, 0)
            return list(entries)

        def filer(repo, title, body):
            filed.append((repo, title, body))
            return "https://github.com/arii/remote-server/issues/1"

        result = autofile.scan_repo(
            "remote-server", gh="arii/remote-server", now=NOW,
            reader=reader,
            marker_check=lambda gh, m: marker, filer=filer)
        return result, filed

    def test_third_failure_files_once(self):
        result, filed = self._scan(_streak(3))
        self.assertEqual(len(filed), 1)
        self.assertEqual(len(result["filed"]), 1)
        self.assertEqual(result["filed"][0]["count"], 3)
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["errors"], [])

    def test_two_failures_file_nothing(self):
        result, filed = self._scan(_streak(2))
        self.assertEqual(filed, [])
        self.assertEqual(result["signatures"], 0)

    def test_old_failures_fall_out_of_window(self):
        entries = _streak(1) + [_webhook(ts=NOW - 25 * HOUR - i)
                                for i in range(2)]
        result, filed = self._scan(entries)
        self.assertEqual(filed, [])

    def test_successes_dont_count(self):
        entries = _streak(2) + [_webhook(ok=True,
                                         detail="up to date",
                                         ts=NOW - i)
                                for i in range(5)]
        result, filed = self._scan(entries)
        self.assertEqual(filed, [])

    def test_distinct_summaries_dont_group(self):
        # Rule-distinct details collapse to distinct summaries
        # (details that all map to the generic fallback would
        # share one summary, exactly like the History view).
        entries = [_webhook(detail=detail)
                   for detail in ("connection refused by host",
                                  "permission denied for key",
                                  "merge conflict in tree")]
        self.assertEqual(len({autofile.failure_signature(e)
                              for e in entries}), 3)
        result, filed = self._scan(entries)
        self.assertEqual(filed, [])
        self.assertEqual(result["signatures"], 0)

    def test_distinct_repos_dont_group(self):
        entries = [_webhook(repo="boomtick") for _ in range(3)]
        result, filed = self._scan(entries)
        self.assertEqual(filed, [])
        self.assertEqual(result["scanned"], 0)

    def test_undated_rows_ignored(self):
        entries = _streak(2)
        row = _webhook()
        del row["ts"]
        entries.append(row)
        result, filed = self._scan(entries)
        self.assertEqual(filed, [])

    def test_run_streak_files(self):
        entries = [_run(ts=NOW - i * HOUR) for i in range(3)]
        result, filed = self._scan(entries)
        self.assertEqual(len(filed), 1)
        self.assertIn("preflight", filed[0][1])


class DedupTest(unittest.TestCase):
    def _scan(self, entries, marker_check):
        filed = []

        def filer(repo, title, body):
            filed.append((repo, title, body))
            return "https://example.invalid/i/1"

        result = autofile.scan_repo(
            "remote-server", gh="arii/remote-server", now=NOW,
            reader=lambda since, limit: list(entries),
            marker_check=marker_check, filer=filer)
        return result, filed

    def test_open_marker_issue_skips_filing(self):
        seen = []
        result, filed = self._scan(
            _streak(4),
            lambda gh, m: seen.append((gh, m)) or True)
        self.assertEqual(filed, [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["count"], 4)
        self.assertEqual(seen[0][0], "arii/remote-server")
        self.assertTrue(seen[0][1].startswith(
            "auto-filed-signature: "))

    def test_close_rearms_filing(self):
        # No local stamp: the same streak files the moment the
        # marker check goes clear again.
        _, filed_shut = self._scan(
            _streak(3), lambda gh, m: True)
        _, filed_open = self._scan(
            _streak(3), lambda gh, m: False)
        self.assertEqual(filed_shut, [])
        self.assertEqual(len(filed_open), 1)

    def test_failed_marker_check_files_nothing(self):
        def explode(gh, marker):
            raise RuntimeError("gh down")

        result, filed = self._scan(_streak(3), explode)
        self.assertEqual(filed, [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("marker check failed",
                      result["errors"][0]["error"])

    def test_failed_file_lands_in_errors(self):
        def explode(repo, title, body):
            raise RuntimeError("gh down")

        result = autofile.scan_repo(
            "remote-server", gh="arii/remote-server", now=NOW,
            reader=lambda since, limit: _streak(3),
            marker_check=lambda gh, m: False, filer=explode)
        self.assertEqual(result["filed"], [])
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("file failed", result["errors"][0]["error"])

    def test_marker_stable_and_repo_scoped(self):
        key = ("webhook", "remote-server", "some summary")
        other = ("webhook", "boomtick", "some summary")
        self.assertEqual(autofile.marker_for(key),
                         autofile.marker_for(key))
        self.assertNotEqual(autofile.marker_for(key),
                            autofile.marker_for(other))
        self.assertRegex(autofile.marker_for(key),
                         r"^auto-filed-signature: [0-9a-f]{12}$")


class BodyShapeTest(unittest.TestCase):
    def test_webhook_body_shape(self):
        key = ("webhook", "remote-server", "fetch failed down")
        members = _streak(3)
        title, body = autofile.build_issue(
            key, members, "arii/remote-server")
        self.assertTrue(title.startswith("[autofile] webhook"))
        self.assertIn("x3", title)
        self.assertLessEqual(len(title), 200)
        self.assertLessEqual(len(body), 4000)
        self.assertIn("failures: 3", body)
        self.assertIn("24h", body)
        self.assertIn(autofile.marker_for(key), body)
        marker_at = body.index(autofile.marker_for(key))
        self.assertLess(marker_at, body.index("re-arms filing"))
        self.assertIn("https://github.com/arii/remote-server",
                      body)
        self.assertIn("kind filter: `webhook`", body)

    def test_run_body_links_actions(self):
        key = ("run", "remote-server",
               "preflight - td --json preflight")
        members = [_run(ts=NOW - i * HOUR) for i in range(3)]
        title, body = autofile.build_issue(
            key, members, "arii/remote-server")
        self.assertIn("https://github.com/arii/remote-server/"
                      "actions", body)
        self.assertIn("[exit 1]", body)

    def test_compare_link_needs_both_shas(self):
        key = ("webhook", "remote-server", "fetch failed down")
        good = _webhook(observed={"head": "a" * 40,
                                  "origin": "b" * 40,
                                  "ok": True})
        _, body = autofile.build_issue(
            key, [good, _webhook(), _webhook()],
            "arii/remote-server")
        self.assertIn(f"/compare/{'a' * 40}...{'b' * 40}", body)
        _, plain = autofile.build_issue(
            key, _streak(3), "arii/remote-server")
        self.assertNotIn("/compare/", plain)

    def test_long_summary_truncates_title(self):
        key = ("webhook", "remote-server", "w" * 500)
        title, _ = autofile.build_issue(
            key, _streak(3), "arii/remote-server")
        self.assertLessEqual(len(title), 200)

    def test_huge_body_keeps_marker(self):
        key = ("webhook", "remote-server", "s" * 200)
        members = [_webhook(detail="d" * 200,
                            ts=NOW - i) for i in range(50)]
        _, body = autofile.build_issue(
            key, members, "arii/remote-server")
        self.assertLessEqual(len(body), 4000)
        self.assertIn(autofile.marker_for(key), body)


class MarkerSearchTest(unittest.TestCase):
    def _check(self, stdout, seen):
        from types import SimpleNamespace

        class Runner:
            @staticmethod
            def run(argv, **kw):
                seen.append(argv)
                return SimpleNamespace(
                    returncode=0, stdout=stdout, stderr="")

        return autofile.open_issue_has_marker(
            "arii/remote-server", "auto-filed-signature: abc123",
            runner=Runner)

    def test_search_argv_and_verdicts(self):
        seen = []
        self.assertTrue(self._check('[{"number": 7}]', seen))
        self.assertFalse(self._check('[]', seen))
        self.assertEqual(seen[0][:2], ["gh", "issue"])
        self.assertIn("--search", seen[0])
        self.assertIn("auto-filed-signature: abc123", seen[0])
        self.assertIn("--state", seen[0])
        self.assertIn("open", seen[0])


class EngineWiringTest(unittest.TestCase):
    def test_action_registered(self):
        self.assertIn("autofile-scan", engine.ALLOWED_ACTIONS)
        self.assertIn("autofile-scan", engine.REPO_SCOPED_ACTIONS)
        self.assertNotIn("autofile-scan",
                         engine.DESTRUCTIVE_ACTIONS)

    def test_action_steps(self):
        steps, err = engine.action_steps(
            "autofile-scan", "remote-server",
            "arii/remote-server", "/tmp")
        self.assertIsNone(err)
        self.assertEqual(steps, [[sys.executable, "-m",
                                  "backend.autofile_scan",
                                  "--repo", "remote-server",
                                  "--gh", "arii/remote-server"]])

    def test_action_steps_needs_repo(self):
        steps, err = engine.action_steps(
            "autofile-scan", "remote-server", "", "/tmp")
        self.assertIsNone(steps)
        self.assertIn("needs a repo", err)


class CliRunnerTest(unittest.TestCase):
    def test_main_prints_audit_and_returns_zero(self):
        result = {"repo": "r", "gh": "arii/r", "scanned": 9,
                  "signatures": 2,
                  "filed": [{"signature": "a" * 12, "count": 3,
                             "marker": "m",
                             "url": "https://example.invalid/1"}],
                  "skipped": [{"signature": "b" * 12, "count": 5,
                               "marker": "m"}],
                  "errors": [{"signature": "c" * 12,
                              "error": "file failed: x"}]}
        with patch("backend.autofile.scan_repo",
                   return_value=result) as scan:
            with patch("builtins.print") as shown:
                code = autofile_scan.main(
                    ["--repo", "r", "--gh", "arii/r"])
        self.assertEqual(code, 0)
        scan.assert_called_once_with("r", gh="arii/r")
        text = "\n".join(call.args[0] for call in
                         shown.call_args_list)
        self.assertIn("filed", text)
        self.assertIn("skipped=1", text)
        self.assertIn("errors=1", text)


class AutofileSeedTest(unittest.TestCase):
    """Daily autofile seeds (issue #825). Discovery is stubbed;
    the store + sentinel live in tmp."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "sched.json")
        self.old = os.environ.get("BUTTONS_JOBS_SCHEDULE_PATH")
        os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = self.path

    def tearDown(self):
        if self.old is None:
            os.environ.pop("BUTTONS_JOBS_SCHEDULE_PATH", None)
        else:
            os.environ["BUTTONS_JOBS_SCHEDULE_PATH"] = self.old
        self.tmp.cleanup()

    def _seeded(self, repos):
        with patch.object(jobs_mod, "_notify_seed_repos",
                          return_value=list(repos)):
            return jobs_mod._ensure_default_autofile_schedules(
                now=10000.0)

    def test_seed_one_per_repo_daily_staggered_offset(self):
        count = self._seeded(["remote-server", "boomtick"])
        self.assertEqual(count, 2)
        rows = jobs_mod.list_action_schedules()["schedules"]
        self.assertEqual(len(rows), 2)
        by_repo = {r["repo"]: r for r in rows}
        for repo in ("remote-server", "boomtick"):
            self.assertEqual(by_repo[repo]["action"],
                             "autofile-scan")
            self.assertEqual(by_repo[repo]["cadence"], "daily")
            self.assertEqual(by_repo[repo]["td_argv"], [])
            self.assertIsNone(by_repo[repo]["last_run"])
        first = min(r["next_run"] for r in rows)
        second = max(r["next_run"] for r in rows)
        offset = jobs_mod.AUTOFILE_SEED_OFFSET
        self.assertEqual(first, 10000.0 + offset)
        self.assertEqual(second, 10000.0 + offset
                         + jobs_mod.NOTIFY_SEED_STAGGER)

    def test_seed_is_idempotent_and_deletes_stick(self):
        self.assertEqual(self._seeded(["remote-server"]), 1)
        self.assertEqual(self._seeded(["remote-server"]), 0)
        rows = jobs_mod.list_action_schedules()["schedules"]
        self.assertEqual(len(rows), 1)
        jobs_mod.delete_action_schedule(rows[0]["id"])
        self.assertEqual(self._seeded(["remote-server"]), 0)

    def test_seed_uses_own_sentinel(self):
        self.assertEqual(self._seeded(["remote-server"]), 1)
        with patch.object(
                jobs_mod, "_notify_seed_repos",
                return_value=["remote-server"]):
            self.assertEqual(
                jobs_mod._ensure_default_notify_schedules(
                    now=10000.0), 1)
        rows = jobs_mod.list_action_schedules()["schedules"]
        self.assertEqual(
            sorted(r["action"] for r in rows),
            ["autofile-scan", "check-notifications"])

    def test_seed_nothing_without_repos(self):
        self.assertEqual(self._seeded([]), 0)


if __name__ == "__main__":
    unittest.main()
