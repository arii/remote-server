import json
import os
import subprocess
import unittest


def compose_config():
    res = subprocess.run(
        ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True
    )
    return json.loads(res.stdout)


class TestDurability(unittest.TestCase):
    def test_all_services_log_rotated(self):
        services = compose_config()["services"]
        for name in ("oauth2-proxy", "terminal", "ollama", "tailscale",
                      "buttons", "caddy", "browser-relay", "code-server"):
            with self.subTest(service=name):
                logging = services[name].get("logging", {})
                self.assertEqual(logging.get("driver"), "json-file")
                self.assertEqual(logging.get("options", {}).get("max-size"), "10m")
                self.assertEqual(logging.get("options", {}).get("max-file"), "3")

    def test_healthchecks(self):
        services = compose_config()["services"]
        for name in ("terminal", "code-server", "buttons", "caddy", "ollama"):
            with self.subTest(service=name):
                self.assertIn("healthcheck", services[name])
        for name in ("oauth2-proxy", "tailscale", "browser-relay"):
            with self.subTest(service=name):
                # Deliberately none: TLS edge (no plain probe), host-net VPN
                # (nothing gates on it), relay (agent-managed daemon).
                self.assertNotIn("healthcheck", services[name])

    def test_depends_on_conditions(self):
        deps = compose_config()["services"]["caddy"]["depends_on"]
        for name in ("terminal", "buttons", "code-server"):
            with self.subTest(service=name):
                self.assertEqual(deps[name]["condition"], "service_healthy")
        code_server = compose_config()["services"]["code-server"]["depends_on"]
        self.assertEqual(code_server["ollama"]["condition"], "service_healthy")

    def test_terminal_persists_shell_state(self):
        env = compose_config()["services"]["terminal"]["environment"]
        self.assertEqual(env.get("HISTFILE"), "/root/.local/share/bash_history")
        self.assertEqual(env.get("XDG_CACHE_HOME"), "/root/.local/cache")

    def test_vimrc_persists_viminfo(self):
        with open("terminal/vimrc.local", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("set viminfofile=/root/.local/share/vim/viminfo", content)

    def test_zellij_serializes_sessions(self):
        with open("terminal/config.kdl", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("session_serialization true", content)
        self.assertIn("serialize_pane_viewport true", content)

    def test_durability_doc_exists(self):
        self.assertTrue(os.path.isfile("docs/durability.md"))

    def test_npm_scripts_cover_services(self):
        with open("package.json", "r", encoding="utf-8") as f:
            scripts = json.load(f)["scripts"]
        for name in ("terminal", "buttons", "caddy", "browser-relay", "code-server"):
            with self.subTest(service=name):
                self.assertIn(f"{name}:logs", scripts)
        for name in ("terminal", "code-server", "browser-relay"):
            with self.subTest(service=name):
                self.assertIn(f"{name}:recreate", scripts)
        self.assertIn("caddy:reload", scripts)
        self.assertIn("terminal:build", scripts)


if __name__ == "__main__":
    unittest.main()
