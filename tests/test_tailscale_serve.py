#!/usr/bin/env python3
"""Tests for the opt-in Tailscale serve fallback (user directive 2026-11-08).

scripts/tailscale-entrypoint.sh serves the edge over the tailnet only when
TS_SERVE=1 (default off: VPN-only). Deliberately static (source scans plus
`sh -n`): executing the entrypoint would start tailscaled, which needs the
binary plus NET_ADMIN. Live serving is verified by hand on the box.
"""
import re
import subprocess
import unittest

ENTRYPOINT = "scripts/tailscale-entrypoint.sh"
COMPOSE = "docker-compose.yml"
ENV_EXAMPLE = ".env.example"
README = "README.md"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TestTailscaleServeContract(unittest.TestCase):
    def test_entrypoint_syntax(self):
        res = subprocess.run(
            ["sh", "-n", ENTRYPOINT], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"sh -n failed: {res.stderr}")

    def test_serve_gated_behind_flag(self):
        src = _read(ENTRYPOINT)
        self.assertIn("TS_SERVE", src)
        # The serve invocation exists exactly once and is indented
        # (inside the TS_SERVE conditional, never top-level).
        top_level = re.findall(r"^tailscale serve\b", src, re.M)
        self.assertEqual(top_level, [])
        indented = re.findall(r"^[ \t]+tailscale serve --https=443", src, re.M)
        self.assertEqual(len(indented), 1)

    def test_serve_defaults_off_with_proxy_target(self):
        src = _read(ENTRYPOINT)
        self.assertIn("${TS_SERVE:-}", src)
        self.assertIn(
            "TS_SERVE_TARGET:-http://127.0.0.1:${TERMINAL_PORT}", src)

    def test_compose_passes_serve_env(self):
        src = _read(COMPOSE)
        self.assertIn("TS_SERVE: ${TS_SERVE:-}", src)
        self.assertIn("TS_SERVE_TARGET: ${TS_SERVE_TARGET:-}", src)

    def test_docs_mention_opt_in(self):
        self.assertIn("TS_SERVE=1", _read(README))
        self.assertIn("TS_SERVE", _read(ENV_EXAMPLE))


if __name__ == "__main__":
    unittest.main()
