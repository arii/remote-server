"""Run summaries carry the turn repo (issue #933)."""

import importlib.util
import os
import sys
import unittest


def _load_runs():
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(os.path.dirname(here))
    deep_agent = os.path.join(root, "deep-agent")
    sys.path.insert(0, deep_agent)
    try:
        spec = importlib.util.spec_from_file_location(
            "runs_repo_summary", os.path.join(deep_agent, "runs.py"))
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(deep_agent)


runs = _load_runs()


def _row(**over):
    row = {"script": "deep_chat.py", "model": "ollama:qwen3:8b",
           "thread_id": "t", "ts": 1, "usage": {"total_tokens": 3},
           "final_preview": "hi", "extra": {}}
    row.update(over)
    return row


class RunRepoSummaryTest(unittest.TestCase):
    def test_summary_carries_extra_repo(self):
        summary = runs._entry_summary(
            0, _row(extra={"repo": "arii/remote-server"}))
        self.assertEqual(summary["repo"], "arii/remote-server")

    def test_summary_repo_null_for_legacy_rows(self):
        self.assertIsNone(runs._entry_summary(0, _row())["repo"])
        self.assertIsNone(
            runs._entry_summary(0, _row(extra={"repo": ""}))["repo"])
        self.assertIsNone(
            runs._entry_summary(0, _row(extra={"repo": 7}))["repo"])
        self.assertIsNone(
            runs._entry_summary(0, _row(extra=None))["repo"])


if __name__ == "__main__":
    unittest.main()
