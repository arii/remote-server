"""tracing.py unit tests. No network, no langchain needed (the
tracer import is guarded). Run: python3 tests/test_tracing.py"""
import os
import unittest
from unittest import mock

import tracing


class TestTracingCallbacks(unittest.TestCase):
    def callbacks(self, env):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.update(env)
            return tracing.tracing_callbacks()

    def test_disabled_by_default(self):
        self.assertEqual(self.callbacks({}), [])

    def test_tracing_true_but_no_key(self):
        self.assertEqual(
            self.callbacks({"LANGSMITH_TRACING": "true"}), [])

    def test_key_but_not_enabled(self):
        self.assertEqual(
            self.callbacks({"LANGSMITH_API_KEY": "dummy"}), [])

    def test_enabled(self):
        tracer = object()
        with mock.patch.object(tracing, "LangChainTracer",
                               return_value=tracer) as cls:
            got = self.callbacks({"LANGSMITH_TRACING": "true",
                                  "LANGSMITH_API_KEY": "dummy",
                                  "LANGSMITH_PROJECT": "proj"})
        self.assertEqual(got, [tracer])
        cls.assert_called_once_with(project_name="proj")

    def test_default_project(self):
        with mock.patch.object(tracing, "LangChainTracer") as cls:
            self.callbacks({"LANGSMITH_TRACING": "true",
                            "LANGSMITH_API_KEY": "dummy"})
        cls.assert_called_once_with(project_name=None)

    def test_missing_library_is_silent(self):
        with mock.patch.object(tracing, "LangChainTracer", None):
            got = self.callbacks({"LANGSMITH_TRACING": "true",
                                  "LANGSMITH_API_KEY": "dummy"})
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
