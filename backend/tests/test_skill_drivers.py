"""Acceptance tests for the decoupled browser driver (issue arii/remote-server#1259).

Contract source: the issue text only. These tests pin the public
interface ``backend.skill_drivers`` behavior, not implementation
internals. Authored without reading ``backend/skill_drivers.py`` or
``backend/skill_replay.py``; ``backend.skill_replay.run_skill`` is
merged prior art consulted only as ``run_skill(skill, driver)->dict``.
"""

import copy
import inspect
import os
import unittest

from backend.skill_drivers import (
    BrowserDriver,
    BrowserDriverError,
    BrowserMCPDriver,
    GridCDPDriver,
    PlaywrightDriver,
    check_driver,
    get_driver,
)
from backend.skill_format import validate_skill
from backend.skill_replay import run_skill

FORM_URL = "https://example.com/form"
CONF_URL = "https://example.com/confirmation"

INTERFACE_METHODS = (
    "snapshot",
    "navigate",
    "click",
    "type_text",
    "key",
    "wheel",
)


def make_form_elements():
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
            "tid": "submit",
            "id": "submit-btn",
            "name": "submit-btn",
            "role": "button",
            "cls": "btn",
        },
    ]


def make_good_pages():
    return {
        FORM_URL: {
            "url": FORM_URL,
            "title": "Contact form",
            "elements": make_form_elements(),
        },
        CONF_URL: {
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
        },
    }


def make_three_step_skill():
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


def _inside(x, y, el):
    try:
        fx, fy = float(x), float(y)
    except (TypeError, ValueError):
        return False
    ex, ey = float(el.get("x", 0)), float(el.get("y", 0))
    ew, eh = float(el.get("w", 0)), float(el.get("h", 0))
    return ex <= fx <= ex + ew and ey <= fy <= ey + eh


class ConformingFakeDriver:
    """Interface-conformant scripted driver for the form skill."""

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

    def click(self, x, y):
        self.actions.append(f"click:{x},{y}")
        page = self._pages[self._current]
        for el in page.get("elements", []) or []:
            if not isinstance(el, dict):
                continue
            if el.get("tid") == "submit" or el.get("id") == "submit-btn":
                if _inside(x, y, el):
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


class SecondConformingFakeDriver:
    """A second, distinctly-implemented conformant driver.

    Same observable replay behavior as ConformingFakeDriver, different
    class: swapping between them must not change the replay outcome.
    """

    def __init__(self, pages, start_url):
        self._store = copy.deepcopy(pages)
        if start_url not in self._store:
            self._store[start_url] = {"url": start_url, "title": "", "elements": []}
        self._here = start_url
        self.calls = []

    def snapshot(self):
        self.calls.append("snapshot")
        return copy.deepcopy(self._store[self._here])

    def navigate(self, url):
        self.calls.append(f"navigate:{url}")
        if url not in self._store:
            self._store[url] = {"url": url, "title": "", "elements": []}
        self._here = url

    def click(self, x, y):
        self.calls.append(f"click:{x},{y}")
        page = self._store[self._here]
        for el in page.get("elements", []) or []:
            if not isinstance(el, dict):
                continue
            if el.get("tid") == "submit" or el.get("id") == "submit-btn":
                if _inside(x, y, el):
                    self._here = CONF_URL
                    return

    def type_text(self, text):
        self.calls.append(f"type_text:{text}")
        page = self._store[self._here]
        for el in page.get("elements", []) or []:
            if isinstance(el, dict) and el.get("tid") == "name":
                el["val"] = text
                return

    def key(self, key):
        self.calls.append(f"key:{key}")

    def wheel(self, x, y, dx, dy):
        self.calls.append(f"wheel:{x},{y},{dx},{dy}")


class OnlySnapshotDriver:
    """Partial object: implements snapshot only."""

    def snapshot(self):
        return {"url": "", "title": "", "elements": []}


class PublicSurfaceTest(unittest.TestCase):
    def test_public_names_are_importable_and_shaped(self):
        """Contract: public surfaces are BrowserDriver, check_driver,
        get_driver, GridCDPDriver, PlaywrightDriver, BrowserMCPDriver,
        BrowserDriverError (interfaces only)."""
        self.assertTrue(callable(check_driver))
        self.assertTrue(callable(get_driver))
        for cls in (GridCDPDriver, PlaywrightDriver, BrowserMCPDriver):
            self.assertTrue(isinstance(cls, type))
        self.assertTrue(isinstance(BrowserDriverError, type))
        self.assertTrue(issubclass(BrowserDriverError, Exception))


