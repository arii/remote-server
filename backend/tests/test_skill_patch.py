"""Acceptance tests for the skill correction patch (issue #1256).

Contract source: the issue text only. These tests pin the public
interface ``backend.skill_patch.apply_patch`` behavior, not
implementation internals. The implementer wrote
``backend/skill_patch.py``; this file was authored without reading it.
"""

import copy
import json
import unittest

from backend.skill_format import validate_skill
from backend.skill_patch import apply_patch


def make_example_skill():
    """Build the contract's fill-contact-form v1 example plus a step 3.

    Steps 1-2 mirror the frozen-format example; step 3 is added so the
    "steps 1 and 3 unchanged" assertion is meaningful.
    """
    return {
        "skill": "fill-contact-form",
        "version": 1,
        "source_url": "https://example.com/form",
        "steps": [
            {
                "n": 1,
                "action": "type",
                "selector": "[data-testid='name']",
                "value": "Ariel Anders",
                "check": {"type": "value_equals", "expected": "Ariel Anders"},
            },
            {
                "n": 2,
                "action": "click",
                "selector": "[data-testid='submit']",
                "check": {"type": "url_contains", "expected": "confirmation"},
            },
            {
                "n": 3,
                "action": "click",
                "selector": "[data-testid='done']",
                "check": {
                    "type": "element_visible",
                    "expected": "[data-testid='done']",
                },
            },
        ],
    }


def assert_success_shape(testcase, result):
    """Check the success return shape without pinning key names beyond it."""
    testcase.assertNotIn("error", result)
    testcase.assertIn("skill", result)
    testcase.assertIn("log", result)
    testcase.assertIsInstance(result["skill"], dict)
    testcase.assertIsInstance(result["log"], dict)


def assert_failure_shape(testcase, result, original):
    """Check the failure return shape: non-empty error, version unbumped."""
    testcase.assertIn("error", result)
    testcase.assertTrue(result["error"])
    if "skill" in result and isinstance(result["skill"], dict):
        testcase.assertEqual(result["skill"].get("version"), original["version"])


def assert_log_covers(
    testcase,
    log,
    *,
    skill,
    from_version,
    to_version,
    step,
    field,
    old,
    new,
    by,
    why,
):
    """Check the log records every contract item without pinning key names."""
    blob = json.dumps(log, default=str)
    testcase.assertIn(skill, blob)
    testcase.assertIn(str(from_version), blob)
    testcase.assertIn(str(to_version), blob)
    testcase.assertIn(str(step), blob)
    testcase.assertIn(field, blob)
    testcase.assertIn(str(old), blob)
    testcase.assertIn(str(new), blob)
    testcase.assertIn(by, blob)
    testcase.assertIn(why, blob)
    ts_keys = [
        key
        for key in log
        if "time" in key.lower()
        or "date" in key.lower()
        or "stamp" in key.lower()
        or key.lower() in ("ts", "at", "when")
        or key.lower().endswith("_at")
    ]
    testcase.assertTrue(ts_keys, "log entry must record a timestamp")
    testcase.assertTrue(any(log[key] for key in ts_keys))


