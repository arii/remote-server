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

    def test_buttons_ollama_url(self):
        """buttons OLLAMA_URL must default to the compose-network alias.

        localhost inside the buttons container is itself, so every
        model call refused (issue #966). The default must be the
        ollama service address with an env override, never localhost.
        """
        with open(os.path.abspath(COMPOSE), encoding="utf-8") as f:
            lines = f.read().splitlines()
        gated = [line for line in lines if "OLLAMA_URL" in line]
        self.assertEqual(len(gated), 1,
                         "expected exactly one OLLAMA_URL line")
        self.assertIn("http://ollama:11434", gated[0])
        self.assertNotIn("localhost", gated[0])
        self.assertRegex(gated[0], r"\$\{OLLAMA_URL:-http://ollama:11434",
                         "OLLAMA_URL must keep the host override with "
                         "the network-alias default")

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

    def test_grid_session_timeout(self):
        """Grid idle reap must be minutes, not an hour (issue #1054).

        A leaked session wedged the single browser slot ~1hr
        (SE_NODE_SESSION_TIMEOUT=3600). Agent runs issue commands
        continuously and the viewer recreates transparently, so a
        10-minute ceiling only kills truly idle sessions.
        """
        with open(os.path.abspath(COMPOSE), encoding="utf-8") as f:
            lines = f.read().splitlines()
        timeout = [
            line for line in lines
            if "SE_NODE_SESSION_TIMEOUT" in line
            and not line.strip().startswith("#")
        ]
        self.assertEqual(len(timeout), 1,
                         "expected exactly one SE_NODE_SESSION_TIMEOUT line")
        m = re.search(r"SE_NODE_SESSION_TIMEOUT=(\d+)", timeout[0])
        self.assertIsNotNone(m, "session timeout must carry an integer value")
        self.assertLessEqual(int(m.group(1)), 600,
                             "idle reap must be 10 minutes or less")

    def test_agent_control_shared_state(self):
        """buttons + terminal must share one pause-state file (#1104).

        /tmp is per-container, so the default split-brains takeover
        pauses between the buttons API and the terminal pipeline.
        Both services pin AGENT_CONTROL_STATE_FILE to the shared
        buttons-data volume, and terminal mounts that volume.
        """
        with open(os.path.abspath(COMPOSE), encoding="utf-8") as f:
            lines = f.read().splitlines()
        service = None
        state_lines = {}
        mounts_buttons_data = set()
        for line in lines:
            if line and not line.startswith((" ", "\t", "#")):
                service = None
                continue
            svc = re.match(r"^  ([A-Za-z0-9_.-]+):\s*(#.*)?$", line)
            if svc:
                service = svc.group(1)
                continue
            if service in ("buttons", "terminal"):
                if re.match(r"^\s*AGENT_CONTROL_STATE_FILE\s*:",
                            line):
                    state_lines.setdefault(service, []).append(
                        line.strip())
                if ("buttons-data:/var/lib/buttons" in line
                        and not line.strip().startswith("#")):
                    mounts_buttons_data.add(service)
        for svc in ("buttons", "terminal"):
            self.assertEqual(
                state_lines.get(svc),
                ["AGENT_CONTROL_STATE_FILE: "
                 "/var/lib/buttons/agent_control_state.json"],
                f"{svc} must pin the shared pause-state file exactly once")
        self.assertIn(
            "buttons", mounts_buttons_data,
            "buttons must mount buttons-data at /var/lib/buttons")
        self.assertIn(
            "terminal", mounts_buttons_data,
            "terminal must mount the shared buttons-data volume")

    def test_chroma_dir_unified_on_td_output(self):
        """buttons must watch the sqlite `td rag index` produces (#1094).

        td persists to <repo>/.td_rag/chroma_db; the server and the
        entrypoint both follow S5_CHROMA_DIR, so the buttons service
        pins it at the in-container td output dir. Otherwise a
        zero-exit build lands sqlite where nobody watches and
        /health reports missing forever.
        """
        with open(os.path.abspath(COMPOSE), encoding="utf-8") as f:
            lines = f.read().splitlines()
        service = None
        chroma_lines = []
        for line in lines:
            if line and not line.startswith((" ", "\t", "#")):
                service = None
                continue
            svc = re.match(r"^  ([A-Za-z0-9_.-]+):\s*(#.*)?$", line)
            if svc:
                service = svc.group(1)
                continue
            if service == "buttons" and re.match(
                    r"^\s*S5_CHROMA_DIR\s*:", line):
                chroma_lines.append(line.strip())
        self.assertEqual(
            chroma_lines,
            ["S5_CHROMA_DIR: "
             "/home/ari/remote-server/.td_rag/chroma_db"],
            "buttons must pin S5_CHROMA_DIR at the td output dir")

    def test_buttons_playwright_browsers_path(self):
        """buttons must pin the baked browser home (#1118).

        The bake runs as root while the runtime sets HOME=/home/ari,
        so the default ~/.cache lookup misses the baked chromium.
        The compose pin must match the Dockerfile bake path.
        """
        with open(os.path.abspath(COMPOSE), encoding="utf-8") as f:
            lines = f.read().splitlines()
        service = None
        browser_lines = []
        for line in lines:
            if line and not line.startswith((" ", "\t", "#")):
                service = None
                continue
            svc = re.match(r"^  ([A-Za-z0-9_.-]+):\s*(#.*)?$", line)
            if svc:
                service = svc.group(1)
                continue
            if service == "buttons" and re.match(
                    r"^\s*PLAYWRIGHT_BROWSERS_PATH\s*:", line):
                browser_lines.append(line.strip())
        self.assertEqual(
            browser_lines,
            ["PLAYWRIGHT_BROWSERS_PATH: /opt/ms-playwright"],
            "buttons must pin PLAYWRIGHT_BROWSERS_PATH at the bake dir")

    def test_terminal_requirements_parity(self):
        """terminal/requirements.txt must include all backend deps and pytest."""
        backend_req = os.path.join(ROOT, "backend", "requirements.txt")
        terminal_req = os.path.join(ROOT, "terminal", "requirements.txt")
        self.assertTrue(os.path.exists(terminal_req))

        def parse_reqs(path):
            reqs = set()
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        reqs.add(line.split(";")[0].strip())
            return reqs

        backend_deps = parse_reqs(backend_req)
        terminal_deps = parse_reqs(terminal_req)
        for dep in backend_deps:
            self.assertIn(dep, terminal_deps,
                          f"backend dependency {dep} missing from terminal/requirements.txt")
        self.assertTrue(any(d.startswith("pytest") for d in terminal_deps),
                        "pytest missing from terminal/requirements.txt")


if __name__ == "__main__":
    unittest.main()
