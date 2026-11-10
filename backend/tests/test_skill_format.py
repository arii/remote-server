"""Acceptance tests freezing the skill file format (issue #1253).

Contract source: the issue text only. These tests pin the public
interface ``backend.skill_format.validate_skill`` /
``validate_skill_file`` behavior, not implementation internals.
"""

import copy
import json
import os
import tempfile
import unittest

from backend.skill_format import validate_skill, validate_skill_file


def make_example_skill():
    """Build the contract's canonical fill-contact-form v1 example."""
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
        ],
    }


class ValidateSkillContractTest(unittest.TestCase):
    def test_example_skill_validates(self):
        """Contract: the fill-contact-form v1 example MUST validate."""
        errors = validate_skill(make_example_skill())
        self.assertEqual(errors, [])

    def test_missing_selector_rejected(self):
        """Contract: every step needs a selector; a step without one is invalid."""
        skill = make_example_skill()
        del skill["steps"][1]["selector"]
        self.assertTrue(validate_skill(skill))

    def test_missing_check_rejected(self):
        """Contract: every step has one check; a step without a check is invalid."""
        skill = make_example_skill()
        del skill["steps"][0]["check"]
        self.assertTrue(validate_skill(skill))

    def test_unknown_check_type_rejected(self):
        """Contract: check types are only url_contains/element_visible/text_contains/value_equals."""
        skill = make_example_skill()
        bad = copy.deepcopy(skill)
        bad["steps"][0]["check"] = {"type": "color_equals", "expected": "blue"}
        self.assertTrue(validate_skill(bad))

    def test_unknown_action_rejected(self):
        """Contract: actions are only the drive ops navigate/click/type/key/wheel."""
        skill = make_example_skill()
        bad = copy.deepcopy(skill)
        bad["steps"][0]["action"] = "hover"
        self.assertTrue(validate_skill(bad))

    def test_non_sequential_n_rejected(self):
        """Contract: steps run n=1..N in order; gaps are invalid."""
        skill = make_example_skill()
        bad = copy.deepcopy(skill)
        bad["steps"][1]["n"] = 3
        self.assertTrue(validate_skill(bad))

    def test_xpath_selector_rejected(self):
        """Contract: selectors never use XPath."""
        skill = make_example_skill()
        bad = copy.deepcopy(skill)
        bad["steps"][1]["selector"] = "//button[text()='Submit']"
        self.assertTrue(validate_skill(bad))

    def test_bare_text_selector_rejected(self):
        """Contract (issue #1253): selectors use data-testid or ARIA role first; never XPath, never bare text — "Submit" is bare text, rejected."""
        skill = make_example_skill()
        bad = copy.deepcopy(skill)
        bad["steps"][1]["selector"] = "Submit"
        self.assertTrue(validate_skill(bad))

    def test_bare_known_tag_button_valid(self):
        """Contract (issue #1253): selectors use data-testid or ARIA role first; never XPath, never bare text — bare known-tag "button" stays valid."""
        skill = make_example_skill()
        good = copy.deepcopy(skill)
        good["steps"][1]["selector"] = "button"
        self.assertEqual(validate_skill(good), [])

    def test_bare_known_tag_div_valid(self):
        """Contract (issue #1253): selectors use data-testid or ARIA role first; never XPath, never bare text — bare known-tag "div" stays valid."""
        skill = make_example_skill()
        good = copy.deepcopy(skill)
        good["steps"][1]["selector"] = "div"
        self.assertEqual(validate_skill(good), [])

    def test_nonsense_word_selector_rejected(self):
        """Contract (issue #1253): selectors use data-testid or ARIA role first; never XPath, never bare text — nonsense word "frobnicate" is bare text, rejected."""
        skill = make_example_skill()
        bad = copy.deepcopy(skill)
        bad["steps"][1]["selector"] = "frobnicate"
        self.assertTrue(validate_skill(bad))

    def test_empty_steps_rejected(self):
        """Contract: a skill needs at least one step; empty steps is invalid."""
        skill = make_example_skill()
        skill["steps"] = []
        self.assertTrue(validate_skill(skill))

    def test_version_zero_rejected(self):
        """Contract: version is int >= 1; version 0 is invalid."""
        skill = make_example_skill()
        skill["version"] = 0
        self.assertTrue(validate_skill(skill))

    def test_non_http_source_url_rejected(self):
        """Contract: source_url must be an http(s) URL; non-http is invalid."""
        skill = make_example_skill()
        skill["source_url"] = "ftp://example.com/form"
        self.assertTrue(validate_skill(skill))

    def test_type_step_without_value_rejected(self):
        """Contract: a type step carries the text to type; without value it is invalid."""
        skill = make_example_skill()
        bad = copy.deepcopy(skill)
        del bad["steps"][0]["value"]
        self.assertTrue(validate_skill(bad))

    def test_bad_json_file_rejected(self):
        """Contract: validate_skill_file reports file content that is not valid JSON/skill."""
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write("{not valid json,,,")
            errors = validate_skill_file(path)
            self.assertTrue(errors)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def test_example_skill_file_validates(self):
        """Contract: the fill-contact-form v1 example validates from a JSON file too."""
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(make_example_skill(), fh)
            self.assertEqual(validate_skill_file(path), [])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
