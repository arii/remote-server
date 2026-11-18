"""Legacy BrowserMCP coordinates must never mutate the browser."""

from unittest import mock

from backend.skill_drivers import BrowserDriverError, BrowserMCPDriver
from backend.skill_replay import run_skill
from backend.tests.agent_control_case import AgentControlTestCase


class BrowserMCPTargetTest(AgentControlTestCase):
    def test_click_rejects_coordinates_without_contacting_relay(self):
        driver = BrowserMCPDriver()
        with mock.patch.object(driver, "_call") as relay:
            for x, y in ((0, 0), (120, 240)):
                with self.subTest(x=x, y=y):
                    with self.assertRaisesRegex(
                        BrowserDriverError, r"click.*BrowserMCP v2 contract mismatch"
                    ):
                        driver.click(x, y)
                    relay.assert_not_called()

    def test_wheel_rejects_deltas_without_hovering_or_scrolling(self):
        driver = BrowserMCPDriver()
        with mock.patch.object(driver, "_call") as relay:
            for dx, dy in ((0, 120), (-20, -240), (0, 0)):
                with self.subTest(dx=dx, dy=dy):
                    with self.assertRaisesRegex(
                        BrowserDriverError, r"wheel.*BrowserMCP v2 contract mismatch"
                    ):
                        driver.wheel(0, 0, dx, dy)
                    relay.assert_not_called()

    def test_replay_stops_at_unsupported_target_without_mutating_relay(self):
        for action in ("click", "wheel"):
            with self.subTest(action=action):
                driver = BrowserMCPDriver()
                snapshot = {
                    "url": "https://fixture.invalid/",
                    "snapshot": {"role": "button", "name": "Pane", "ref": "e12"},
                }
                step = {
                    "n": 1,
                    "action": action,
                    "selector": "button[role='button']",
                    "dx": 0,
                    "dy": 120,
                    "check": {"type": "element_visible", "expected": ""},
                }
                skill = {
                    "skill": "mcp-target",
                    "version": 1,
                    "source_url": snapshot["url"],
                    "steps": [step, dict(step, n=2)],
                }
                with mock.patch.object(driver, "_call", return_value=snapshot) as relay:
                    result = run_skill(skill, driver)
                self.assertFalse(result["passed"])
                self.assertEqual(result["failed_step"], 1)
                self.assertEqual(len(result["steps"]), 1)
                self.assertEqual(result["steps"][0]["outcome"]["verdict"], "error")
                self.assertIn("BrowserMCP v2 contract mismatch", result["reason"])
                relay.assert_called_once_with("browser_snapshot", {})
