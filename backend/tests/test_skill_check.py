"""Acceptance tests for the deterministic checker library (issue #1254).

Contract source: the issue text plus the task contract only. These tests
pin the public interface ``backend.skill_check.check_step`` /
``backend.skill_check.match_selector`` behavior, not implementation
internals. The implementer wrote ``backend/skill_check.py``; this file was
authored without reading it.
"""

import os
import unittest

from backend.skill_check import check_step, match_selector


def make_form_page():
    """Build the contract's canned form page.

    Page shape: {"url", "title", "elements": [{tag, text, x, y, w, h,
    tid, id, name, role, cls, val}]} — all element fields optional.
    Canned page has an INPUT with tid "name" and a value, and a BUTTON
    with tid "submit" and text "Submit".
    """
    return {
        "url": "https://example.com/form",
        "title": "Contact form",
        "elements": [
            {
                "tag": "INPUT",
                "text": "",
                "x": 100,
                "y": 200,
                "w": 200,
                "h": 30,
                "tid": "name",
                "id": "name-input",
                "name": "full-name",
                "role": "textbox",
                "cls": "field",
                "val": "Ariel Anders",
            },
            {
                "tag": "BUTTON",
                "text": "Submit",
                "x": 100,
                "y": 300,
                "w": 80,
                "h": 30,
                "tid": "submit",
                "id": "submit-btn",
                "name": "submit-btn",
                "role": "button",
                "cls": "btn",
            },
        ],
    }


def make_step(selector, check_type, expected, action="click", n=1, value=None):
    """Build the contract's step shape.

    Step shape: {"n", "action", "selector", "value",
    "check": {"type", "expected"}}.
    """
    step = {
        "n": n,
        "action": action,
        "selector": selector,
        "check": {"type": check_type, "expected": expected},
    }
    if value is not None:
        step["value"] = value
    return step


class CheckStepShapeTest(unittest.TestCase):
    def test_pass_returns_true_and_empty_reason(self):
        """Contract: check_step returns (passed: bool, reason: str), empty on pass."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "element_visible", "")
        passed, reason = check_step(page, step)
        self.assertIsInstance(passed, bool)
        self.assertIsInstance(reason, str)
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_fail_reason_is_nonempty_string(self):
        """Contract: reason empty on pass and naming the mismatch on fail."""
        page = make_form_page()
        step = make_step("[data-testid='submit']", "url_contains", "confirmation")
        passed, reason = check_step(page, step)
        self.assertIsInstance(passed, bool)
        self.assertIsInstance(reason, str)
        self.assertFalse(passed)
        self.assertTrue(reason)


class UrlContainsTest(unittest.TestCase):
    def test_url_contains_pass(self):
        """Contract: url_contains passes when expected is a substring of page url."""
        page = make_form_page()
        step = make_step("[data-testid='submit']", "url_contains", "example.com")
        passed, reason = check_step(page, step)
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_url_contains_fail(self):
        """Contract: url_contains fails when expected is not in the page url."""
        page = make_form_page()
        step = make_step("[data-testid='submit']", "url_contains", "confirmation")
        passed, reason = check_step(page, step)
        self.assertFalse(passed)
        self.assertTrue(reason)

    def test_url_contains_fail_reason_names_mismatch(self):
        """Contract: reason empty on pass and naming the mismatch on fail."""
        page = make_form_page()
        expected = "confirmation"
        step = make_step("[data-testid='submit']", "url_contains", expected)
        passed, reason = check_step(page, step)
        self.assertFalse(passed)
        self.assertTrue(reason)
        self.assertTrue(expected in reason or page["url"] in reason)


class ElementVisibleTest(unittest.TestCase):
    def test_element_visible_pass_by_testid(self):
        """Contract: element_visible passes when the selector matches a target."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "element_visible", "")
        passed, reason = check_step(page, step)
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_element_visible_pass_by_tag(self):
        """Contract: element_visible passes when the selector matches a target (tag)."""
        page = make_form_page()
        step = make_step("button", "element_visible", "")
        passed, reason = check_step(page, step)
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_element_visible_fail_for_no_match(self):
        """Contract: a selector matching nothing fails element_visible."""
        page = make_form_page()
        step = make_step("[data-testid='missing']", "element_visible", "")
        passed, _reason = check_step(page, step)
        self.assertFalse(passed)

    def test_element_visible_fail_reason_names_selector(self):
        """Contract: no-match fails element_visible with a reason naming the selector."""
        page = make_form_page()
        selector = "[data-testid='missing']"
        step = make_step(selector, "element_visible", "")
        passed, reason = check_step(page, step)
        self.assertFalse(passed)
        self.assertIn(selector, reason)


