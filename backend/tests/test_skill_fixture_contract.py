"""#1261 acceptance: real Chromium, a local fixture, and no external requests.

Fixture contract: tests/fixtures/browser/form.html is self-contained. It has
name/email fields and a submit control with those data-testid values, and a
confirmation element displaying both values at a URL containing /confirmation.
Submission only changes the local fixture; it never sends a real message.
The fixture itself is implementation work, deliberately absent in this turn.
"""

import json
import os
import tempfile
import unittest
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend import skill_patch, skill_runs, skill_store
from backend.api.routers import browser_view
from backend.skill_drivers import PlaywrightDriver

FIXTURE = Path("tests/fixtures/browser/form.html")
FORM_URL = "http://skill-fixture.invalid/form"


class LocalFormFixtureContractTest(unittest.TestCase):
    def setUp(self):
        self.require_fixture()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.activity = root / "activity.jsonl"
        self.enterContext(
            mock.patch.dict(
                os.environ,
                {
                    "BROWSER_DATA_DIR": str(root),
                    "BROWSER_ACTIVITY_FILE": str(self.activity),
                    "BROWSER_NOTES_FILE": str(root / "notes.md"),
                    "BROWSER_SKILLS_DIR": str(root / "skills"),
                    "BROWSER_RUNS_FILE": str(root / "runs.jsonl"),
                },
            )
        )
        self.enterContext(
            mock.patch.object(browser_view, "_ACTIVITY", deque(maxlen=500))
        )
        self.enterContext(mock.patch.object(browser_view, "_LEARN_TOTAL", [0]))
        self.enterContext(
            mock.patch.object(
                browser_view, "_LEARN_STATE", {"covered": 0, "last_learn_at": 0.0}
            )
        )
        self.enterContext(
            mock.patch(
                "backend.api.routers.browser_drive.agent_control.read_control_state",
                return_value=SimpleNamespace(state="RUNNING"),
            )
        )
        try:
            from playwright.sync_api import Error
        except ImportError:
            self.skipTest("#1261 acceptance requires Playwright and local Chromium")
        self.driver = PlaywrightDriver(headless=True)
        self.addCleanup(self.driver.close)
        try:
            self.page = self.driver._ensure()
        except Error as exc:
            if "Executable doesn't exist" in str(exc):
                self.skipTest("#1261 acceptance requires installed Chromium")
            raise
        self.changed_layout = False
        self.blocked = []
        self.served = []
        self.page.context.route("**/*", self.route)

    def route(self, route):
        url = route.request.url
        if url != FORM_URL:
            self.blocked.append(url)
            route.abort()
            return
        self.served.append(url)
        source = FIXTURE.read_text(encoding="utf-8")
        if self.changed_layout:
            source += """<script>
                document.querySelector('[data-testid="submit"]').dataset.testid = 'submit-changed';
            </script>"""
        route.fulfill(status=200, content_type="text/html", body=source)

    def require_fixture(self):
        self.assertTrue(FIXTURE.is_file(), "#1261 needs the repo-local form fixture")

    def demonstrate(self, name="Ariel Fixture", email="ariel@example.invalid"):
        self.require_fixture()
        checks = []

        def act(action, selector, value, check):
            before = self.driver.snapshot()
            if action == "navigate":
                self.driver.navigate(selector)
            elif action == "click":
                box = self.page.locator(selector).bounding_box()
                self.assertIsNotNone(box)
                self.driver.click(
                    int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)
                )
            else:
                self.driver.type_text(value)
            browser_view._log_activity(
                action,
                value,
                via="human",
                action=action,
                selector=selector,
                url=selector if action == "navigate" else before["url"],
                page_hash=browser_view.page_hash_of(before),
                target="playwright",
            )
            checks.append(check)

        act("navigate", FORM_URL, "", {"type": "url_contains", "expected": "/form"})
        for tid, value in (("name", name), ("email", email)):
            selector = f"[data-testid='{tid}']"
            act("click", selector, "", {"type": "element_visible", "expected": ""})
            act("type", selector, value, {"type": "value_equals", "expected": value})
        act(
            "click",
            "[data-testid='submit']",
            "",
            {"type": "url_contains", "expected": "/confirmation"},
        )
        self.assert_confirmation(name, email)
        events = [json.loads(line) for line in self.activity.read_text().splitlines()]
        human = [event for event in events if event["via"] == "human"]
        self.assertEqual(
            [event["action"] for event in human],
            ["navigate", "click", "type", "click", "type", "click"],
        )
        learned = browser_view.run_learn_pass(manual=True)
        self.assertTrue(learned["wrote"])
        self.assertEqual(learned["events_covered"], 6)
        self.assertIn("human", browser_view.read_notes()["notes"])
        return {
            "skill": "local-fixture-form",
            "version": 1,
            "source_url": FORM_URL,
            "steps": [
                {
                    "n": n,
                    "action": event["action"],
                    "selector": event["selector"],
                    "value": event["detail"],
                    "check": check,
                }
                for n, (event, check) in enumerate(zip(human, checks), 1)
            ],
        }

    def assert_confirmation(self, name, email):
        self.assertIn("/confirmation", self.page.url)
        confirmation = self.page.locator("[data-testid='confirmation']")
        self.assertTrue(confirmation.is_visible())
        text = confirmation.inner_text()
        self.assertIn(name, text)
        self.assertIn(email, text)

    def test_every_form_field_has_a_test_id(self):
        """#1261: every real fixture field has a stable recorder/replayer hook."""
        self.require_fixture()
        self.driver.navigate(FORM_URL)
        controls = self.page.locator("input, textarea, select, button").evaluate_all(
            "els => els.map(e => ({tid: e.dataset.testid, type: e.type}))"
        )
        self.assertGreaterEqual(len(controls), 3)
        ids = [control.get("tid") for control in controls]
        self.assertTrue(all(ids), controls)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue({"name", "email", "submit"}.issubset(ids))
        self.assertNotIn("password", [control["type"] for control in controls])

    def test_record_replay_break_correct_without_external_requests(self):
        """#1261: real recording/learning, pass, layout failure, correction, pass."""
        skill = self.demonstrate()
        self.assertNotIn("error", skill_store.save_skill(skill))
        stored = skill_store.read_skill(skill["skill"])
        first = skill_runs.run_and_record(stored, self.driver)
        self.assertTrue(first["passed"], first)
        self.assert_confirmation("Ariel Fixture", "ariel@example.invalid")
        self.changed_layout = True
        broken = skill_runs.run_and_record(stored, self.driver)
        self.assertFalse(broken["passed"])
        self.assertEqual(broken["failed_step"], 6)
        self.assertNotIn("/confirmation", self.page.url)
        patched = skill_patch.apply_patch(
            stored,
            {
                "step": 6,
                "selector": "[data-testid='submit-changed']",
                "by": "fixture-human",
                "why": "local layout changed",
            },
        )
        self.assertNotIn("error", patched)
        self.assertNotIn("error", skill_store.save_skill(patched["skill"]))
        corrected = skill_store.read_skill(skill["skill"])
        third = skill_runs.run_and_record(corrected, self.driver)
        self.assertTrue(third["passed"], third)
        self.assert_confirmation("Ariel Fixture", "ariel@example.invalid")
        rows = skill_runs.list_runs(skill["skill"])
        self.assertEqual([row["passed"] for row in rows], [True, False, True])
        self.assertEqual([row["failed_step"] for row in rows], [None, 6, None])
        self.assertEqual([row["version"] for row in rows], [1, 1, 2])
        original = skill_store.skills_dir() / f"{skill['skill']}.v1.json"
        self.assertEqual(json.loads(original.read_text()), skill)
        self.assertTrue(self.served)
        self.assertEqual(
            self.blocked, [], "self-contained fixture fetched another resource"
        )
        refused = self.page.evaluate(
            "fetch('https://external.invalid/forbidden').then(() => false, () => true)"
        )
        self.assertTrue(refused)
        self.assertEqual(self.blocked, ["https://external.invalid/forbidden"])

    def test_confirmation_is_derived_from_the_entered_values(self):
        """#1261: confirmation is not a canned success page with hardcoded values."""
        self.demonstrate("Second Fixture Person", "second@example.invalid")
        self.assertNotIn(
            "Ariel Fixture",
            self.page.locator("[data-testid='confirmation']").inner_text(),
        )
