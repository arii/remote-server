"""Compose guard tests: security-critical env interpolations must fail
closed. The oauth2-proxy single-user gate (issue #561) is enforced by a
`:?` guard on GITHUB_USER so compose refuses to start with an open gate.
Run: python3 -m unittest tests.test_compose_guards"""
import os
import re
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
COMPOSE = os.path.join(ROOT, "docker-compose.yml")


class TestComposeGuards(unittest.TestCase):
    def test_github_user_guard(self):
        """OAUTH2_PROXY_GITHUB_USER must carry a :? required-var guard.

        An empty --github-user disables oauth2-proxy's user restriction,
        and email_domains=["*"] then admits any GitHub user; the guard
        makes `docker compose config` fail fast instead (issue #561).
        """
        with open(os.path.abspath(COMPOSE), encoding="utf-8") as f:
            lines = f.read().splitlines()
        gated = [
            line for line in lines
            if "OAUTH2_PROXY_GITHUB_USER" in line
        ]
        self.assertEqual(len(gated), 1,
                         "expected exactly one OAUTH2_PROXY_GITHUB_USER line")
        self.assertRegex(
            gated[0],
            r"\$\{GITHUB_USER:\?",
            "GITHUB_USER interpolation must use the :? fail-closed guard",
        )


if __name__ == "__main__":
    unittest.main()
