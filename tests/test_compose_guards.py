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

    def test_pinned_service_images(self):
        """ollama/tailscale/code-server images must be digest-pinned.

        These three floated on :latest/:stable with unhonored "pin at
        deploy" comments; any recreate could silently jump versions
        (issue #478). Each ref must carry an @sha256: digest so a
        recreate pulls identical bytes.
        """
        with open(os.path.abspath(COMPOSE), encoding="utf-8") as f:
            lines = f.read().splitlines()
        images = {}
        service = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            top = re.match(r"^(\S[^:]*):\s*(#.*)?$", line)
            if top and not line.startswith((" ", "\t")):
                service = None
                continue
            svc = re.match(r"^  ([A-Za-z0-9_.-]+):\s*(#.*)?$", line)
            if svc:
                service = svc.group(1)
                continue
            m = re.match(r"^\s+image:\s*(\S+)", line)
            if m and service:
                images.setdefault(service, m.group(1))
        for svc in ("ollama", "tailscale", "code-server"):
            self.assertIn(svc, images, f"no image ref found for {svc}")
            self.assertRegex(
                images[svc],
                r"^[^@\s]+@sha256:[0-9a-f]{64}$",
                f"{svc} image must be pinned by digest (got {images[svc]})",
            )


if __name__ == "__main__":
    unittest.main()
