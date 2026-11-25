import unittest
from pathlib import Path

import yaml


class AiReviewGateTests(unittest.TestCase):
    def test_review_step_is_sha_pinned_and_gated(self):
        workflow = yaml.safe_load(Path(".github/workflows/ai-review.yml").read_text())
        steps = workflow["jobs"]["ai-review"]["steps"]
        matches = [
            s for s in steps if str(s.get("uses", "")).startswith("arii/boomtick@")
        ]
        self.assertEqual(len(matches), 1)
        step = matches[0]
        self.assertRegex(step["uses"], r"^arii/boomtick@[0-9a-f]{40}$")
        self.assertEqual(step["with"]["fail-on-verdict"], "true")
        self.assertNotIn("continue-on-error", workflow["jobs"]["ai-review"])
        self.assertNotIn("continue-on-error", step)

    def test_owned_action_uses_reviewed_main_not_retired_feature_pin(self):
        source = Path(".github/workflows/ai-review.yml").read_text()
        self.assertIn("arii/boomtick@aab29dc2c3339b3420af9cfd1c46b63a3182da41", source)
        self.assertNotIn("8a413b25a47cf251f6472c46becfeeba7fbb3e67", source)
        self.assertNotIn("re-pin to the boomtick main", source)

    def test_missing_review_dependency_is_not_masked(self):
        workflow = yaml.safe_load(Path(".github/workflows/ai-review.yml").read_text())
        steps = workflow["jobs"]["ai-review"]["steps"]
        preflight = next(
            s for s in steps if s.get("name") == "Verify review dependency"
        )
        self.assertEqual(preflight["run"], "command -v td")
        self.assertNotIn("continue-on-error", preflight)
        review_index = next(
            i
            for i, s in enumerate(steps)
            if str(s.get("uses", "")).startswith("arii/boomtick@")
        )
        self.assertLess(steps.index(preflight), review_index)


if __name__ == "__main__":
    unittest.main()
