"""Tests for scripts/buttons-entrypoint.sh (#562, #699).

Wiring tests assert the Dockerfile entrypoint/call chain; behavior
tests run the script against fake app roots (fake `td` on PATH)
so no container is ever needed. The server
execs immediately while the index builds in the background: tests
assert the fast boot synchronously, then poll the readiness markers
(.index_building -> gone, .index_failed) for the background outcome.
Cases needing a passing Ollama probe skip without one; all other
cases point OLLAMA_URL at a closed port.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
ENTRYPOINT = os.path.join(ROOT, "scripts", "buttons-entrypoint.sh")

SCRUB = ("SKIP_INDEX_BUILD", "S5_CHROMA_DIR", "APP_ROOT",
         "BUTTONS_STACK", "CHUNK_FLOOR",
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


def _write_sqlite(path, rows):
    """Real chroma-shaped sqlite: embeddings table with N rows."""
    import sqlite3
    if os.path.exists(path):
        os.unlink(path)
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE embeddings (id INTEGER PRIMARY KEY)")
        con.executemany("INSERT INTO embeddings (id) VALUES (?)",
                        [(i,) for i in range(rows)])
        con.commit()


def _git_init_stack(root):
    """Turn root into a git checkout with one commit; return its SHA."""
    subprocess.run(["git", "init", "-q", root], check=True,
                   capture_output=True, timeout=60)
    subprocess.run(["git", "-C", root, "config", "user.email",
                    "test@example.com"], check=True,
                   capture_output=True, timeout=60)
    subprocess.run(["git", "-C", root, "config", "user.name",
                    "test"], check=True,
                   capture_output=True, timeout=60)
    with open(os.path.join(root, "probe.txt"), "w",
              encoding="utf-8") as f:
        f.write("probe\n")
    subprocess.run(["git", "-C", root, "add", "probe.txt"],
                   check=True, capture_output=True, timeout=60)
    subprocess.run(["git", "-C", root, "commit", "-qm", "seed"],
                   check=True, capture_output=True, timeout=60)
    proc = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True,
                          timeout=60)
    return proc.stdout.strip()


def _write_td_meta(stack_root, sha):
    meta_dir = os.path.join(stack_root, ".td_rag")
    os.makedirs(meta_dir, exist_ok=True)
    with open(os.path.join(meta_dir, "meta.json"), "w",
              encoding="utf-8") as f:
        f.write('{"last_indexed_sha": "%s"}' % sha)


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

    def test_dockerfile_pins_browsers_path_before_bake(self):
        with open(os.path.join(
                ROOT, "Dockerfile"), encoding="utf-8") as f:
            body = f.read()
        pin = "ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright"
        self.assertIn(pin, body)
        self.assertLess(body.index(pin),
                        body.index("playwright install"),
                        "the bake must run under the pinned path")


@unittest.skipUnless(
    shutil.which("bash") and shutil.which("curl"),
    "needs bash + curl")
class TestEntrypointBehavior(unittest.TestCase):
    def test_skip_flag_passthrough(self):
        proc = _run(
            ["echo", "skip-marker"],
            {"SKIP_INDEX_BUILD": "1",
             "OLLAMA_URL": CLOSED_PORT_URL,
             "S5_CHROMA_DIR": "/nonexistent/index"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("SKIP_INDEX_BUILD=1", proc.stdout)
        self.assertIn("skip-marker", proc.stdout)

    def test_present_index_skips_build(self):
        with tempfile.TemporaryDirectory() as index_dir:
            _write_sqlite(os.path.join(
                index_dir, "chroma.sqlite3"), 1500)
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
            bindir, _, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "boot-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "OLLAMA_URL": CLOSED_PORT_URL,
                     "OLLAMA_WAIT_SECS": "0",
                     "OLLAMA_TRIES": "2"})
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

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_live_checkout_root_used_for_index(self):
        # The baked /app copy has no .git and goes stale on deploy:
        # the index must build from the mounted live checkout
        # (BUTTONS_STACK doctrine in the Dockerfile).
        with tempfile.TemporaryDirectory() as app_root:
            self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as stack_root:
                stack_bind, log, marker = self._fake_app(stack_root)
                with tempfile.TemporaryDirectory() as index_dir:
                    proc = _run(
                        ["echo", "happy-marker"],
                        {"APP_ROOT": app_root,
                         "BUTTONS_STACK": stack_root,
                         "S5_CHROMA_DIR": index_dir,
                         "PATH": stack_bind + os.pathsep + os.environ.get(
                             "PATH", "")})
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertTrue(
                        _wait_for(marker),
                        "background build never ran")
                    with open(log, encoding="utf-8") as f:
                        body = f.read()
                    self.assertIn(stack_root, body)
                    self.assertNotIn(app_root, body)
                    # Missing artifacts must force a full rebuild:
                    # incremental state (stale SHA, wiped sqlite)
                    # no-ops and leaves RAG unavailable.
                    self.assertIn("--full", body)

    def test_generate_suffix_stripped_from_probe(self):
        # OLLAMA_URL ships with the /api/generate path in compose;
        # the tags probe must hit the bare host, or a healthy
        # Ollama 404s forever and the index degrades to failed
        # (same suffix bug as model_router #968).
        with tempfile.TemporaryDirectory() as app_root:
            self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "boot-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "OLLAMA_URL": "http://127.0.0.1:9/api/generate",
                     "OLLAMA_WAIT_SECS": "0",
                     "OLLAMA_TRIES": "2"})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                failed = os.path.join(index_dir, ".index_failed")
                self.assertTrue(
                    _wait_for(failed),
                    "background build never recorded its failure")
                with open(failed, encoding="utf-8") as f:
                    body = f.read()
                self.assertIn("http://127.0.0.1:9", body)
                self.assertNotIn("/api/generate", body)

    def _fake_app(self, root, build_exit=0, write_sqlite=True,
                  sqlite_rows=1500):
        """Fake APP_ROOT: a fake `td` on a shim PATH records its
        argv and the index build outcome. With write_sqlite it also
        drops a real chroma-shaped sqlite (sqlite_rows embedding
        rows) into the S5_CHROMA_DIR the entrypoint watches; without
        it the shim reproduces #1094's silent success (exit 0, no
        sqlite)."""
        bindir = os.path.join(root, "fakebin")
        os.makedirs(bindir)
        log = os.path.join(root, "td.log")
        marker = os.path.join(root, "built.marker")
        seed = os.path.join(root, "seed.sqlite3")
        if write_sqlite:
            _write_sqlite(seed, sqlite_rows)
            write_step = (
                'if [ -n "$S5_CHROMA_DIR" ]; then '
                f'cp "{seed}" '
                '"$S5_CHROMA_DIR/chroma.sqlite3"; fi\n'
            )
        else:
            write_step = ""
        _write_exe(
            os.path.join(bindir, "td"),
            "#!/bin/sh\n"
            f'echo "td-argv: $*" >> "{log}"\n'
            f'echo "built" > "{marker}"\n'
            f"{write_step}"
            f"exit {build_exit}\n")
        return bindir, log, marker

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_missing_index_builds_in_background(self):
        with tempfile.TemporaryDirectory() as app_root:
            bindir, log, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "happy-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "PATH": bindir + os.pathsep + os.environ.get(
                         "PATH", "")})
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
                    body = f.read()
                    self.assertIn("rag", body)
                    self.assertIn("index", body)
                    self.assertIn(app_root, body)
                with open(os.path.join(
                        index_dir, "index_build.log"),
                        encoding="utf-8") as f:
                    self.assertIn("build done", f.read())

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_empty_sqlite_rebuilds_in_background(self):
        with tempfile.TemporaryDirectory() as app_root:
            bindir, _, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                open(os.path.join(
                    index_dir, "chroma.sqlite3"), "wb").close()
                proc = _run(
                    ["echo", "empty-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "PATH": bindir + os.pathsep + os.environ.get(
                         "PATH", "")})
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
            bindir, _, marker = self._fake_app(app_root)
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
                     "PATH": bindir + os.pathsep + os.environ.get(
                         "PATH", "")})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("server starting now", proc.stdout)
                self.assertTrue(
                    _wait_for(marker),
                    "background rebuild never ran")

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_silent_success_marks_failed(self):
        # #1094: `td rag index` exits 0 while writing only meta (no
        # embedding backend, no sqlite). The entrypoint must not
        # report success then: missing sqlite after a zero-exit
        # build records .index_failed just like a hard failure.
        with tempfile.TemporaryDirectory() as app_root:
            bindir, _, _ = self._fake_app(
                app_root, build_exit=0, write_sqlite=False)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "silent-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "PATH": bindir + os.pathsep + os.environ.get(
                         "PATH", "")})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("server starting now", proc.stdout)
                self.assertIn("silent-marker", proc.stdout)
                failed = os.path.join(index_dir, ".index_failed")
                self.assertTrue(
                    _wait_for(failed),
                    "silent success never recorded its failure")
                self.assertTrue(_wait_gone(os.path.join(
                    index_dir, ".index_building")))
                with open(failed, encoding="utf-8") as f:
                    self.assertIn("chroma.sqlite3", f.read())

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_thin_sqlite_rebuilds_in_background(self):
        # #1119: a non-empty sqlite is not proof of servability — 50
        # rows (floor 1000) must trigger a rebuild, and the rebuild
        # must record build_meta.json with the proved chunk count.
        with tempfile.TemporaryDirectory() as app_root:
            bindir, _, _ = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as index_dir:
                _write_sqlite(os.path.join(
                    index_dir, "chroma.sqlite3"), 50)
                proc = _run(
                    ["echo", "thin-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "PATH": bindir + os.pathsep + os.environ.get(
                         "PATH", "")})
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("thin-marker", proc.stdout)
                self.assertTrue(
                    _wait_gone(os.path.join(
                        index_dir, ".index_building")))
                self.assertFalse(os.path.exists(os.path.join(
                    index_dir, ".index_failed")))
                meta_path = os.path.join(index_dir, "build_meta.json")
                self.assertTrue(os.path.isfile(meta_path))
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                self.assertGreaterEqual(meta["chunks"], 1000)

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_behind_sha_rebuilds_in_git_stack(self):
        # td meta records an older SHA than the stack HEAD: the index
        # no longer matches the tree, so it must rebuild even though
        # the sqlite is thick.
        with tempfile.TemporaryDirectory() as app_root:
            bindir, log, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as stack_root:
                _git_init_stack(stack_root)
                _write_td_meta(stack_root, "0" * 40)
                with tempfile.TemporaryDirectory() as index_dir:
                    _write_sqlite(os.path.join(
                        index_dir, "chroma.sqlite3"), 1500)
                    proc = _run(
                        ["echo", "behind-marker"],
                        {"APP_ROOT": app_root,
                         "BUTTONS_STACK": stack_root,
                         "S5_CHROMA_DIR": index_dir,
                         "PATH": bindir + os.pathsep + os.environ.get(
                             "PATH", "")})
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("behind-marker", proc.stdout)
                    self.assertTrue(
                        _wait_for(marker),
                        "background rebuild never ran")
                    with open(log, encoding="utf-8") as f:
                        self.assertIn("rag", f.read())

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_fresh_git_stack_skips_build(self):
        # sqlite thick AND td meta SHA matches stack HEAD: the one
        # case where a rebuild is provably unnecessary.
        with tempfile.TemporaryDirectory() as app_root:
            self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as stack_root:
                head = _git_init_stack(stack_root)
                _write_td_meta(stack_root, head)
                with tempfile.TemporaryDirectory() as index_dir:
                    _write_sqlite(os.path.join(
                        index_dir, "chroma.sqlite3"), 1500)
                    proc = _run(
                        ["echo", "fresh-marker"],
                        {"APP_ROOT": app_root,
                         "BUTTONS_STACK": stack_root,
                         "S5_CHROMA_DIR": index_dir,
                         "PATH": os.path.join(
                             app_root,
                             "fakebin") + os.pathsep + os.environ.get(
                             "PATH", "")})
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("fresh-marker", proc.stdout)
                    self.assertIn("skipping rebuild", proc.stdout)

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_missing_meta_rebuilds_in_git_stack(self):
        # No td meta in a git stack: freshness is unprovable, so a
        # thick sqlite alone must not skip the build (#1119).
        with tempfile.TemporaryDirectory() as app_root:
            bindir, log, marker = self._fake_app(app_root)
            with tempfile.TemporaryDirectory() as stack_root:
                _git_init_stack(stack_root)
                with tempfile.TemporaryDirectory() as index_dir:
                    _write_sqlite(os.path.join(
                        index_dir, "chroma.sqlite3"), 1500)
                    proc = _run(
                        ["echo", "nometa-marker"],
                        {"APP_ROOT": app_root,
                         "BUTTONS_STACK": stack_root,
                         "S5_CHROMA_DIR": index_dir,
                         "PATH": bindir + os.pathsep + os.environ.get(
                             "PATH", "")})
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("nometa-marker", proc.stdout)
                    self.assertTrue(
                        _wait_for(marker),
                        "background rebuild never ran")
                    with open(log, encoding="utf-8") as f:
                        self.assertIn("rag", f.read())

    @unittest.skipUnless(_ollama_up(), "needs reachable Ollama")
    def test_build_failure_boots_then_marks_failed(self):
        with tempfile.TemporaryDirectory() as app_root:
            bindir, _, _ = self._fake_app(app_root, build_exit=3)
            with tempfile.TemporaryDirectory() as index_dir:
                proc = _run(
                    ["echo", "fail-marker"],
                    {"APP_ROOT": app_root,
                     "S5_CHROMA_DIR": index_dir,
                     "PATH": bindir + os.pathsep + os.environ.get(
                         "PATH", "")})
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
