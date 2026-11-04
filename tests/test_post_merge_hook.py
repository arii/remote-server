"""Tests for hooks/post-merge (stdlib unittest only).

Runs the tracked hook against fixture git repos: a merge touching
backend/ must skip the auto-rebuild outside the live project or off
main, and must rebuild inside the live project on main. Every test
shadows docker with a recording shim, so no container is ever
touched even if a guard regresses.
"""

import os
import subprocess
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(__file__), "..", "hooks",
                    "post-merge")


def _git(cwd, *args, env=None):
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        ["git", *args], cwd=cwd, env=merged, capture_output=True,
        text=True, check=True)


def _fixture(branch="main"):
    tmp = tempfile.TemporaryDirectory()
    cwd = tmp.name
    _git(cwd, "init", "-b", branch)
    _git(cwd, "config", "user.email", "test@example.com")
    _git(cwd, "config", "user.name", "test")
    _git(cwd, "commit", "--allow-empty", "-m", "root")
    os.makedirs(os.path.join(cwd, "backend"), exist_ok=True)
    with open(os.path.join(cwd, "backend", "x.py"), "w") as fh:
        fh.write("x = 1\n")
    _git(cwd, "add", ".")
    _git(cwd, "commit", "-m", "touch backend")
    _git(cwd, "update-ref", "ORIG_HEAD", "HEAD~1")
    return tmp


def _run_hook(cwd, extra_env=None):
    import shutil

    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    bash = shutil.which("bash") or "/bin/bash"
    return subprocess.run(
        [bash, os.path.abspath(HOOK)], cwd=cwd, env=env,
        capture_output=True, text=True)


def _docker_shim():
    """A PATH-leading fake docker that records its argv and succeeds,
    so the rebuild step is proved without touching containers."""
    tmp = tempfile.TemporaryDirectory()
    marker = os.path.join(tmp.name, "docker-argv")
    with open(os.path.join(tmp.name, "docker"), "w") as fh:
        fh.write(f"#!/bin/sh\necho \"$@\" > {marker}\nexit 0\n")
    os.chmod(os.path.join(tmp.name, "docker"), 0o755)
    path = tmp.name + os.pathsep + os.environ.get("PATH", "")
    return tmp, marker, path


