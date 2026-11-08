"""deep-agent/triage_common.py unit tests. Stdlib only: no langchain,
no subprocess, no network. Run: python3 tests/test_triage_common.py"""
import unittest
from types import SimpleNamespace
from unittest import mock

import triage_common


def msg(*names):
    return SimpleNamespace(tool_calls=[{"name": n} for n in names])


class TestValidRepo(unittest.TestCase):
    def test_accepts_owner_name(self):
        self.assertTrue(triage_common.valid_repo("arii/tech-dancer"))
        self.assertTrue(triage_common.valid_repo("a/b.c-d_e"))

    def test_rejects_bad_shapes(self):
        for bad in ("", "noslash", "a/b c", "a/", "a/b/c", None, 42):
            self.assertFalse(triage_common.valid_repo(bad), bad)


class TestFetchTriageCounts(unittest.TestCase):
    def test_rejects_bad_repo_without_subprocess(self):
        with mock.patch.object(
            triage_common.subprocess, "run"
        ) as run_mock:
            out = triage_common.fetch_triage_counts("noslash")
        self.assertEqual(out, triage_common.REJECTED_REPO)
        run_mock.assert_not_called()

    def test_combines_stdout_and_stderr(self):
        proc = SimpleNamespace(stdout="counts\n", stderr="warn\n")
        with mock.patch.object(
            triage_common.subprocess, "run", return_value=proc
        ) as run_mock:
            out = triage_common.fetch_triage_counts("arii/tech-dancer")
        run_mock.assert_called_once_with(
            ["td", "agent", "suggest", "-r", "arii/tech-dancer"],
            capture_output=True,
            text=True,
            timeout=triage_common.TRIAGE_TIMEOUT,
        )
        self.assertEqual(out, "counts\nwarn")

    def test_output_capped(self):
        proc = SimpleNamespace(stdout="x" * 2000, stderr="")
        with mock.patch.object(
            triage_common.subprocess, "run", return_value=proc
        ):
            out = triage_common.fetch_triage_counts("a/b")
        self.assertEqual(len(out), triage_common.TRIAGE_OUTPUT_LIMIT)


class TestCountToolCalls(unittest.TestCase):
    def test_counts_all_without_filter(self):
        messages = [msg("task", "task"), msg("triage_counts"), msg()]
        self.assertEqual(triage_common.count_tool_calls(messages), 3)

    def test_filters_by_name(self):
        messages = [msg("task", "other"), msg("task")]
        self.assertEqual(
            triage_common.count_tool_calls(messages, "task"), 2
        )
        self.assertEqual(
            triage_common.count_tool_calls(messages, "missing"), 0
        )

    def test_tolerates_missing_or_empty(self):
        self.assertEqual(triage_common.count_tool_calls([]), 0)
        self.assertEqual(triage_common.count_tool_calls(None), 0)
        plain = SimpleNamespace(content="no calls here")
        self.assertEqual(triage_common.count_tool_calls([plain]), 0)
        none_calls = SimpleNamespace(tool_calls=None)
        self.assertEqual(triage_common.count_tool_calls([none_calls]), 0)


if __name__ == "__main__":
    unittest.main()
