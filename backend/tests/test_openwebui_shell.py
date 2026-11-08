"""Tests for the Open WebUI subpath shell shim (no live OUI needed)."""

import unittest
from unittest import mock

from fastapi.testclient import TestClient

from backend.api.routers import openwebui_shell

FIXTURE_SHELL = """<!doctype html>
<html><head>
<link href="/_app/immutable/assets/0.css">
<link href='/static/favicon.png'>
<link rel="manifest" href="/manifest.json">
<script>sveltekit_abc = {base: ""};</script>
<script src="/_app/immutable/entry/app.js"></script>
</head><body>app</body></html>"""


def _urlopen_ok(*args, **kwargs):
    resp = mock.MagicMock()
    resp.read.return_value = FIXTURE_SHELL.encode("utf-8")
    context = mock.MagicMock()
    context.__enter__.return_value = resp
    return context


class RewriteShellTest(unittest.TestCase):
    def test_rewrite_pins_base_and_assets(self):
        out = openwebui_shell.rewrite_oui_shell(FIXTURE_SHELL)
        self.assertIn('base: "/webui"', out)
        self.assertNotIn('base: ""', out)
        self.assertIn('"/webui/_app/immutable/entry/app.js"', out)
        self.assertIn("'/webui/static/favicon.png'", out)
        self.assertIn('"/webui/manifest.json"', out)
        self.assertIn("<body>app</body>", out)

    def test_rewrite_fails_closed_on_drift(self):
        with self.assertRaises(ValueError):
            openwebui_shell.rewrite_oui_shell("<html><body>no bootstrap</body></html>")


class OuiShellRouteTest(unittest.TestCase):
    def setUp(self):
        from backend.server import app

        self.client = TestClient(app)

    def test_shell_serves_rewritten(self):
        with mock.patch("urllib.request.urlopen", side_effect=_urlopen_ok):
            resp = self.client.get("/_oui-shell")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn('base: "/webui"', resp.text)

    def test_shell_502_when_upstream_down(self):
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionError("down")):
            resp = self.client.get("/_oui-shell")
        self.assertEqual(resp.status_code, 502)

    def test_shell_502_on_format_drift(self):
        def drift(*args, **kwargs):
            resp = mock.MagicMock()
            resp.read.return_value = b"<html>changed</html>"
            context = mock.MagicMock()
            context.__enter__.return_value = resp
            return context

        with mock.patch("urllib.request.urlopen", side_effect=drift):
            resp = self.client.get("/_oui-shell")
        self.assertEqual(resp.status_code, 502)
