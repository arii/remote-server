"""Acceptance tests for the skill replayer (issue #1255).

Contract source: the issue text only. These tests pin the public
interface ``backend.skill_replay.run_skill`` behavior, not
implementation internals. Authored without reading
``backend/skill_replay.py``; ``backend/skill_format.py`` and
``backend/skill_check.py`` are merged prior art used only for
fixture guards.
"""

import copy
import unittest

from backend.skill_format import validate_skill
from backend.skill_replay import run_skill

FORM_URL = "https://example.com/form"
CONF_URL = "https://example.com/confirmation"
SENTINEL_URL = "https://example.com/sentinel-never"


def make_form_elements(submit_tid="submit", submit_id="submit-btn"):
    """Canned form page elements (drive snapshot shape)."""
    return [
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
            "val": "",
        },
        {
            "tag": "BUTTON",
            "text": "Submit",
            "x": 100,
            "y": 300,
            "w": 80,
            "h": 30,
            "tid": submit_tid,
            "id": submit_id,
            "name": "submit-btn",
            "role": "button",
            "cls": "btn",
        },
    ]


def make_confirmation_page():
    return {
        "url": CONF_URL,
        "title": "Confirmation",
        "elements": [
            {
                "tag": "DIV",
                "text": "Thank you Ariel Anders",
                "x": 10,
                "y": 10,
                "w": 200,
                "h": 30,
                "tid": "thanks",
            },
        ],
    }


def make_good_pages():
    return {
        FORM_URL: {
            "url": FORM_URL,
            "title": "Contact form",
            "elements": make_form_elements(),
        },
        CONF_URL: make_confirmation_page(),
    }


def make_changed_layout_pages():
    """Same form page with the submit target renamed, so the recorded
    ``[data-testid='submit']`` selector no longer resolves."""
    return {
        FORM_URL: {
            "url": FORM_URL,
            "title": "Contact form",
            "elements": make_form_elements(
                submit_tid="submit-renamed", submit_id="submit-btn-v2"
            ),
        },
        CONF_URL: make_confirmation_page(),
    }


class FakeDriver:
    """Scripted driver speaking the contract's structural protocol.

    Scripted pages move form -> confirmation only when the submit
    button itself is clicked (coordinate-checked), so a focus click
    on the name field never advances the page. Every call, snapshot
    included, is recorded in ``actions``.
    """

    def __init__(self, pages, start_url):
        self._pages = copy.deepcopy(pages)
        if start_url not in self._pages:
            self._pages[start_url] = {"url": start_url, "title": "", "elements": []}
        self._current = start_url
        self.actions = []

    def snapshot(self):
        self.actions.append("snapshot")
        return copy.deepcopy(self._pages[self._current])

    def navigate(self, url):
        self.actions.append(f"navigate:{url}")
        if url not in self._pages:
            self._pages[url] = {"url": url, "title": "", "elements": []}
        self._current = url

    @staticmethod
    def _inside(x, y, el):
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            return False
        ex, ey = float(el.get("x", 0)), float(el.get("y", 0))
        ew, eh = float(el.get("w", 0)), float(el.get("h", 0))
        return ex <= fx <= ex + ew and ey <= fy <= ey + eh

    def click(self, x, y):
        self.actions.append(f"click:{x},{y}")
        page = self._pages[self._current]
        for el in page.get("elements", []) or []:
            if not isinstance(el, dict):
                continue
            if el.get("tid") == "submit" or el.get("id") == "submit-btn":
                if self._inside(x, y, el):
                    self._current = CONF_URL
                    return

    def type_text(self, text):
        self.actions.append(f"type_text:{text}")
        page = self._pages[self._current]
        for el in page.get("elements", []) or []:
            if isinstance(el, dict) and el.get("tid") == "name":
                el["val"] = text
                return

    def key(self, key):
        self.actions.append(f"key:{key}")

    def wheel(self, x, y, dx, dy):
        self.actions.append(f"wheel:{x},{y},{dx},{dy}")