class SelectorPatchAcceptanceTest(unittest.TestCase):
    def test_selector_patch_on_step2_bumps_version_1_to_2(self):
        """Contract (issue #1256 acceptance): version goes from 1 to 2."""
        skill = make_example_skill()
        patch = {
            "step": 2,
            "selector": "[data-testid='submit-v2']",
            "by": "ari",
            "why": "submit button renamed",
        }
        result = apply_patch(skill, patch)
        assert_success_shape(self, result)
        self.assertEqual(result["skill"]["version"], 2)

    def test_selector_patch_on_step2_leaves_steps_1_and_3_unchanged(self):
        """Contract (issue #1256 acceptance): patching step 2 leaves steps 1 and 3 unchanged."""
        skill = make_example_skill()
        patch = {
            "step": 2,
            "selector": "[data-testid='submit-v2']",
            "by": "ari",
            "why": "submit button renamed",
        }
        result = apply_patch(skill, patch)
        assert_success_shape(self, result)
        new_skill = result["skill"]
        self.assertEqual(new_skill["steps"][0], skill["steps"][0])
        self.assertEqual(new_skill["steps"][2], skill["steps"][2])
        self.assertEqual(new_skill["steps"][1]["selector"], "[data-testid='submit-v2']")
        self.assertEqual(new_skill["steps"][1]["action"], skill["steps"][1]["action"])
        self.assertEqual(new_skill["steps"][1]["check"], skill["steps"][1]["check"])
        self.assertEqual(new_skill["steps"][1]["n"], 2)
        self.assertEqual(new_skill["skill"], skill["skill"])
        self.assertEqual(new_skill["source_url"], skill["source_url"])
        self.assertEqual(len(new_skill["steps"]), len(skill["steps"]))

    def test_selector_patched_skill_still_validates(self):
        """Contract (issue #1256 success): the patched skill still validates."""
        skill = make_example_skill()
        patch = {
            "step": 2,
            "selector": "[data-testid='submit-v2']",
            "by": "ari",
            "why": "submit button renamed",
        }
        result = apply_patch(skill, patch)
        assert_success_shape(self, result)
        self.assertEqual(validate_skill(result["skill"]), [])


class CheckPatchTest(unittest.TestCase):
    def test_check_patch_on_step2_replaces_only_the_check(self):
        """Contract (issue #1256 success): one call patches one check; everything else identical."""
        skill = make_example_skill()
        new_check = {"type": "text_contains", "expected": "Thank you"}
        patch = {
            "step": 2,
            "check": new_check,
            "by": "ari",
            "why": "confirmation page shows banner",
        }
        result = apply_patch(skill, patch)
        assert_success_shape(self, result)
        new_skill = result["skill"]
        self.assertEqual(new_skill["version"], skill["version"] + 1)
        self.assertEqual(new_skill["steps"][1]["check"], new_check)
        self.assertEqual(
            new_skill["steps"][1]["selector"], skill["steps"][1]["selector"]
        )
        self.assertEqual(new_skill["steps"][1]["action"], skill["steps"][1]["action"])
        self.assertEqual(new_skill["steps"][0], skill["steps"][0])
        self.assertEqual(new_skill["steps"][2], skill["steps"][2])
        self.assertEqual(validate_skill(new_skill), [])


class ValuePatchTest(unittest.TestCase):
    def test_value_patch_on_type_step1_replaces_only_the_value(self):
        """Contract (issue #1256 success): one call patches one value on a type step."""
        skill = make_example_skill()
        patch = {
            "step": 1,
            "value": "Jordan Lee",
            "by": "ari",
            "why": "example name updated",
        }
        result = apply_patch(skill, patch)
        assert_success_shape(self, result)
        new_skill = result["skill"]
        self.assertEqual(new_skill["version"], skill["version"] + 1)
        self.assertEqual(new_skill["steps"][0]["value"], "Jordan Lee")
        self.assertEqual(
            new_skill["steps"][0]["selector"], skill["steps"][0]["selector"]
        )
        self.assertEqual(new_skill["steps"][0]["check"], skill["steps"][0]["check"])
        self.assertEqual(new_skill["steps"][1], skill["steps"][1])
        self.assertEqual(new_skill["steps"][2], skill["steps"][2])
        self.assertEqual(validate_skill(new_skill), [])