class InterfaceProtocolTest(unittest.TestCase):
    def test_interface_defines_all_six_protocol_methods(self):
        """Contract: driver protocol is snapshot/navigate/click/
        type_text/key/wheel."""
        for name in INTERFACE_METHODS:
            self.assertTrue(
                callable(getattr(BrowserDriver, name, None)),
                f"BrowserDriver must define {name}",
            )

    def test_interface_methods_are_sync(self):
        """Contract: driver protocol is all sync."""
        for name in INTERFACE_METHODS:
            fn = getattr(BrowserDriver, name, None)
            self.assertFalse(
                inspect.iscoroutinefunction(fn),
                f"BrowserDriver.{name} must be sync",
            )

    def test_full_fake_passes_conformance(self):
        """Contract: check_driver(driver) is [] when conforming."""
        fake = ConformingFakeDriver(make_good_pages(), FORM_URL)
        self.assertEqual(check_driver(fake), [])

    def test_partial_object_lists_exactly_missing_methods(self):
        """Contract: check_driver lists missing-method names; a partial
        object lists exactly the missing methods."""
        missing = check_driver(OnlySnapshotDriver())
        self.assertTrue(missing, "partial driver must not conform")
        mentioned = [
            name
            for name in INTERFACE_METHODS
            if any(name in entry for entry in missing)
        ]
        self.assertCountEqual(
            mentioned, ["navigate", "click", "type_text", "key", "wheel"]
        )

    def test_empty_object_lists_all_protocol_methods(self):
        """Contract: an object implementing nothing lists every
        protocol method as missing."""

        class Empty:
            pass

        missing = check_driver(Empty())
        self.assertTrue(missing, "empty object must not conform")
        mentioned = [
            name
            for name in INTERFACE_METHODS
            if any(name in entry for entry in missing)
        ]
        self.assertCountEqual(mentioned, list(INTERFACE_METHODS))


class FactoryMappingTest(unittest.TestCase):
    def _set_env(self, value):
        old = os.environ.get("BROWSER_DRIVER")
        if value is None:
            os.environ.pop("BROWSER_DRIVER", None)
        else:
            os.environ["BROWSER_DRIVER"] = value

        def restore():
            if old is None:
                os.environ.pop("BROWSER_DRIVER", None)
            else:
                os.environ["BROWSER_DRIVER"] = old

        self.addCleanup(restore)

    def test_get_driver_grid_maps_to_grid_cdp_driver(self):
        """Contract: get_driver builds grid|playwright|browser-mcp;
        explicit 'grid' gives the Grid CDP driver."""
        self._set_env(None)
        driver = get_driver("grid")
        self.assertIsInstance(driver, GridCDPDriver)

    def test_get_driver_playwright_maps_to_playwright_driver(self):
        """Contract: explicit 'playwright' gives the local Playwright
        driver."""
        self._set_env(None)
        driver = get_driver("playwright")
        self.assertIsInstance(driver, PlaywrightDriver)

    def test_get_driver_browser_mcp_maps_to_browser_mcp_driver(self):
        """Contract: explicit 'browser-mcp' gives the Browser MCP
        driver."""
        self._set_env(None)
        driver = get_driver("browser-mcp")
        self.assertIsInstance(driver, BrowserMCPDriver)

    def test_get_driver_defaults_to_grid_when_env_unset(self):
        """Contract: get_driver(name=None) defaults to grid when
        BROWSER_DRIVER is unset."""
        self._set_env(None)
        driver = get_driver(None)
        self.assertIsInstance(driver, GridCDPDriver)

    def test_get_driver_reads_browser_driver_env(self):
        """Contract: get_driver(name=None) builds the BROWSER_DRIVER
        env value."""
        self._set_env("playwright")
        self.assertIsInstance(get_driver(None), PlaywrightDriver)
        self._set_env("browser-mcp")
        self.assertIsInstance(get_driver(None), BrowserMCPDriver)

    def test_get_driver_explicit_name_overrides_env(self):
        """Contract: an explicit name wins over the BROWSER_DRIVER env
        (one config line selects the driver)."""
        self._set_env("playwright")
        self.assertIsInstance(get_driver("grid"), GridCDPDriver)

    def test_get_driver_rejects_unknown_explicit_name(self):
        """Contract: get_driver rejects unknown names."""
        self._set_env(None)
        with self.assertRaises(BrowserDriverError):
            get_driver("no-such-driver")

    def test_get_driver_rejects_unknown_env_value(self):
        """Contract: get_driver rejects an unknown BROWSER_DRIVER env
        value."""
        self._set_env("no-such-driver")
        with self.assertRaises(BrowserDriverError):
            get_driver(None)


