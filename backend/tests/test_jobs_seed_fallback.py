"""Seed-fallback attribution (audit fallback batch).

Every default seed helper in backend/api/routers/jobs.py never
raises: on any exception it logs a warning naming the seed and
reports 0. Discovery (_notify_seed_repos, the first call every
seed makes) is forced to raise so the tests exercise the fallback
path without touching git or the schedule store.
"""

import unittest
from unittest.mock import patch

from backend.api.routers import jobs as jobs_mod


class SeedFallbackWarnTest(unittest.TestCase):
    def _assert_fallback(self, seed_fn, seed_name):
        with patch.object(
            jobs_mod,
            "_notify_seed_repos",
            side_effect=RuntimeError("discovery down"),
        ):
            with self.assertLogs(jobs_mod.__name__, level="WARNING") as logs:
                self.assertEqual(seed_fn(now=10000.0), 0)
        warnings = [line for line in logs.output if f"{seed_name} seed failed" in line]
        self.assertTrue(
            warnings,
            f"expected a {seed_name!r} seed-failed warning, got: {logs.output}",
        )
        self.assertTrue(
            any("discovery down" in line for line in warnings),
            f"warning names no cause, got: {logs.output}",
        )

    def test_notify_seed_failure_warns_and_reports_zero(self):
        self._assert_fallback(jobs_mod._ensure_default_notify_schedules, "notify")

    def test_autofile_seed_failure_warns_and_reports_zero(self):
        self._assert_fallback(jobs_mod._ensure_default_autofile_schedules, "autofile")

    def test_impact_seed_failure_warns_and_reports_zero(self):
        self._assert_fallback(jobs_mod._ensure_default_impact_schedules, "impact")

    def test_supervise_seed_failure_warns_and_reports_zero(self):
        self._assert_fallback(jobs_mod._ensure_default_supervise_schedules, "supervise")


if __name__ == "__main__":
    unittest.main()
