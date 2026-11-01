import json
import os
import subprocess
import unittest

class TestDockerComposeConfig(unittest.TestCase):
    def setUp(self):
        self.root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.compose_path = os.path.join(self.root_dir, "docker-compose.yml")
        self.env_example_path = os.path.join(self.root_dir, ".env.example")

    def test_jules_api_key_declared_in_compose_file(self):
        # Inspect raw compose file content to ensure explicit declaration with fallback syntax
        with open(self.compose_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("terminal:", content)
        self.assertIn("buttons:", content)
        self.assertIn("JULES_API_KEY: ${JULES_API_KEY:-}", content)

    def test_docker_compose_config_validation(self):
        # Validate that `docker compose --env-file .env.example config --quiet` succeeds
        proc = subprocess.run(
            ["docker", "compose", "--env-file", self.env_example_path, "config", "--quiet"],
            cwd=self.root_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        self.assertEqual(
            proc.returncode, 0,
            f"docker compose config failed: {proc.stderr}"
        )

    def test_jules_api_key_forwarding_in_config(self):
        # Test forwarding behavior when JULES_API_KEY is present in env
        test_key = "test_jules_key_secret_12345"
        env = os.environ.copy()
        env["JULES_API_KEY"] = test_key

        proc = subprocess.run(
            ["docker", "compose", "--env-file", self.env_example_path, "config", "--format", "json"],
            cwd=self.root_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        self.assertEqual(proc.returncode, 0, f"docker compose config failed: {proc.stderr}")
        resolved_config = json.loads(proc.stdout)
        terminal_env = resolved_config["services"]["terminal"]["environment"]
        buttons_env = resolved_config["services"]["buttons"]["environment"]

        self.assertEqual(terminal_env.get("JULES_API_KEY"), test_key)
        self.assertEqual(buttons_env.get("JULES_API_KEY"), test_key)


if __name__ == "__main__":
    unittest.main()
