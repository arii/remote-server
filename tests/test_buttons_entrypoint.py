"""Tests for scripts/buttons-entrypoint.sh (#562).

Wiring tests assert the Dockerfile entrypoint/call chain; behavior
tests run the script against fake app roots (fake venv python +
fake build_index.py) so no container is ever needed. The happy-path
and build-failure tests need a reachable Ollama (one instant
readiness probe); they skip otherwise. All other cases point
OLLAMA_URL at a closed port.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
ENTRYPOINT = os.path.join(ROOT, "scripts", "buttons-entrypoint.sh")

SCRUB = ("SKIP_INDEX_BUILD", "S5_CHROMA_DIR", "APP_ROOT",
         "DEEP_AGENT_VENV", "OLLAMA_WAIT_SECS")
CLOSED_PORT_URL = "http://127.0.0.1:9/"


def _ollama_up():
    base = (
        os.environ.get("OLLAMA_URL") or "http://ollama:11434").rstrip("/")
    try:
        with urllib.request.urlopen(
                base + "/api/tags", timeout=5) as res:
            return res.status == 200
    except Exception:
        return False


def _run(args, extra_env, timeout=120):
    env = dict(os.environ)
    for key in SCRUB:
        env.pop(key, None)
    env.update(extra_env)
    return subprocess.run(
        [ENTRYPOINT, *args], capture_output=True, text=True,
        timeout=timeout, env=env)


def _write_exe(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    os.chmod(path, 0o755)


class TestEntrypointWiring(unittest.TestCase):
    def test_script_present_and_executable(self):
        self.assertTrue(os.path.isfile(ENTRYPOINT))
        self.assertTrue(os.access(ENTRYPOINT, os.X_OK))

    def test_dockerfile_wires_entrypoint(self):
        with open(os.path.join(ROOT, "Dockerfile")) as f:
            body = f.read()
        self.assertIn(
            "COPY scripts/buttons-entrypoint.sh "
            "/usr/local/bin/buttons-entrypoint.sh", body)
        self.assertIn(
            'ENTRYPOINT ["/usr/local/bin/buttons-entrypoint.sh"]',
            body)

    def test_dockerignore_keeps_scripts(self):
        with open(os.path.join(ROOT, ".dockerignore")) as f:
            lines = [
                line.strip()
                for line in f
                if line.strip() and not line.startswith("#")
            ]
        self.assertIn("!scripts/", lines)
        self.assertIn("!scripts/**", lines)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_bash_syntax(self):
        proc = subprocess.run(
            ["bash", "-n", ENTRYPOINT], capture_output=True, text=True,
            timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipUnless(
        shutil.which("shellcheck"), "needs shellcheck")
    def test_shellcheck(self):
        proc = subprocess.run(
            ["shellcheck", "-S", "warning", ENTRYPOINT],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("curl"),
    "needs bash + curl")
class TestEntrypointBehavior(unittest.TestCase):
    def test_skip_flag_passthrough(self):
        proc = _run(
            ["echo", "skip-marker"],
            {"SKIP_INDEX_BUILD": "1",
             "OLLAMA_URL": CLOSED_PORT_URL,
             "DEEP_AGENT_VENV": "/nonexistent/python",
             "S5_CHROMA_DIR": "/nonexistent/index"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SKIP_INDEX_BUILD=1", proc.stdout)
        self.assertIn("skip-marker", proc.stdout)

    def test_present_index_skips_build(self):
        with tempfile.TemporaryDirectory() as index_dir:
            with open(os.path.join(
                    index_dir, "chroma.sqlite3"), "wb") as f:
                f.write(b"fake-index-bytes")
            proc = _run(
                ["echo", "present-marker"],
                {"S5_CHROMA_DIR": index_dir,
                 "OLLAMA_URL": CLOSED_PORT_URL,
                 "DEEP_AGENT_VENV": "/nonexistent/python"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skipping rebuild", proc.stdout)
        self.assertIn("present-marker", proc.stdout)

    def test_ollama_down_refuses_empty_index(self):
        with tempfile.TemporaryDirectory() as index_dir:
            proc = _run(
                ["echo", "must-not-print"],
                {"S5_CHROMA_DIR": index_dir,
                 "OLLAMA_URL": CLOSED_PORT_URL,
                 "OLLAMA_WAIT_SECS": "0",
                 "DEEP_AGENT_VENV": "/nonexistent/python"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "refusing to serve an empty index", proc.stderr)
        self.assertNotIn("must-not-print", proc.stdout)

    def _fake_app(self, root, build_exit=0):
        """Fake APP_ROOT: venv shim logs argv then execs the build
        shim, which records that it ran."""
        deep = os.path.join(root, "deep-agent")
        os.makedirs(deep)
        log = os.path.join(root, "venv.log")
        marker = os.path.join(root, "built.marker")
        venv = os.path.join(root, "venv-python")
        _write_exe(
            venv,
            "#!/bin/sh\n"
            f'echo "venv-argv: $*" >> "{log}"\n'
            'exec "$@"\n')
        _write_exe(
            os.path.join(deep, "build_index.py"),
            "#!/bin/sh\n"
            f'echo "built" > "{marker}"\n'
            f"exit {build_exit}\n")
        return venv, log, marker

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_missing_index_builds_then_execs(self):
        with tempfile.TemporaryDirectory() as app_root:
            venv, log, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "happy-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "DEEP_AGENT_VENV": venv})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("building RAG index", proc.stdout)
                self.assertIn("happy-marker", proc.stdout)
                with open(marker, encoding="utf-8") as f:
                    self.assertEqual(f.read().strip(), "built")
                with open(log, encoding="utf-8") as f:
                    self.assertIn(
                        "deep-agent/build_index.py", f.read())

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_empty_sqlite_rebuilds(self):
        with tempfile.TemporaryDirectory() as app_root:
            venv, _, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                open(os.path.join(
                    index_dir, "chroma.sqlite3"), "wb").close()
                proc = _run(
                    ["echo", "empty-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "DEEP_AGENT_VENV": venv})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("building RAG index", proc.stdout)
                with open(marker, encoding="utf-8") as f:
                    self.assertEqual(f.read().strip(), "built")

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_build_failure_refuses_empty_index(self):
        with tempfile.TemporaryDirectory() as app_root:
            venv, _, _ = self._fake_app(app_root, build_exit=3)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "must-not-print"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "DEEP_AGENT_VENV": venv})
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "refusing to serve an empty index", proc.stderr)
        self.assertNotIn("must-not-print", proc.stdout)


if __name__ == "__main__":
    unittest.main()
