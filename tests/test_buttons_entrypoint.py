"""Tests for scripts/buttons-entrypoint.sh (#562, #699).

Wiring tests assert the Dockerfile entrypoint/call chain; behavior
tests run the script against fake app roots (fake venv python +
fake build_index.py) so no container is ever needed. The server
execs immediately while the index builds in the background: tests
assert the fast boot synchronously, then poll the readiness markers
(.index_building -> gone, .index_failed) for the background outcome.
Cases needing a passing Ollama probe skip without one; all other
cases point OLLAMA_URL at a closed port.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
ENTRYPOINT = os.path.join(ROOT, "scripts", "buttons-entrypoint.sh")

SCRUB = ("SKIP_INDEX_BUILD", "S5_CHROMA_DIR", "APP_ROOT",
         "DEEP_AGENT_VENV", "OLLAMA_WAIT_SECS", "OLLAMA_TRIES")
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


def _wait_for(path, timeout=20):
    """Poll for a background step to create path. Returns bool."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


def _wait_gone(path, timeout=20):
    """Poll for a background step to remove path. Returns bool."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not os.path.exists(path):
            return True
        time.sleep(0.1)
    return False


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
            self.assertFalse(os.path.exists(os.path.join(
                index_dir, ".index_building")))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("skipping rebuild", proc.stdout)
        self.assertIn("present-marker", proc.stdout)

    def test_missing_index_boots_without_waiting(self):
        # Ollama down: the server still execs at once; the build
        # fails in the background and records .index_failed.
        with tempfile.TemporaryDirectory() as app_root:
            _, _, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "boot-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "OLLAMA_URL": CLOSED_PORT_URL,
                     "OLLAMA_WAIT_SECS": "0",
                     "OLLAMA_TRIES": "2",
                     "DEEP_AGENT_VENV": "/nonexistent/python"})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("server starting now", proc.stdout)
                self.assertIn("boot-marker", proc.stdout)
                self.assertTrue(os.path.isfile(os.path.join(
                    index_dir, ".index_building")))
                self.assertTrue(
                    _wait_for(os.path.join(
                        index_dir, ".index_failed")),
                    "background build never recorded its failure")
                self.assertTrue(_wait_gone(os.path.join(
                    index_dir, ".index_building")))
                self.assertFalse(os.path.exists(marker))
                with open(os.path.join(
                        index_dir, "index_build.log"),
                        encoding="utf-8") as f:
                    self.assertIn("ollama unreachable", f.read())

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
    def test_missing_index_builds_in_background(self):
        with tempfile.TemporaryDirectory() as app_root:
            venv, log, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "happy-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "DEEP_AGENT_VENV": venv})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("server starting now", proc.stdout)
                self.assertIn("happy-marker", proc.stdout)
                self.assertTrue(
                    _wait_for(marker),
                    "background build never ran")
                self.assertTrue(_wait_gone(os.path.join(
                    index_dir, ".index_building")))
                self.assertFalse(os.path.exists(os.path.join(
                    index_dir, ".index_failed")))
                with open(marker, encoding="utf-8") as f:
                    self.assertEqual(f.read().strip(), "built")
                with open(log, encoding="utf-8") as f:
                    self.assertIn(
                        "deep-agent/build_index.py", f.read())
                with open(os.path.join(
                        index_dir, "index_build.log"),
                        encoding="utf-8") as f:
                    self.assertIn("build done", f.read())

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_empty_sqlite_rebuilds_in_background(self):
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
                self.assertIn("server starting now", proc.stdout)
                self.assertIn("empty-marker", proc.stdout)
                self.assertTrue(
                    _wait_for(marker),
                    "background rebuild never ran")
                self.assertTrue(_wait_gone(os.path.join(
                    index_dir, ".index_building")))

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_stale_building_marker_rebuilds_in_background(self):
        # Killed mid-build then `restart`: usable-looking sqlite but
        # a leftover .index_building forces a background rebuild.
        with tempfile.TemporaryDirectory() as app_root:
            venv, _, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                with open(os.path.join(
                        index_dir, "chroma.sqlite3"), "wb") as f:
                    f.write(b"partial-index-bytes")
                open(os.path.join(
                    index_dir, ".index_building"), "w").close()
                proc = _run(
                    ["echo", "stale-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "DEEP_AGENT_VENV": venv})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("server starting now", proc.stdout)
                self.assertTrue(
                    _wait_for(marker),
                    "background rebuild never ran")

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_build_failure_boots_then_marks_failed(self):
        with tempfile.TemporaryDirectory() as app_root:
            venv, _, _ = self._fake_app(app_root, build_exit=3)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "fail-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "DEEP_AGENT_VENV": venv})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("server starting now", proc.stdout)
                self.assertIn("fail-marker", proc.stdout)
                failed = os.path.join(index_dir, ".index_failed")
                self.assertTrue(
                    _wait_for(failed),
                    "background build never recorded its failure")
                self.assertTrue(_wait_gone(os.path.join(
                    index_dir, ".index_building")))
                with open(failed, encoding="utf-8") as f:
                    self.assertIn("build failed", f.read())


if __name__ == "__main__":
    unittest.main()
