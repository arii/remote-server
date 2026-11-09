"""Single Continue config (issue #1206): one yaml, no drift trap.

config/continue/ carried the same Ollama(qwen3:8b) intent in two
formats (config.json + config.yaml, both mounted ro into code-server);
the installed extension reads one format silently, so edits to the
other file did nothing. The legacy config.json is deleted: exactly
one config file remains, it carries the Ollama wiring, the compose
mount serves the whole directory (so the survivor is what the
editor reads), the schema holds no dangling reference to the
deleted file, and the repo validation gate still passes.

NOTE on "loads via validate_config": backend/validate_config.py is
JSON-only (stdlib json.load; no YAML parser in backend/requirements
or CI), and config/schema.json carries no continue entry — so the
yaml cannot ride that validator without a new parser dependency.
These tests pin what the repo can enforce without one: the single-
file invariant, the surviving content, the directory mount, and a
green validation gate.
Run: python3 -m unittest tests.test_continue_config
"""

import json
import os
import re
import subprocess
import sys
import unittest

REPO_ROOT = os.path.abspath(".")
CONTINUE_DIR = os.path.abspath(os.path.join("config", "continue"))
JSON_PATH = os.path.join(CONTINUE_DIR, "config.json")
YAML_PATH = os.path.join(CONTINUE_DIR, "config.yaml")
SCHEMA_PATH = os.path.abspath(os.path.join("config", "schema.json"))
COMPOSE_PATH = os.path.abspath("docker-compose.yml")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class SingleConfigTest(unittest.TestCase):
    def test_legacy_json_is_gone(self):
        # The drift trap: two formats, one silently wins. The legacy
        # format must not exist on disk.
        self.assertFalse(
            os.path.exists(JSON_PATH),
            "config/continue/config.json still present: the Continue "
            "drift trap (issue #1206) is back",
        )

    def test_exactly_one_config_file_remains(self):
        names = sorted(
            name for name in os.listdir(CONTINUE_DIR) if name.startswith("config.")
        )
        self.assertEqual(names, ["config.yaml"])

    def test_yaml_carries_ollama_qwen_wiring(self):
        # Same intent the deleted json carried (Ollama qwen3:8b on
        # the compose network, chat + edit roles, autocomplete).
        # Line-oriented: no YAML parser in the CI test env.
        text = _read(YAML_PATH)
        self.assertIn("model: qwen3:8b", text)
        self.assertIn("provider: ollama", text)
        self.assertIn("apiBase: http://ollama:11434", text)
        self.assertIn("tabAutocompleteModel", text)
        roles = re.search(r"roles:(.*?)(?:\n\S|\Z)", text, re.S)
        self.assertIsNotNone(roles, "yaml must declare model roles")
        self.assertIn("chat", roles.group(1))
        self.assertIn("edit", roles.group(1))

    def test_yaml_declares_local_ollama_once(self):
        # Exactly one Ollama endpoint: a second apiBase (localhost,
        # remote host) would split traffic or refuse inside the
        # code-server container network.
        text = _read(YAML_PATH)
        self.assertEqual(
            len(re.findall(r"apiBase:", text)),
            2,
            "models + tabAutocompleteModel endpoints",
        )
        self.assertNotIn("localhost", text)
        for line in text.splitlines():
            if "apiBase" in line:
                self.assertIn("http://ollama:11434", line)

    def test_compose_mounts_continue_dir_read_only(self):
        # The mount serves the whole directory ro, so the surviving
        # yaml is what code-server reads — no per-file mount still
        # pointing at the deleted json.
        text = _read(COMPOSE_PATH)
        mounts = [
            line.strip() for line in text.splitlines() if "config/continue" in line
        ]
        self.assertEqual(
            len(mounts), 1, "expected exactly one config/continue mount: %r" % mounts
        )
        self.assertIn("/home/coder/.continue", mounts[0])
        self.assertIn(":ro", mounts[0])
        self.assertNotIn("config.json", mounts[0])

    def test_schema_has_no_dangling_continue_reference(self):
        schema = json.loads(_read(SCHEMA_PATH))
        for key in schema:
            self.assertNotIn("continue", key.lower())

    def test_repo_validation_gate_still_green(self):
        # The deletion must not break `python -m backend.validate_config`.
        proc = subprocess.run(
            [sys.executable, "-m", "backend.validate_config"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=REPO_ROOT,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("FAIL", proc.stdout)


if __name__ == "__main__":
    unittest.main()
