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


if __name__ == "__main__":
    unittest.main()
