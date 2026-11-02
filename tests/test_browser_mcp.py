import unittest
import os
import sys
import json
import subprocess
import tempfile
import shutil

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


class TestBrowserMcp(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.env = os.environ.copy()
        self.env["TARGET_HOME"] = self.tmpdir
        self.env["SETTINGS_FILE"] = os.path.join(self.tmpdir, ".gemini/antigravity-cli/settings.json")
        self.env["CONFIG_FILE"] = os.path.join(self.tmpdir, ".gemini/config/config.json")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_setup_browser_mcp_script(self):
        setup_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts/setup-browser-mcp.sh"))
        res = subprocess.run(["bash", setup_script], env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"setup-browser-mcp.sh failed: {res.stderr}")

        # Check directory layout
        launcher = os.path.join(self.tmpdir, ".gemini/antigravity-cli/bin/start_browsermcp.sh")
        self.assertTrue(os.path.isfile(launcher))
        self.assertTrue(os.access(launcher, os.X_OK))

        browser_json = os.path.join(self.tmpdir, ".gemini/antigravity-cli/mcp/browser-mcp/browser.json")
        self.assertTrue(os.path.isfile(browser_json))
        with open(browser_json, "r", encoding="utf-8") as f:
            schema = json.load(f)
        self.assertEqual(schema["name"], "browser")
        self.assertIn("commands", schema["parameters"]["properties"])

        instructions_md = os.path.join(self.tmpdir, ".gemini/antigravity-cli/mcp/browser-mcp/instructions.md")
        self.assertTrue(os.path.isfile(instructions_md))

        # Check symlink alias
        alias = os.path.join(self.tmpdir, ".gemini/antigravity-cli/mcp/browsermcp")
        self.assertTrue(os.path.islink(alias))
        self.assertEqual(os.path.realpath(alias), os.path.realpath(os.path.dirname(browser_json)))

        # Check mcp_config.json
        cfg_path = os.path.join(self.tmpdir, ".gemini/config/mcp_config.json")
        self.assertTrue(os.path.isfile(cfg_path))
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("browser-mcp", cfg["mcpServers"])
        self.assertIn("browsermcp", cfg["mcpServers"])

    def test_update_permissions_script(self):
        update_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts/update-permissions.sh"))
        res = subprocess.run(["bash", update_script], env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"update-permissions.sh failed: {res.stderr}")

        settings_path = self.env["SETTINGS_FILE"]
        self.assertTrue(os.path.isfile(settings_path))
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        allows = settings.get("permissions", {}).get("allow", [])

        self.assertIn("mcp(boomtick/*)", allows)
        self.assertIn("mcp(browser-mcp/*)", allows)
        self.assertIn("mcp(browsermcp/*)", allows)
        self.assertIn("mcp(browser/*)", allows)
        self.assertIn("command(git)", allows)
        self.assertIn("command(gh)", allows)
        self.assertIn("command(docker)", allows)
        self.assertIn("command(pnpm)", allows)

    def test_allow_boomtick_mcp_deprecation_wrapper(self):
        dep_script = os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts/allow-boomtick-mcp.sh"))
        res = subprocess.run(["bash", dep_script], env=self.env, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"allow-boomtick-mcp.sh failed: {res.stderr}")
        self.assertIn("deprecated", res.stderr)


if __name__ == "__main__":
    unittest.main()
