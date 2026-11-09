"""No shadow skill copies (issue #1207).

.agents/skills/ is the single source of truth; .claude/skills/ exists
only because some consumers read that path, so every entry there must
be a symlink into the canonical tree. scripts/check-no-shadow-skills.sh
is the CI enforcement; these tests pin the live tree and the guard's
accept/reject behavior against synthetic roots (the script derives its
root from its own path, so the synthetic roots carry a byte copy of
the real script).
Run: python3 -m unittest tests.test_no_shadow_skills
"""

import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = os.path.abspath(os.path.join("scripts", "check-no-shadow-skills.sh"))
SHIM_DIR = os.path.abspath(os.path.join(".claude", "skills"))
CANON_DIR = os.path.abspath(os.path.join(".agents", "skills"))


def _run_script(cwd):
    return subprocess.run(
        ["bash", SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=cwd,
    )


def _install_script_copy(root):
    dest_dir = os.path.join(root, "scripts")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "check-no-shadow-skills.sh")
    shutil.copyfile(SCRIPT, dest)
    os.chmod(dest, 0o755)
    return dest


def _make_root(entries):
    """Synthetic repo root: entries maps name -> kind.

    kind is "file" (real file), "dir" (real dir with a file in it),
    "link" (symlink to a sibling target), or "dangling" (symlink to
    nothing). Returns the root path.
    """
    tmp = tempfile.mkdtemp()
    shim = os.path.join(tmp, ".claude", "skills")
    os.makedirs(shim)
    for name, kind in entries.items():
        path = os.path.join(shim, name)
        if kind == "file":
            with open(path, "w", encoding="utf-8") as f:
                f.write("# shadow\n")
        elif kind == "dir":
            os.makedirs(path)
            with open(os.path.join(path, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("# shadow\n")
        elif kind == "link":
            target = os.path.join(tmp, "target-%s" % name)
            os.makedirs(target, exist_ok=True)
            os.symlink(target, path)
        elif kind == "dangling":
            os.symlink(os.path.join(tmp, "not-provisioned-%s" % name), path)
    script = _install_script_copy(tmp)
    return tmp, script


def _run_root_script(root, script):
    return subprocess.run(
        ["bash", script],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=root,
    )


class LiveTreeTest(unittest.TestCase):
    def test_no_real_files_under_claude_skills(self):
        # The pre-fix tree carried a full 52-file real copy
        # (.claude/skills/impeccable/); every entry must now be a
        # symlink into .agents/skills/. A real file or dir here is
        # the drift surface the guard exists to catch.
        if not os.path.isdir(SHIM_DIR):
            return
        for name in sorted(os.listdir(SHIM_DIR)):
            path = os.path.join(SHIM_DIR, name)
            self.assertTrue(
                os.path.islink(path),
                "shadow skill copy: %r is a real file/dir, not a symlink" % path,
            )

    def test_symlinks_point_at_canonical_tree_when_provisioned(self):
        # Dangling links pass (fresh checkouts provision
        # .agents/skills/ via scripts/provision-skills.sh), but a
        # link that resolves must land inside the canonical tree —
        # never at an unrelated copy.
        if not os.path.isdir(SHIM_DIR):
            return
        for name in sorted(os.listdir(SHIM_DIR)):
            path = os.path.join(SHIM_DIR, name)
            if not os.path.islink(path) or not os.path.exists(path):
                continue
            target = os.path.realpath(path)
            self.assertTrue(
                target == os.path.realpath(CANON_DIR)
                or target.startswith(os.path.realpath(CANON_DIR) + os.sep),
                "%r resolves outside .agents/skills/: %r" % (path, target),
            )

    def test_guard_script_passes_on_live_tree(self):
        proc = _run_script(".")
        self.assertEqual(proc.returncode, 0, proc.stderr)


class GuardBehaviorTest(unittest.TestCase):
    def _run_entries(self, entries):
        root, script = _make_root(entries)
        self.addCleanup(shutil.rmtree, root, True)
        return _run_root_script(root, script)

    def test_real_file_fails_and_names_it(self):
        proc = self._run_entries({"impeccable": "file"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("impeccable", proc.stderr)

    def test_real_dir_fails(self):
        proc = self._run_entries({"impeccable": "dir"})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("impeccable", proc.stderr)

    def test_symlink_only_passes(self):
        proc = self._run_entries({"impeccable": "link", "karpathy-guidelines": "link"})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_dangling_symlink_passes(self):
        # Fresh checkouts have no .agents/skills/ until provisioned;
        # dangling links are the expected pre-provision state.
        proc = self._run_entries({"impeccable": "dangling"})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_shim_dir_passes(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        script = _install_script_copy(root)
        proc = _run_root_script(root, script)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