class RealDriverConstructionTest(unittest.TestCase):
    def test_each_driver_constructs_with_no_required_args(self):
        """Contract: each driver class constructs with no required args
        (env-configured)."""
        for cls in (GridCDPDriver, PlaywrightDriver, BrowserMCPDriver):
            try:
                cls()
            except TypeError as exc:
                self.fail(f"{cls.__name__}() must take no required args: {exc}")

    def test_each_constructed_real_driver_conforms(self):
        """Contract: each env-configured driver conforms to the
        interface (no live browser needed for the check)."""
        for cls in (GridCDPDriver, PlaywrightDriver, BrowserMCPDriver):
            with self.subTest(driver=cls.__name__):
                self.assertEqual(check_driver(cls()), [])

    def test_real_driver_methods_are_sync(self):
        """Contract: driver protocol is all sync, including the real
        driver implementations."""
        for cls in (GridCDPDriver, PlaywrightDriver, BrowserMCPDriver):
            with self.subTest(driver=cls.__name__):
                instance = cls()
                for name in INTERFACE_METHODS:
                    fn = getattr(instance, name, None)
                    self.assertTrue(callable(fn), f"{cls.__name__}.{name} missing")
                    self.assertFalse(
                        inspect.iscoroutinefunction(fn),
                        f"{cls.__name__}.{name} must be sync",
                    )


class ReplayerInterfaceTest(unittest.TestCase):
    def _set_env(self, value):
        old = os.environ.get("BROWSER_DRIVER")
        if value is None:
            os.environ.pop("BROWSER_DRIVER", None)
        else:
            os.environ["BROWSER_DRIVER"] = value

        def restore():
            if old is None:
                os.environ.pop("BROWSER_DRIVER", None)
            else:
                os.environ["BROWSER_DRIVER"] = old

        self.addCleanup(restore)

    def test_run_skill_operates_purely_through_interface_methods(self):
        """Contract: recorder and replayer call the interface, never the
        driver directly; every call flows through interface methods."""
        skill = make_three_step_skill()
        self.assertEqual(validate_skill(skill), [])
        driver = ConformingFakeDriver(make_good_pages(), FORM_URL)
        result = run_skill(skill, driver)
        self.assertIsInstance(result, dict)
        self.assertIs(result["passed"], True)
        kinds = [a.split(":")[0] for a in driver.actions]
        for expected in ("snapshot", "navigate", "type_text", "click"):
            self.assertIn(expected, kinds)
        for kind in kinds:
            self.assertIn(kind, INTERFACE_METHODS)

    def test_swapping_driver_via_single_config_line_keeps_replay_identical(self):
        """Contract: swapping the driver changes one config line, no
        recorder or replayer code; the same skill through run_skill
        with two interface-conformant drivers gives identical replay
        outcomes."""
        skill = make_three_step_skill()
        self.assertEqual(validate_skill(skill), [])

        def driver_from_config():
            name = os.environ.get("BROWSER_DRIVER", "grid")
            registry = {
                "grid": ConformingFakeDriver,
                "playwright": SecondConformingFakeDriver,
                "browser-mcp": SecondConformingFakeDriver,
            }
            return registry[name](make_good_pages(), FORM_URL)

        self._set_env("grid")
        first = run_skill(copy.deepcopy(skill), driver_from_config())
        self._set_env("browser-mcp")
        second = run_skill(copy.deepcopy(skill), driver_from_config())
        self.assertIs(first["passed"], True)
        self.assertIs(second["passed"], True)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
