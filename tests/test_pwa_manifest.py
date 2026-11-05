"""PWA installability static assertions (#892).

The console ships a web manifest + icons + install meta so it is
installable from Chrome/Android and presentable on iOS. Most checks
are build-free: they assert the source files Vite copies (public/)
and rewrites (index.html) carry everything installability needs.
The served test below mounts a fixture dist through the same
mount_spa call the production image uses, proving the edge path
(/buttons/manifest.json, prefix stripped by caddy) answers 200 +
JSON instead of the #959 404. Serving (content-type, /buttons/ base
rewrite) was additionally verified against a production build
during review; the mimetypes assertions guard the extension choices
StaticFiles serves by.
"""

import json
import mimetypes
import os
import shutil
import struct
import tempfile
import unittest

FRONTEND = os.path.join(os.path.dirname(__file__), "..", "frontend")
PUBLIC = os.path.join(FRONTEND, "public")
MANIFEST = os.path.join(PUBLIC, "manifest.json")
INDEX = os.path.join(FRONTEND, "index.html")


def png_size(path):
    with open(path, "rb") as fh:
        data = fh.read(26)
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError(f"not a PNG: {path}")
    return struct.unpack(">II", data[16:24])


class PwaManifestTests(unittest.TestCase):
    def test_manifest_has_installability_fields(self):
        with open(MANIFEST, encoding="utf-8") as fh:
            manifest = json.load(fh)
        for field in ("name", "short_name", "start_url", "scope",
                      "display", "theme_color", "background_color",
                      "icons"):
            self.assertIn(field, manifest)
        self.assertEqual(manifest["display"], "standalone")
        sizes = {i["sizes"] for i in manifest["icons"]}
        self.assertIn("192x192", sizes)
        self.assertIn("512x512", sizes)
        self.assertTrue(
            any("maskable" in i.get("purpose", "")
                for i in manifest["icons"]),
            "a maskable icon is required")

    def test_manifest_urls_are_base_relative(self):
        # Vite copies public/ verbatim (no base rewrite inside the
        # file), so every URL must resolve against the manifest URL
        # under both the dev (/) and image (/buttons/) bases.
        with open(MANIFEST, encoding="utf-8") as fh:
            manifest = json.load(fh)
        for key in ("start_url", "scope"):
            self.assertFalse(manifest[key].startswith("/"),
                             f"{key} must be relative")
        for icon in manifest["icons"]:
            self.assertFalse(icon["src"].startswith("/"),
                             "icon src must be relative")

    def test_declared_icons_exist_at_declared_sizes(self):
        with open(MANIFEST, encoding="utf-8") as fh:
            manifest = json.load(fh)
        for icon in manifest["icons"]:
            path = os.path.join(PUBLIC, icon["src"])
            self.assertTrue(os.path.isfile(path), path)
            want = tuple(int(n) for n in icon["sizes"].split("x"))
            self.assertEqual(png_size(path), want, path)

    def test_apple_touch_icon_exists_opaque_png(self):
        path = os.path.join(PUBLIC, "icons", "apple-touch-icon.png")
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(png_size(path), (180, 180))

    def test_index_links_manifest_icons_and_meta(self):
        with open(INDEX, encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn('rel="manifest"', html)
        self.assertIn("/manifest.json", html)
        # Manifest fetches omit credentials by default; without
        # use-credentials the OAuth cookie is not sent and the fetch
        # 302s to GitHub, failing CORS (#943).
        self.assertIn('crossorigin="use-credentials"', html)
        self.assertIn('name="theme-color"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("mobile-web-app-capable", html)
        self.assertNotIn("apple-mobile-web-app-capable", html)
        self.assertIn("apple-mobile-web-app-status-bar-style", html)

    def test_oauth_proxy_skips_auth_for_manifest_and_icons(self):
        # The manifest is fetched without credentials, so oauth2-proxy
        # must let GET manifest/icons through without a 302 to GitHub
        # (#943). The app shell and /api stay gated.
        compose = os.path.join(
            os.path.dirname(__file__), "..", "docker-compose.yml")
        with open(compose, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn(
            "--skip-auth-route=GET=^/buttons/manifest", text)
        self.assertIn(
            "--skip-auth-route=GET=^/buttons/icons/", text)
        # The only other skip-auth route stays the exact POST webhook.
        self.assertIn(
            "--skip-auth-route=POST=^/buttons/api/webhook$", text)

    def test_served_content_types(self):
        # StaticFiles serves by mimetypes: the manifest must be
        # JSON, icons PNG.
        self.assertEqual(
            mimetypes.guess_type("manifest.json")[0], "application/json")
        self.assertEqual(
            mimetypes.guess_type("icon-192.png")[0], "image/png")

    def test_served_manifest_and_icons(self):
        # Edge regression (#959): the manifest fetched fine past
        # OAuth but 404d because nothing served it. Mount a fixture
        # dist (public/ tree, as Vite copies it) through the same
        # mount_spa the image uses and prove 200 + JSON, not 404.
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from backend.server import mount_spa

        dist = tempfile.mkdtemp(prefix="pwa-dist-")
        self.addCleanup(shutil.rmtree, dist, True)
        shutil.copytree(PUBLIC, dist, dirs_exist_ok=True)
        with open(os.path.join(dist, "index.html"), "w",
                  encoding="utf-8") as fh:
            fh.write("<html><body>console</body></html>")

        probed = FastAPI()
        mount_spa(probed, dist)
        client = TestClient(probed, raise_server_exceptions=False)

        manifest = client.get("/manifest.json")
        self.assertEqual(manifest.status_code, 200)
        self.assertIn("application/json",
                      manifest.headers["content-type"])
        body = manifest.json()
        self.assertEqual(body["short_name"], "Console")
        self.assertTrue(body["icons"])

        icon = client.get("/icons/icon-192.png")
        self.assertEqual(icon.status_code, 200)
        self.assertIn("image/png", icon.headers["content-type"])

        # The SPA fallback still answers the shell path ...
        shell = client.get("/")
        self.assertEqual(shell.status_code, 200)
        self.assertIn("text/html", shell.headers["content-type"])
        # ... while a truly missing asset stays an honest 404.
        missing = client.get("/no-such-asset.json")
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()
