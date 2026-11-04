"""Pin the TLS-edge header-trust posture (#531).

oauth2-proxy IS the internet edge (nothing in front), so it must not
trust client X-Forwarded-* headers. This pins the config value and its
threat-model doc claim together: the #531 bug was config/doc drift.
"""
import os
import unittest

CFG = os.path.join(
    os.path.dirname(__file__), "..", "config", "oauth2-proxy.cfg")
DOC = os.path.join(
    os.path.dirname(__file__), "..", "docs", "remote-access.md")


def _active_lines(path):
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f
                if line.strip() and not line.strip().startswith("#")]


class TestEdgePosture(unittest.TestCase):
    def test_proxy_ignores_forwarded_headers(self):
        self.assertIn("reverse_proxy = false", _active_lines(CFG))

    def test_doc_matches_config(self):
        with open(DOC, encoding="utf-8") as f:
            doc = f.read()
        self.assertIn("reverse_proxy = false", doc)
