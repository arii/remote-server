"""Config schema tests: live files conform, and the validator
catches drift. Fixture cases run against a temp --root; the live-tree
case pins the real files. Run: python3 tests/test_config_schema.py"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core_cli import validate_config

ROOT = os.path.join(os.path.dirname(__file__), "..")


def write(root, rel, text):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


SCHEMA = {
    "a.json": {
        "type": "object",
        "required": ["name", "n"],
        "properties": {
            "name": {"type": "string"},
            "n": {"type": "integer", "minimum": 0},
            "chain": {"$ref": "#/$defs/chain"},
        },
    },
    ".env.example": {"type": "env-keys"},
    "$defs": {
        "chain": {
            "type": "object",
            "required": ["primary"],
            "properties": {"primary": {"type": "string"}},
        }
    },
}


class TestValidator(unittest.TestCase):
    def make_root(self, files):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        write(tmp.name, "config/schema.json", json.dumps(SCHEMA))
        for rel, text in files.items():
            write(tmp.name, rel, text)
        return tmp.name

    def validate(self, root):
        out = {}
        schema = json.load(open(os.path.join(root, "config/schema.json")))
        for rel, section in schema.items():
            if rel.startswith("$"):
                continue
            out[rel] = validate_config.validate_file(
                root, rel, section, schema)
        return out

    def test_happy_path(self):
        root = self.make_root({
            "a.json": '{"name": "x", "n": 2, "chain": {"primary": "m"}}',
            "docker-compose.yml": "image: a:${TAG:-latest}\n",
            ".env.example": "# nothing needed\n",
        })
        out = self.validate(root)
        self.assertEqual(out["a.json"], [])
        self.assertEqual(out[".env.example"], [])

    def test_catches_shape_drift(self):
        root = self.make_root({
            "a.json": '{"n": -1, "chain": {}}',
            "docker-compose.yml": "",
            ".env.example": "",
        })
        errors = self.validate(root)["a.json"]
        joined = "\n".join(errors)
        self.assertIn("missing 'name'", joined)
        self.assertIn("want >= 0", joined)
        self.assertIn("missing 'primary'", joined)

    def test_catches_undocumented_compose_var(self):
        root = self.make_root({
            "a.json": '{"name": "x", "n": 0}',
            "docker-compose.yml": "key: ${SECRET_KEY}\n",
            ".env.example": "OTHER=1\n",
        })
        errors = self.validate(root)[".env.example"]
        self.assertEqual(len(errors), 1)
        self.assertIn("SECRET_KEY", errors[0])

    def test_missing_file_and_bad_json(self):
        root = self.make_root({
            "docker-compose.yml": "",
            ".env.example": "",
        })
        errors = self.validate(root)["a.json"]
        self.assertTrue(any("file missing" in e for e in errors))
        write(root, "a.json", "{nope")
        errors = self.validate(root)["a.json"]
        self.assertTrue(any("invalid JSON" in e for e in errors))


class TestLiveTree(unittest.TestCase):
    def test_live_configs_validate(self):
        proc = subprocess.run(
            [sys.executable, "-m", "core_cli", "validate-config"],
            capture_output=True, text=True, timeout=120,
            cwd=os.path.abspath(ROOT),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("FAIL", proc.stdout)


if __name__ == "__main__":
    unittest.main()
