import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(".")
FILES = (
    "Dockerfile",
    "terminal/Dockerfile",
    ".github/workflows/e2e.yml",
    ".github/workflows/ci.yml",
    ".github/workflows/ai-review.yml",
    "tests/e2e/run.sh",
)
SHA = "a" * 40


class BumpBoomtickCliTests(unittest.TestCase):
    def run_bump(self, status="ahead", tag="cli-v9.0.0", broken=None, released=False):
        workflow = yaml.safe_load(
            (ROOT / ".github/workflows/bump-boomtick-cli.yml").read_text()
        )
        script = workflow["jobs"]["bump"]["steps"][1]["run"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                content = (ROOT / name).read_text()
                if released:
                    content = content.replace(
                        "# v0.20.2 + unreleased workflows/MCP changes; no compatible release yet.",
                        "# v1.0.0",
                    )
                path.write_text(content)
            if broken:
                (root / broken).write_text("no pin\n")
            before = {name: (root / name).read_text() for name in FILES}
            bin_dir = root / "bin"
            bin_dir.mkdir()
            commands = {
                "gh": """#!/bin/bash
case "$2" in
  */releases) printf '%s' "$RELEASE_RESULT" ;;
  */commits/*) printf '%s' "$SHA_RESULT" ;;
  */compare/*) printf '%s' "$STATUS_RESULT" ;;
  *) exit 99 ;;
esac
""",
                "git": """#!/bin/bash
if [[ "$*" == 'diff --quiet' ]]; then exit 0; fi
exit 99
""",
            }
            for name, body in commands.items():
                path = bin_dir / name
                path.write_text(body)
                path.chmod(0o755)
            result = subprocess.run(
                ["bash", "-c", script],
                cwd=root,
                env={
                    **os.environ,
                    "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
                    "RELEASE_RESULT": json.dumps(
                        [
                            {
                                "tag_name": tag,
                                "draft": False,
                                "prerelease": False,
                                "published_at": "2026-11-19T00:00:00Z",
                            }
                        ]
                        if tag
                        else []
                    ),
                    "SHA_RESULT": SHA,
                    "STATUS_RESULT": status,
                },
                capture_output=True,
                text=True,
            )
            after = {name: (root / name).read_text() for name in FILES}
        return result, before, after

    def test_newer_release_updates_all_pins(self):
        result, before, after = self.run_bump()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in FILES:
            self.assertNotEqual(before[name], after[name])
            self.assertIn(SHA, after[name])
        self.assertIn("# v9.0.0", after[".github/workflows/e2e.yml"])

    def test_old_or_identical_release_keeps_pins(self):
        for status in ("behind", "identical"):
            with self.subTest(status=status):
                result, before, after = self.run_bump(status=status)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(before, after)

    def test_subsequent_release_updates_version_comments(self):
        result, _, after = self.run_bump(released=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("# v9.0.0", after[".github/workflows/e2e.yml"])
        for content in after.values():
            self.assertNotIn("# v1.0.0", content)

    def test_no_release_fails_without_writing(self):
        result, before, after = self.run_bump(tag="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no published cli release found", result.stdout)
        self.assertEqual(before, after)

    def test_diverged_release_fails_without_writing(self):
        result, before, after = self.run_bump(status="diverged")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manual review required", result.stdout)
        self.assertEqual(before, after)

    def test_missing_pin_fails_before_any_write(self):
        result, before, after = self.run_bump(broken=FILES[-1])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected one immutable pin", result.stderr)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