class PostMergeHookTest(unittest.TestCase):
    def test_skips_outside_live_project(self):
        tmp = _fixture()
        shim, marker, path = _docker_shim()
        try:
            proc = _run_hook(tmp.name, {"PATH": path})
            self.assertEqual(proc.returncode, 0)
            self.assertIn("skipping auto-rebuild", proc.stdout)
            self.assertIn("not the live project", proc.stdout)
            self.assertNotIn("rebuilding buttons container",
                             proc.stdout)
            self.assertFalse(os.path.exists(marker))
        finally:
            tmp.cleanup()
            shim.cleanup()

    def test_skips_off_main(self):
        tmp = _fixture(branch="side")
        shim, marker, path = _docker_shim()
        try:
            proc = _run_hook(
                tmp.name,
                {"REMOTE_BUTTONS_PROJECT_DIR": tmp.name,
                 "PATH": path})
            self.assertEqual(proc.returncode, 0)
            self.assertIn("skipping auto-rebuild", proc.stdout)
            self.assertIn("not main", proc.stdout)
            self.assertNotIn("rebuilding buttons container",
                             proc.stdout)
            self.assertFalse(os.path.exists(marker))
        finally:
            tmp.cleanup()
            shim.cleanup()

    def test_rebuilds_in_live_project_on_main(self):
        tmp = _fixture()
        shim, marker, path = _docker_shim()
        try:
            proc = _run_hook(
                tmp.name,
                {"REMOTE_BUTTONS_PROJECT_DIR": tmp.name,
                 "PATH": path})
            self.assertEqual(proc.returncode, 0)
            self.assertNotIn("skipping auto-rebuild", proc.stdout)
            self.assertIn("rebuilding buttons container",
                          proc.stdout)
            with open(marker) as fh:
                argv = fh.read()
            self.assertIn("up -d --build buttons", argv)
            self.assertIn(tmp.name, argv)
        finally:
            tmp.cleanup()
            shim.cleanup()

    def test_ignores_non_buttons_changes(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            cwd = tmp.name
            _git(cwd, "init", "-b", "main")
            _git(cwd, "config", "user.email", "t@e.com")
            _git(cwd, "config", "user.name", "t")
            _git(cwd, "commit", "--allow-empty", "-m", "root")
            with open(os.path.join(cwd, "notes.txt"), "w") as fh:
                fh.write("hi\n")
            _git(cwd, "add", ".")
            _git(cwd, "commit", "-m", "notes")
            _git(cwd, "update-ref", "ORIG_HEAD", "HEAD~1")
            shim, marker, path = _docker_shim()
            try:
                proc = _run_hook(cwd, {"PATH": path})
                self.assertFalse(os.path.exists(marker))
            finally:
                shim.cleanup()
            self.assertEqual(proc.returncode, 0)
            self.assertNotIn("buttons changed", proc.stdout)
        finally:
            tmp.cleanup()

    def test_refuses_rebuild_inside_container(self):
        tmp = _fixture()
        shim, marker, path = _docker_shim()
        try:
            container_probe = os.path.join(tmp.name, "backend",
                                           "x.py")
            proc = _run_hook(
                tmp.name,
                {"REMOTE_BUTTONS_PROJECT_DIR": tmp.name,
                 "BUTTONS_CONTAINER_MARKER": container_probe,
                 "PATH": path})
            self.assertEqual(proc.returncode, 0)
            self.assertIn("refusing to rebuild from inside",
                          proc.stdout)
            self.assertNotIn("rebuilding buttons container",
                             proc.stdout)
            self.assertFalse(os.path.exists(marker))
        finally:
            tmp.cleanup()
            shim.cleanup()

    def test_prints_oauth2_proxy_restart_reminder(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            cwd = tmp.name
            _git(cwd, "init", "-b", "main")
            _git(cwd, "config", "user.email", "t@e.com")
            _git(cwd, "config", "user.name", "t")
            _git(cwd, "commit", "--allow-empty", "-m", "root")
            os.makedirs(os.path.join(cwd, "config"), exist_ok=True)
            with open(os.path.join(cwd, "config",
                                   "oauth2-proxy.cfg"), "w") as fh:
                fh.write("x = 1\n")
            _git(cwd, "add", ".")
            _git(cwd, "commit", "-m", "touch oauth2-proxy cfg")
            _git(cwd, "update-ref", "ORIG_HEAD", "HEAD~1")
            shim, marker, path = _docker_shim()
            try:
                proc = _run_hook(cwd, {"PATH": path})
                self.assertFalse(os.path.exists(marker))
            finally:
                shim.cleanup()
            self.assertEqual(proc.returncode, 0)
            self.assertIn("oauth2-proxy config changed",
                          proc.stdout)
            self.assertIn("restart it:", proc.stdout)
            self.assertNotIn("rebuilding buttons container",
                             proc.stdout)
        finally:
            tmp.cleanup()

    def test_restores_exec_bits_on_scripts(self):
        tmp = _fixture()
        try:
            script = os.path.join(tmp.name, "scripts", "deploy.sh")
            os.makedirs(os.path.dirname(script), exist_ok=True)
            with open(script, "w") as fh:
                fh.write("#!/bin/sh\necho hi\n")
            os.chmod(script, 0o644)
            proc = _run_hook(tmp.name)
            self.assertEqual(proc.returncode, 0)
            mode = os.stat(script).st_mode
            self.assertTrue(mode & 0o111,
                            f"{script} is not executable: {oct(mode)}")
        finally:
            tmp.cleanup()
