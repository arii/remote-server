# Catalog failures classify as LIMITED (transient/requeue), not BLOCKED_DETERMINISTIC.
import unittest

import pipeline as pipeline_mod


class TestClassifyFailureCatalog(unittest.TestCase):
    def test_catalog_causes_are_limited(self):
        classify = pipeline_mod.Pipeline._classify_failure
        reasons = [
            "No provider available. google: no synced eligible chat model matching requested model/free-tier/tools/JSON capabilities; run twt sync",
            "upstream HTTP 403",
            "upstream payment required (HTTP 402)",
            "upstream HTTP 503",
            "upstream key limit: paid attempt refused at $0 cap",
            "timeout",
            "google: timeout",
            "catalog model google/gemini-3.8-flash is not served by google",
            "provider mismatch for google/gemini-3.8-flash",
        ]
        for reason in reasons:
            with self.subTest(reason=reason):
                self.assertEqual(classify(None, reason), pipeline_mod.LIMITED)

    def test_deterministic_failures_stay_blocked(self):
        classify = pipeline_mod.Pipeline._classify_failure
        for reason in (
            "CoderBudgetExceeded: tool budget exhausted",
            "pre-dispatch check failed: missing brief",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(classify(None, reason), pipeline_mod.BLOCKED)