class PatchFailureTest(unittest.TestCase):
    def test_two_patch_fields_at_once_rejected(self):
        """Contract (issue #1256 failure): two patch fields at once applies nothing."""
        skill = make_example_skill()
        snapshot = copy.deepcopy(skill)
        patch = {
            "step": 2,
            "selector": "[data-testid='submit-v2']",
            "value": "extra",
            "by": "ari",
            "why": "two fields at once",
        }
        result = apply_patch(skill, patch)
        assert_failure_shape(self, result, snapshot)
        self.assertEqual(skill, snapshot)

    def test_missing_by_rejected(self):
        """Contract (issue #1256 failure): missing by applies nothing."""
        skill = make_example_skill()
        snapshot = copy.deepcopy(skill)
        patch = {
            "step": 2,
            "selector": "[data-testid='submit-v2']",
            "why": "no author given",
        }
        result = apply_patch(skill, patch)
        assert_failure_shape(self, result, snapshot)
        self.assertEqual(skill, snapshot)

    def test_missing_why_rejected(self):
        """Contract (issue #1256 failure): missing why applies nothing."""
        skill = make_example_skill()
        snapshot = copy.deepcopy(skill)
        patch = {"step": 2, "selector": "[data-testid='submit-v2']", "by": "ari"}
        result = apply_patch(skill, patch)
        assert_failure_shape(self, result, snapshot)
        self.assertEqual(skill, snapshot)

    def test_empty_by_rejected(self):
        """Contract (issue #1256 patch shape): by must be non-empty."""
        skill = make_example_skill()
        snapshot = copy.deepcopy(skill)
        patch = {
            "step": 2,
            "selector": "[data-testid='submit-v2']",
            "by": "",
            "why": "empty author",
        }
        result = apply_patch(skill, patch)
        assert_failure_shape(self, result, snapshot)
        self.assertEqual(skill, snapshot)

    def test_empty_why_rejected(self):
        """Contract (issue #1256 patch shape): why must be non-empty."""
        skill = make_example_skill()
        snapshot = copy.deepcopy(skill)
        patch = {
            "step": 2,
            "selector": "[data-testid='submit-v2']",
            "by": "ari",
            "why": "",
        }
        result = apply_patch(skill, patch)
        assert_failure_shape(self, result, snapshot)
        self.assertEqual(skill, snapshot)

    def test_unknown_step_rejected(self):
        """Contract (issue #1256 failure): unknown step n applies nothing."""
        skill = make_example_skill()
        snapshot = copy.deepcopy(skill)
        patch = {
            "step": 99,
            "selector": "[data-testid='nope']",
            "by": "ari",
            "why": "no such step",
        }
        result = apply_patch(skill, patch)
        assert_failure_shape(self, result, snapshot)
        self.assertEqual(skill, snapshot)

    def test_incomplete_check_dict_rejected(self):
        """Contract (issue #1256 failure): check must be a complete type/expected pair."""
        skill = make_example_skill()
        snapshot = copy.deepcopy(skill)
        patch = {
            "step": 2,
            "check": {"type": "url_contains"},
            "by": "ari",
            "why": "half a check",
        }
        result = apply_patch(skill, patch)
        assert_failure_shape(self, result, snapshot)
        self.assertEqual(skill, snapshot)

    def test_patch_making_skill_invalid_rejected(self):
        """Contract (issue #1256 failure): a patch that would make the skill invalid applies nothing."""
        skill = make_example_skill()
        snapshot = copy.deepcopy(skill)
        patch = {"step": 2, "selector": "", "by": "ari", "why": "blank selector"}
        result = apply_patch(skill, patch)
        assert_failure_shape(self, result, snapshot)
        self.assertEqual(skill, snapshot)


class InputImmutabilityTest(unittest.TestCase):
    def test_input_skill_not_mutated_by_successful_patch(self):
        """Contract (issue #1256): the input skill dict is never mutated."""
        skill = make_example_skill()
        snapshot = copy.deepcopy(skill)
        patch = {
            "step": 2,
            "selector": "[data-testid='submit-v2']",
            "by": "ari",
            "why": "submit button renamed",
        }
        result = apply_patch(skill, patch)
        assert_success_shape(self, result)
        self.assertEqual(skill, snapshot)
        self.assertIsNot(result["skill"], skill)


class PatchLogTest(unittest.TestCase):
    def test_log_entry_records_versions_step_field_old_new_by_why_and_timestamp(self):
        """Contract (issue #1256 success): the log records name, versions, step, field, old/new, by, why, timestamp."""
        skill = make_example_skill()
        patch = {
            "step": 2,
            "selector": "[data-testid='submit-v2']",
            "by": "ari",
            "why": "submit button renamed",
        }
        result = apply_patch(skill, patch)
        assert_success_shape(self, result)
        assert_log_covers(
            self,
            result["log"],
            skill="fill-contact-form",
            from_version=1,
            to_version=2,
            step=2,
            field="selector",
            old="[data-testid='submit']",
            new="[data-testid='submit-v2']",
            by="ari",
            why="submit button renamed",
        )


if __name__ == "__main__":
    unittest.main()
