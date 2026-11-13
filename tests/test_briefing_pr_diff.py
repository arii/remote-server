import subprocess
import unittest
from unittest import mock

from backend import briefing_summary


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
            argv, ["td", "--json", "gh", "pr-diff", "--pr", "153",
                  "--repo", "arii/remote-server"])
        self.assertIn("hi", out)

    def test_empty_on_missing_difftext(self):
        with mock.patch.object(
                briefing_summary.subprocess, "run",
                return_value=self._completed('{"status": "success"}')):
            self.assertEqual(briefing_summary.fetch_pr_diff(153), "")


class TestRunJsonErrorDetail(unittest.TestCase):
    def _failed(self, stderr, stdout=""):
        return subprocess.CompletedProcess(
            args=["td"], returncode=1, stdout=stdout, stderr=stderr)

    def run_err(self, stderr, stdout=""):
        with mock.patch.object(
                briefing_summary.subprocess, "run",
                return_value=self._failed(stderr, stdout)):
            _, _, err = briefing_summary.run_json(["td"], 5)
        return err

    def test_plain_line_unchanged(self):
        self.assertEqual(self.run_err("boom went wrong\n"), "boom went wrong")

    def test_json_blob_message_extracted(self):
        # td --json failures print a JSON error blob ending in a bare
        # brace, which used to render as e.g. 'Overlap scan
        # unavailable: }'. Take the blob's message, not a raw line.
        self.assertEqual(
            self.run_err('{\n  "message": "nope"\n}\n'),
            "nope")

    def test_json_blob_none_message_falls_back_to_type(self):
        self.assertEqual(
            self.run_err('{"status": "error", "message": null,'
                         ' "type": "AssertionError"}'),
            "AssertionError")

    def test_falls_back_to_stdout(self):
        self.assertEqual(
            self.run_err("", stdout='{\n"oops": true}\n'),
            '"oops": true}')

    def test_all_braces_gives_exit(self):
        self.assertEqual(self.run_err("{\n}\n"), "exit 1")
        failed = subprocess.CompletedProcess(
            args=["td"], returncode=1, stdout="", stderr="boom")
        with mock.patch.object(
                briefing_summary.subprocess, "run",
                return_value=failed):
            self.assertEqual(briefing_summary.fetch_pr_diff(153), "")


if __name__ == "__main__":
    unittest.main()
