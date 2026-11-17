import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class TestCoderLauncher(unittest.TestCase):
    def launch(self, args):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root, "deep-agent")
            directory.mkdir()
            (directory / "coder.py").write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n"
                "raise SystemExit(7 if '--fail' in sys.argv else 0)\n"
            )
            return subprocess.run(
                ["bash", "terminal/bin/s5-coder", *args],
                env={
                    **os.environ,
                    "S5_CODER_REPO": root,
                    "PATH": str(Path(sys.executable).parent)
                    + os.pathsep
                    + os.environ["PATH"],
                },
                capture_output=True,
                text=True,
                timeout=10,
            )

    def test_fresh_checkout_without_repo_venv(self):
        result = self.launch([])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [])

    def test_preserves_argument_boundaries(self):
        args = ["--model", "ollama:qwen", "brief with spaces"]
        result = self.launch(args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), args)

    def test_propagates_coder_failure(self):
        self.assertEqual(self.launch(["--fail"]).returncode, 7)
