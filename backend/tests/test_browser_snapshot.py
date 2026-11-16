"""Shared snapshot expression and error handling (#1297)."""

import json
import shutil
import subprocess
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.routers import browser_drive, browser_view
from backend.skill_drivers import BrowserDriverError, GridCDPDriver, PlaywrightDriver


PAGES = (
    {"url": "https://example.com/", "title": "Example", "els": [{"tag": "A"}]},
    {"url": "about:blank", "title": "", "els": []},
)
ERRORS = (
    {
        "exceptionDetails": {
            "text": "Uncaught",
            "exception": {"description": "SyntaxError: missing ) after argument list"},
        }
    },
    {"exceptionDetails": {"text": "evaluation threw"}},
    {"result": {"value": None}},
    {},
)


class SnapshotTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_real_javascript_redacts_credential_fields(self):
        harness = """
const assert = require('node:assert/strict');
const cases = [
  {type:'password',value:'password-probe'},
  ...['username','current-password','new-password','one-time-code','webauthn']
    .map(token=>({autocomplete:'section-login '+token.toUpperCase(),value:token+'-probe'})),
  {type:'password',value:'',label:'Credential label'},
  {autocomplete:'username',value:''},
  {type:'text',value:'ordinary value'},
  {autocomplete:'email',value:'ordinary email'},
];
const elements=cases.map(c=>({
  tagName:'INPUT',offsetParent:{},id:'',className:'',innerText:'',value:c.value,
  matches(selector){assert.equal(selector,'input[type="password"]');return c.type==='password'},
  getAttribute(name){return {autocomplete:c.autocomplete,'aria-label':c.label}[name]??null},
  getBoundingClientRect(){return {x:0,y:0,width:100,height:20}},
}));
global.document={URL:'https://example.com/',title:'Credential probe',querySelectorAll(){return elements}};
console.log(JSON.stringify(eval(EXPRESSION)));
""".replace("EXPRESSION", json.dumps(browser_drive.SNAPSHOT_JS))
        result = subprocess.run(
            ["node", "-e", harness], text=True, capture_output=True, timeout=10
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        elements = json.loads(result.stdout)["els"]
        self.assertEqual(len(elements), 10)
        for element in elements[:6]:
            self.assertEqual(element["text"], "[redacted]")
            self.assertEqual(element["val"], "[redacted]")
        self.assertEqual(elements[6]["val"], "")
        self.assertEqual(elements[6]["text"], "[redacted]")
        self.assertEqual(elements[7]["val"], "")
        self.assertEqual(elements[7]["text"], "")
        for element, expected in zip(
            elements[8:], ("ordinary value", "ordinary email")
        ):
            self.assertEqual(element["val"], expected)
            self.assertEqual(element["text"], expected)
        self.assertNotIn("password-probe", result.stdout)

    @unittest.skipUnless(shutil.which("node"), "requires Node.js")
    def test_shared_javascript_parses(self):
        result = subprocess.run(
            ["node", "--check"],
            input=browser_drive.SNAPSHOT_JS,
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def _api_snapshot(self, raw):
        bridge = mock.AsyncMock()
        bridge.call.return_value = raw
        app = FastAPI()
        app.include_router(browser_drive.router)
        with (
            mock.patch.object(browser_view, "_grid_live_session", return_value="agent"),
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
            mock.patch.object(browser_view, "_log_activity"),
            mock.patch.dict(browser_view._LAST_PAGE, {"page": None, "hash": None}),
            mock.patch.object(
                browser_view, "remember_page", wraps=browser_view.remember_page
            ) as remember,
        ):
            response = TestClient(app, raise_server_exceptions=False).get(
                "/api/browser/snapshot"
            )
        return response, remember

    def test_api_preserves_populated_and_empty_pages(self):
        for page in PAGES:
            with self.subTest(page=page):
                response, remember = self._api_snapshot({"result": {"value": page}})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["url"], page["url"])
                self.assertEqual(response.json()["title"], page["title"])
                self.assertEqual(response.json()["elements"], page["els"])
                remember.assert_called_once()

    def test_api_rejects_evaluation_errors_without_caching_empty_success(self):
        for raw in ERRORS:
            with self.subTest(raw=raw):
                response, remember = self._api_snapshot(raw)
                self.assertEqual(response.status_code, 502, response.text)
                self.assertIn("snapshot failed", response.json()["detail"])
                if "exceptionDetails" in raw:
                    self.assertIn(
                        "snapshot evaluation failed", response.json()["detail"]
                    )
                remember.assert_not_called()

    def _grid_snapshot(self, raw):
        driver = GridCDPDriver()
        bridge = mock.AsyncMock()
        bridge.call.return_value = raw
        with (
            mock.patch.object(driver, "_acquire", return_value=("agent", None)),
            mock.patch.object(browser_view, "CDPBridge", return_value=bridge),
        ):
            return driver.snapshot()

    def test_grid_preserves_populated_and_empty_pages(self):
        for page in PAGES:
            with self.subTest(page=page):
                actual = self._grid_snapshot({"result": {"value": page}})
                self.assertEqual(
                    actual,
                    {
                        "url": page["url"],
                        "title": page["title"],
                        "elements": page["els"],
                    },
                )

    def test_grid_rejects_evaluation_errors(self):
        for raw in ERRORS:
            with self.subTest(raw=raw), self.assertRaises(BrowserDriverError):
                self._grid_snapshot(raw)

    def _playwright_driver(self, page):
        driver = PlaywrightDriver()
        driver._page = page
        driver._snapshot_js = browser_drive.SNAPSHOT_JS
        return driver

    def test_playwright_preserves_populated_and_empty_pages(self):
        for value in PAGES:
            with self.subTest(value=value):
                page = mock.Mock()
                page.evaluate.return_value = value
                actual = self._playwright_driver(page).snapshot()
                self.assertEqual(
                    actual,
                    {
                        "url": value["url"],
                        "title": value["title"],
                        "elements": value["els"],
                    },
                )
                page.evaluate.assert_called_once_with(browser_drive.SNAPSHOT_JS)

    def test_playwright_rejects_evaluation_errors(self):
        page = mock.Mock()
        page.evaluate.side_effect = RuntimeError("SyntaxError: missing )")
        with self.assertRaisesRegex(BrowserDriverError, "SyntaxError"):
            self._playwright_driver(page).snapshot()

    def test_playwright_rejects_missing_result_fields(self):
        page = mock.Mock()
        page.evaluate.return_value = {}
        with self.assertRaises(BrowserDriverError):
            self._playwright_driver(page).snapshot()
