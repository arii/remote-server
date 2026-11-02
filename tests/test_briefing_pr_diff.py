import subprocess
import unittest
from unittest import mock

from buttons import briefing_summary


class TestFetchPrDiff(unittest.TestCase):
    def _completed(self, stdout):
        return subprocess.CompletedProcess(
            args=["td"], returncode=0, stdout=stdout, stderr="")

    def test_uses_td_pr_diff_json(self):
        payload = ('{"status": "success", "diffText": '
                   '"diff --git a/x b/x\\n+hi"}')
        with mock.patch.object(
                briefing_summary.subprocess, "run",
                return_value=self._completed(payload)) as m:
            out = briefing_summary.fetch_pr_diff(153)
        argv = m.call_args[0][0]
        self.assertEqual(
            argv, ["td", "--json", "gh", "pr-diff", "--pr", "153"])
        self.assertIn("hi", out)

    def test_empty_on_missing_difftext(self):
        with mock.patch.object(
                briefing_summary.subprocess, "run",
                return_value=self._completed('{"status": "success"}')):
            self.assertEqual(briefing_summary.fetch_pr_diff(153), "")
        failed = subprocess.CompletedProcess(
            args=["td"], returncode=1, stdout="", stderr="boom")
        with mock.patch.object(
                briefing_summary.subprocess, "run",
                return_value=failed):
            self.assertEqual(briefing_summary.fetch_pr_diff(153), "")


if __name__ == "__main__":
    unittest.main()