class TextContainsTest(unittest.TestCase):
    def test_text_contains_pass_via_title(self):
        """Contract: text_contains passes when expected is in the title."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "text_contains", "Contact")
        passed, reason = check_step(page, step)
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_text_contains_pass_via_element_text(self):
        """Contract: text_contains passes when expected is in any target text."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "text_contains", "Submit")
        passed, reason = check_step(page, step)
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_text_contains_fail(self):
        """Contract: text_contains fails when expected is in neither title nor text."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "text_contains", "no-such-phrase-xyz")
        passed, _reason = check_step(page, step)
        self.assertFalse(passed)

    def test_text_contains_fail_reason_nonempty(self):
        """Contract: reason empty on pass and naming the mismatch on fail."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "text_contains", "no-such-phrase-xyz")
        passed, reason = check_step(page, step)
        self.assertFalse(passed)
        self.assertTrue(reason)


class ValueEqualsTest(unittest.TestCase):
    def test_value_equals_pass_via_val(self):
        """Contract: value_equals passes when the matched target value equals expected (val)."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "value_equals", "Ariel Anders")
        passed, reason = check_step(page, step)
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_value_equals_pass_via_text_fallback(self):
        """Contract: value_equals uses element text when val is absent."""
        page = make_form_page()
        step = make_step("[data-testid='submit']", "value_equals", "Submit")
        passed, reason = check_step(page, step)
        self.assertTrue(passed)
        self.assertEqual(reason, "")

    def test_value_equals_fail_on_mismatch(self):
        """Contract: value_equals fails when the value differs from expected."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "value_equals", "Nobody Else")
        passed, _reason = check_step(page, step)
        self.assertFalse(passed)

    def test_value_equals_fail_reason_nonempty(self):
        """Contract: reason empty on pass and naming the mismatch on fail."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "value_equals", "Nobody Else")
        passed, reason = check_step(page, step)
        self.assertFalse(passed)
        self.assertTrue(reason)

    def test_value_equals_fail_for_no_match(self):
        """Contract: a selector matching nothing fails value_equals."""
        page = make_form_page()
        step = make_step("[data-testid='missing']", "value_equals", "x")
        passed, _reason = check_step(page, step)
        self.assertFalse(passed)

    def test_value_equals_fail_reason_names_selector(self):
        """Contract: no-match fails value_equals with a reason naming the selector."""
        page = make_form_page()
        selector = "[data-testid='missing']"
        step = make_step(selector, "value_equals", "x")
        passed, reason = check_step(page, step)
        self.assertFalse(passed)
        self.assertIn(selector, reason)


class UnknownCheckTest(unittest.TestCase):
    def test_unknown_check_type_fails(self):
        """Contract: unknown check types fail (only four check types exist)."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "color_equals", "blue")
        passed, _reason = check_step(page, step)
        self.assertFalse(passed)

    def test_unknown_check_type_reason_nonempty(self):
        """Contract: unknown check types fail with a reason naming the mismatch."""
        page = make_form_page()
        step = make_step("[data-testid='name']", "color_equals", "blue")
        passed, reason = check_step(page, step)
        self.assertFalse(passed)
        self.assertTrue(reason)


