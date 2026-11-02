import os
import unittest
import subprocess
import json


class TestCodeServer(unittest.TestCase):
    def test_compose_config_has_code_server_and_single_oauth_proxy(self):
        res = subprocess.run(
            ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        parsed = json.loads(res.stdout)
        services = parsed.get("services", {})
        self.assertIn("code-server", services)
        # Verify oauth2-proxy-code service and CODE_PORT are dropped
        self.assertNotIn("oauth2-proxy-code", services)
        self.assertIn("oauth2-proxy", services)

        # Check that caddy depends on code-server
        caddy = services["caddy"]
        self.assertIn("code-server", caddy.get("depends_on", {}))

        code_server = services["code-server"]
        self.assertEqual(code_server["image"], "codercom/code-server:latest")
        self.assertIn("--auth", code_server["command"])
        self.assertIn("none", code_server["command"])

        # Volumes
        volumes = parsed.get("volumes", {})
        self.assertIn("code-server-data", volumes)
        self.assertIn("code-server-config", volumes)

    def test_caddyfile_subpath_routing(self):
        with open("caddy/Caddyfile") as f:
            caddyfile = f.read()
        self.assertIn("handle /code*", caddyfile)
        self.assertIn("uri strip_prefix /code", caddyfile)
        self.assertIn("reverse_proxy code-server:8080", caddyfile)
        # Ensure /buttons* is ordered before /code* and both before terminal fallback
        idx_buttons = caddyfile.index("handle /buttons*")
        idx_code = caddyfile.index("handle /code*")
        idx_fallback = caddyfile.index("reverse_proxy terminal:7681")
        self.assertLess(idx_buttons, idx_code)
        self.assertLess(idx_code, idx_fallback)

    def test_continue_config_ollama_qwen3(self):
        config_yaml_path = "config/continue/config.yaml"
        self.assertTrue(os.path.exists(config_yaml_path))
        with open(config_yaml_path) as f:
            yaml_content = f.read()
        self.assertIn("qwen3:8b", yaml_content)
        self.assertIn("http://ollama:11434", yaml_content)

        config_json_path = "config/continue/config.json"
        self.assertTrue(os.path.exists(config_json_path))
        with open(config_json_path) as f:
            data_json = json.load(f)
        json_models = data_json.get("models", [])
        self.assertTrue(any(m.get("model") == "qwen3:8b" and "http://ollama:11434" in m.get("apiBase", "") for m in json_models))

    def test_buttons_server_launcher_and_actions(self):
        with open("buttons/server.py") as f:
            content = f.read()

        self.assertIn("launcher-bar", content)
        self.assertIn("codeLauncher", content)
        self.assertIn('href="/code"', content)
        self.assertIn('codeLauncher.href = window.location.origin + "/code"', content)
        self.assertIn("recreate-code", content)
        self.assertNotIn("<!--CODE_PORT-->", content)
        self.assertNotIn("CODE_PORT", content)

    def test_caddyfile_code_trailing_slash_redirect(self):
        with open("caddy/Caddyfile") as f:
            caddyfile = f.read()
        # Exact /code (no trailing slash) must redirect to /code/ BEFORE the
        # strip_prefix block: code-server answers / with a relative redirect
        # (./?folder=...), which a browser sitting on /code would resolve to
        # / (the terminal) instead of staying under /code/.
        idx_exact = caddyfile.index("handle /code {")
        idx_code = caddyfile.index("handle /code*")
        self.assertLess(idx_exact, idx_code)
        self.assertIn("redir * /code/", caddyfile[idx_exact:idx_code])

    def test_code_server_extensions_declared_and_boot_installed(self):
        with open("config/code-server/entrypoint.d/extensions.txt") as f:
            ids = [ln.split()[0] for ln in f
                   if ln.strip() and not ln.strip().startswith("#")]
        for expected in ("continue.continue",
                         "github.vscode-pull-request-github",
                         "github.vscode-github-actions",
                         "dbaeumer.vscode-eslint",
                         "esbenp.prettier-vscode",
                         "ms-python.python"):
            self.assertIn(expected, ids)

        hook = "config/code-server/entrypoint.d/install-extensions.sh"
        self.assertTrue(os.access(hook, os.X_OK))
        with open(hook) as f:
            hook_src = f.read()
        self.assertIn("extensions.txt", hook_src)
        self.assertIn("--install-extension", hook_src)

        res = subprocess.run(
            ["docker", "compose", "--env-file", ".env.example", "config", "--format", "json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True
        )
        code_server = json.loads(res.stdout)["services"]["code-server"]
        self.assertEqual(code_server["environment"]["ENTRYPOINTD"], "/seed/entrypoint.d")
        mounts = [str(v) for v in code_server.get("volumes", [])]
        self.assertTrue(any("entrypoint.d" in v and "/seed/entrypoint.d" in v
                            for v in mounts))


if __name__ == "__main__":
    unittest.main()
