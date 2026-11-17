import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class SetupBoomtickMcpTest(unittest.TestCase):
    def configure(self, token="", alias="", existing=False):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            paths = [
                home / ".gemini/config/mcp_config.json",
                home / ".gemini/antigravity-cli/mcp_config.json",
            ]
            if existing:
                for path in paths:
                    path.parent.mkdir(parents=True)
                    path.write_text(json.dumps({"mcpServers": {
                        "other": {"command": "keep-me"},
                        "boomtick-mcp": {"env": {"GITHUB_TOKEN": "old-dummy-token"}},
                    }}))
            result = subprocess.run(
                ["bash", "scripts/setup-boomtick-mcp.sh"],
                env={**os.environ, "TARGET_HOME": directory,
                     "GITHUB_TOKEN": token, "BOOMTICK_GITHUB_TOKEN": alias},
                capture_output=True, text=True, timeout=30,
                preexec_fn=lambda: os.umask(0o022),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            configs = []
            for path in paths:
                text = path.read_text()
                for secret in (token, alias, "old-dummy-token"):
                    if secret:
                        self.assertNotIn(secret, text)
                self.assertEqual(path.stat().st_mode & 0o777, 0o644)
                configs.append(json.loads(text))
            return configs

    def test_token_is_a_launch_time_reference(self):
        for config in self.configure(token="review-dummy-not-a-secret", existing=True):
            self.assertEqual(config["mcpServers"]["boomtick-mcp"]["env"]["GITHUB_TOKEN"],
                             "${GITHUB_TOKEN}")
            self.assertEqual(config["mcpServers"]["other"], {"command": "keep-me"})

    def test_alias_is_a_launch_time_reference(self):
        for config in self.configure(alias="alias-dummy-not-a-secret"):
            self.assertEqual(config["mcpServers"]["boomtick-mcp"]["env"]["GITHUB_TOKEN"],
                             "${BOOMTICK_GITHUB_TOKEN}")

    def test_primary_token_takes_precedence(self):
        for config in self.configure(token="primary-dummy", alias="alias-dummy"):
            self.assertEqual(config["mcpServers"]["boomtick-mcp"]["env"]["GITHUB_TOKEN"],
                             "${GITHUB_TOKEN}")

    def test_missing_token_removes_old_literal(self):
        for config in self.configure(existing=True):
            self.assertNotIn("GITHUB_TOKEN", config["mcpServers"]["boomtick-mcp"]["env"])