class MatchSelectorTest(unittest.TestCase):
    def test_match_selector_returns_list(self):
        """Contract: match_selector returns the list of matching target dicts."""
        page = make_form_page()
        result = match_selector(page, "[data-testid='name']")
        self.assertIsInstance(result, list)
        self.assertTrue(result)
        self.assertIsInstance(result[0], dict)

    def test_match_selector_empty_when_nothing_matches(self):
        """Contract: match_selector returns possibly-empty list; empty on no match."""
        page = make_form_page()
        self.assertEqual(match_selector(page, "[data-testid='missing']"), [])

    def test_match_selector_by_testid(self):
        """Contract: selector matches on data-testid."""
        page = make_form_page()
        matched = match_selector(page, "[data-testid='submit']")
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0].get("tid"), "submit")

    def test_match_selector_by_tag(self):
        """Contract: selector matches on tag."""
        page = make_form_page()
        matched = match_selector(page, "button")
        self.assertTrue(matched)
        tags = [el.get("tag", "").upper() for el in matched]
        self.assertIn("BUTTON", tags)

    def test_match_selector_by_id(self):
        """Contract: selector matches on id."""
        page = make_form_page()
        matched = match_selector(page, "#name-input")
        self.assertTrue(matched)
        self.assertEqual(matched[0].get("id"), "name-input")

    def test_match_selector_by_name(self):
        """Contract: selector matches on name."""
        page = make_form_page()
        matched = match_selector(page, "input[name='full-name']")
        self.assertTrue(matched)
        self.assertEqual(matched[0].get("name"), "full-name")

    def test_match_selector_by_role(self):
        """Contract: selector matches on role."""
        page = make_form_page()
        matched = match_selector(page, "button[role='button']")
        self.assertTrue(matched)
        self.assertEqual(matched[0].get("role"), "button")

    def test_match_selector_by_class(self):
        """Contract: selector matches on class."""
        page = make_form_page()
        matched = match_selector(page, ".field")
        self.assertTrue(matched)
        self.assertEqual(matched[0].get("cls"), "field")

    def test_match_selector_scoped_matches_subject_only(self):
        """Contract: for scoped selectors only the subject (last compound) must match."""
        page = make_form_page()
        bare = match_selector(page, "[data-testid='name']")
        scoped = match_selector(page, "form [data-testid='name']")
        self.assertTrue(bare)
        self.assertTrue(scoped)
        self.assertEqual(
            [el.get("tid") for el in scoped],
            [el.get("tid") for el in bare],
        )

    def test_match_selector_scoped_tag_subject(self):
        """Contract: for scoped selectors only the subject (last compound) must match."""
        page = make_form_page()
        matched = match_selector(page, "div > button")
        self.assertTrue(matched)
        tags = [el.get("tag", "").upper() for el in matched]
        self.assertIn("BUTTON", tags)


class NoModelKeyTest(unittest.TestCase):
    def test_all_four_checks_run_with_no_model_key(self):
        """Contract (Acceptance): the checker runs with no model key set."""
        keys = [
            "NEBIUS_API_KEY",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "COHERE_API_KEY",
            "MISTRAL_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "OPENROUTER_API_KEY",
            "TOGETHER_API_KEY",
            "GROQ_API_KEY",
            "DEEPSEEK_API_KEY",
            "XAI_API_KEY",
        ]
        saved = {key: os.environ.get(key) for key in keys}
        for key in keys:
            os.environ.pop(key, None)
        self.addCleanup(lambda: self._restore_env(saved))
        page = make_form_page()
        cases = [
            make_step("[data-testid='submit']", "url_contains", "example.com"),
            make_step("[data-testid='name']", "element_visible", ""),
            make_step("[data-testid='name']", "text_contains", "Contact"),
            make_step("[data-testid='name']", "value_equals", "Ariel Anders"),
        ]
        for step in cases:
            with self.subTest(check=step["check"]["type"]):
                passed, reason = check_step(page, step)
                self.assertTrue(passed, (step, reason))
                self.assertEqual(reason, "")

    @staticmethod
    def _restore_env(saved):
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    unittest.main()