def make_three_step_skill():
    """The contract's 3-step form skill: navigate, type name, click submit."""
    return {
        "skill": "replay-probe-form",
        "version": 1,
        "source_url": FORM_URL,
        "steps": [
            {
                "n": 1,
                "action": "navigate",
                "selector": FORM_URL,
                "check": {"type": "url_contains", "expected": "example.com"},
            },
            {
                "n": 2,
                "action": "type",
                "selector": "[data-testid='name']",
                "value": "Ariel Anders",
                "check": {"type": "value_equals", "expected": "Ariel Anders"},
            },
            {
                "n": 3,
                "action": "click",
                "selector": "[data-testid='submit']",
                "check": {"type": "url_contains", "expected": "confirmation"},
            },
        ],
    }


def make_changed_layout_skill():
    """Steps 1-3 mirror the 3-step form skill; step 4 is a sentinel
    navigate that must never execute once step 3 fails."""
    skill = make_three_step_skill()
    skill["steps"].append(
        {
            "n": 4,
            "action": "navigate",
            "selector": SENTINEL_URL,
            "check": {"type": "url_contains", "expected": "sentinel-never"},
        }
    )
    return skill


def make_value_mismatch_skill():
    """Step 2 types 'Ariel Anders' but checks for 'Nobody Else', so
    the post-action check fails; step 3 is a sentinel that must
    never execute."""
    return {
        "skill": "replay-probe-form",
        "version": 1,
        "source_url": FORM_URL,
        "steps": [
            {
                "n": 1,
                "action": "navigate",
                "selector": FORM_URL,
                "check": {"type": "url_contains", "expected": "example.com"},
            },
            {
                "n": 2,
                "action": "type",
                "selector": "[data-testid='name']",
                "value": "Ariel Anders",
                "check": {"type": "value_equals", "expected": "Nobody Else"},
            },
            {
                "n": 3,
                "action": "navigate",
                "selector": SENTINEL_URL,
                "check": {"type": "url_contains", "expected": "sentinel-never"},
            },
        ],
    }


def make_resolve_failure_skill():
    """Navigate then click a selector that matches nothing on the page."""
    return {
        "skill": "replay-probe-form",
        "version": 1,
        "source_url": FORM_URL,
        "steps": [
            {
                "n": 1,
                "action": "navigate",
                "selector": FORM_URL,
                "check": {"type": "url_contains", "expected": "example.com"},
            },
            {
                "n": 2,
                "action": "click",
                "selector": "[data-testid='no-such-target']",
                "check": {"type": "url_contains", "expected": "confirmation"},
            },
        ],
    }


def make_invalid_skill():
    """Same shape as the form skill but with an unknown action, which
    the frozen format rejects."""
    skill = make_three_step_skill()
    skill["steps"][1] = dict(skill["steps"][1])
    skill["steps"][1]["action"] = "hover"
    return skill


class PassEndToEndTest(unittest.TestCase):
    def test_three_step_form_skill_passes_with_failed_step_none(self):
        """Contract: 'A good skill passes' with failed_step None; result
        carries skill/version/passed/failed_step/reason/steps."""
        skill = make_three_step_skill()
        self.assertEqual(validate_skill(skill), [])
        driver = FakeDriver(make_good_pages(), FORM_URL)
        result = run_skill(skill, driver)
        self.assertIsInstance(result, dict)
        self.assertEqual(result["skill"], "replay-probe-form")
        self.assertEqual(result["version"], 1)
        self.assertIsInstance(result["passed"], bool)
        self.assertIs(result["passed"], True)
        self.assertIsNone(result["failed_step"])
        self.assertIsInstance(result["reason"], str)
        self.assertEqual(len(result["steps"]), 3)
        for entry, step in zip(result["steps"], skill["steps"]):
            self.assertEqual(entry["n"], step["n"])
            self.assertEqual(entry["action"], step["action"])
            self.assertIs(entry["ok"], True)
            self.assertIsInstance(entry["reason"], str)
        kinds = [a.split(":")[0] for a in driver.actions]
        self.assertIn("navigate", kinds)
        self.assertIn("type_text", kinds)
        self.assertIn("click", kinds)


