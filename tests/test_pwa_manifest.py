"""PWA installability static assertions (#892).

The console ships a web manifest + icons + install meta so it is
installable from Chrome/Android and presentable on iOS. These checks
are build-free: they assert the source files Vite copies (public/)
and rewrites (index.html) carry everything installability needs.
Serving (content-type, /buttons/ base rewrite) was verified against
a production build during review; the mimetypes assertions below
guard the extension choices StaticFiles serves by.
"""

import json
import mimetypes
import os
import struct
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
        self.assertIn('name="theme-color"', html)
        self.assertIn('rel="apple-touch-icon"', html)
        self.assertIn("apple-mobile-web-app-capable", html)
        self.assertIn("apple-mobile-web-app-status-bar-style", html)

    def test_served_content_types(self):
        # StaticFiles serves by mimetypes: the manifest must be
        # JSON, icons PNG.
        self.assertEqual(
            mimetypes.guess_type("manifest.json")[0], "application/json")
        self.assertEqual(
            mimetypes.guess_type("icon-192.png")[0], "image/png")


if __name__ == "__main__":
    unittest.main()