class ChangedLayoutTest(unittest.TestCase):
    def test_changed_layout_fails_at_changed_step_and_skips_later_steps(self):
        """Contract: 'a skill with a changed layout fails at the changed
        step and stops there'; 'steps after a failure never execute'."""
        skill = make_changed_layout_skill()
        self.assertEqual(validate_skill(skill), [])
        driver = FakeDriver(make_changed_layout_pages(), FORM_URL)
        result = run_skill(skill, driver)
        self.assertIsInstance(result["passed"], bool)
        self.assertIs(result["passed"], False)
        self.assertEqual(result["failed_step"], 3)
        self.assertIsInstance(result["reason"], str)
        self.assertTrue(result["reason"])
        by_n = {entry["n"]: entry for entry in result["steps"]}
        self.assertIs(by_n[1]["ok"], True)
        self.assertIs(by_n[2]["ok"], True)
        self.assertIs(by_n[3]["ok"], False)
        self.assertTrue(by_n[3]["reason"])
        for entry in result["steps"]:
            if entry["n"] > 3:
                self.assertIs(entry["ok"], False)
        self.assertNotIn(
            f"navigate:{SENTINEL_URL}",
            driver.actions,
            "step 4 must never execute after step 3 fails",
        )


class CheckFailureStopsRunTest(unittest.TestCase):
    def test_failing_post_action_check_stops_the_run(self):
        """Contract: 'run the deterministic check after each step, stop
        on the first failed check, return the failing step number';
        checks run against the post-action snapshot."""
        skill = make_value_mismatch_skill()
        self.assertEqual(validate_skill(skill), [])
        driver = FakeDriver(make_good_pages(), FORM_URL)
        result = run_skill(skill, driver)
        self.assertIs(result["passed"], False)
        self.assertEqual(result["failed_step"], 2)
        self.assertTrue(result["reason"])
        by_n = {entry["n"]: entry for entry in result["steps"]}
        self.assertIs(by_n[1]["ok"], True)
        self.assertIs(by_n[2]["ok"], False)
        self.assertTrue(by_n[2]["reason"])
        self.assertIn("type_text:Ariel Anders", driver.actions)
        self.assertNotIn(
            f"navigate:{SENTINEL_URL}",
            driver.actions,
            "step 3 must never execute after step 2's check fails",
        )


class InvalidSkillTest(unittest.TestCase):
    def test_invalid_skill_refused_without_touching_driver(self):
        """Contract: 'run_skill validates first and refuses an invalid
        skill (passed False, failed_step None, reason naming the
        problem) without touching the driver'."""
        skill = make_invalid_skill()
        self.assertTrue(validate_skill(skill))
        driver = FakeDriver(make_good_pages(), FORM_URL)
        result = run_skill(skill, driver)
        self.assertIs(result["passed"], False)
        self.assertIsNone(result["failed_step"])
        self.assertIsInstance(result["reason"], str)
        self.assertTrue(result["reason"])
        self.assertEqual(result["skill"], skill["skill"])
        self.assertEqual(result["version"], skill["version"])
        self.assertIsInstance(result["steps"], list)
        self.assertEqual(driver.actions, [])


class ResolveFailureTest(unittest.TestCase):
    def test_unmatched_selector_fails_step_without_acting(self):
        """Contract: 'Selectors resolve against the pre-action snapshot;
        a selector matching nothing fails the step before acting'."""
        skill = make_resolve_failure_skill()
        self.assertEqual(validate_skill(skill), [])
        driver = FakeDriver(make_good_pages(), FORM_URL)
        result = run_skill(skill, driver)
        self.assertIs(result["passed"], False)
        self.assertEqual(result["failed_step"], 2)
        by_n = {entry["n"]: entry for entry in result["steps"]}
        self.assertIs(by_n[2]["ok"], False)
        self.assertTrue(by_n[2]["reason"])
        self.assertIn(f"navigate:{FORM_URL}", driver.actions)
        self.assertFalse(
            any(a.startswith("click:") for a in driver.actions),
            "the failing click step must not act",
        )


if __name__ == "__main__":
    unittest.main()
