"""Shadow-skills guard covers the .github/skills/ chain (#1214, #1215).

.github/skills/ exists because some consumers read that path, so every
entry under it — like .claude/skills/ — must be a symlink into the
canonical tree. scripts/check-no-shadow-skills.sh is the CI
enforcement; these tests pin the guard's accept/reject behavior for
the .github chain against synthetic roots (the script derives its root
from its own path, so each synthetic root carries a byte copy of the
real script), plus the live tree.

Companion to tests/test_no_shadow_skills.py, which pins the
.claude/skills/ chain (issue #1207).

Run: python3 -m unittest tests.test_no_shadow_skills_github
"""

import os
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = os.path.abspath(os.path.join("scripts", "check-no-shadow-skills.sh"))
GITHUB_SHIM = os.path.abspath(os.path.join(".github", "skills"))


def _install_script_copy(root):
    dest_dir = os.path.join(root, "scripts")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "check-no-shadow-skills.sh")
    shutil.copyfile(SCRIPT, dest)
    os.chmod(dest, 0o755)
    return dest


def _make_root(shim_entries):
    """Synthetic repo root.

    shim_entries maps shim relpath (".claude/skills" or
    ".github/skills") to {name: kind}; kind is "file" (real file),
    "dir" (real dir with a file in it), "link" (live symlink), or
    "dangling" (symlink to nothing). Absent shim paths are not
    created. Returns the root path.
    """
    tmp = tempfile.mkdtemp()
    for shim_rel, entries in shim_entries.items():
        shim = os.path.join(tmp, *shim_rel.split("/"))
        os.makedirs(shim)
        for name, kind in entries.items():
            path = os.path.join(shim, name)
            if kind == "file":
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("# shadow\n")
            elif kind == "dir":
                os.makedirs(path)
                with open(
                    os.path.join(path, "SKILL.md"), "w", encoding="utf-8"
                ) as handle:
                    handle.write("# shadow\n")
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


GITHUB = ".github/skills"
CLAUDE = ".claude/skills"


class GithubChainTest(unittest.TestCase):
    def _run(self, shim_entries):
        root, script = _make_root(shim_entries)
        self.addCleanup(shutil.rmtree, root, True)
        return _run_root_script(root, script)

    def test_real_file_under_github_skills_fails(self):
        proc = self._run({GITHUB: {"impeccable": "file"}})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("impeccable", proc.stderr)

    def test_real_dir_under_github_skills_fails(self):
        proc = self._run({GITHUB: {"impeccable": "dir"}})
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("impeccable", proc.stderr)

    def test_symlink_under_github_skills_passes(self):
        proc = self._run(
            {GITHUB: {"impeccable": "link", "karpathy-guidelines": "link"}}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_dangling_symlink_under_github_skills_passes(self):
        # Fresh checkouts have no canonical tree until provisioned;
        # dangling links are the expected pre-provision state.
        proc = self._run({GITHUB: {"impeccable": "dangling"}})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_missing_github_shim_dir_passes(self):
        proc = self._run({CLAUDE: {"impeccable": "link"}})
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_dirty_github_clean_claude_fails(self):
        # The chain coverage that was missing: a shadow copy under
        # .github/skills/ must fail even when .claude/skills/ is clean.
        proc = self._run(
            {CLAUDE: {"impeccable": "link"}, GITHUB: {"impeccable": "file"}}
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("impeccable", proc.stderr)

    def test_dirty_claude_clean_github_fails(self):
        proc = self._run(
            {CLAUDE: {"impeccable": "file"}, GITHUB: {"impeccable": "link"}}
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("impeccable", proc.stderr)

    def test_both_chains_clean_pass(self):
        proc = self._run(
            {CLAUDE: {"impeccable": "link"}, GITHUB: {"impeccable": "link"}}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


class GithubLiveTreeTest(unittest.TestCase):
    def test_no_real_files_under_github_skills(self):
        if not os.path.isdir(GITHUB_SHIM):
            return
        for name in sorted(os.listdir(GITHUB_SHIM)):
            path = os.path.join(GITHUB_SHIM, name)
            self.assertTrue(
                os.path.islink(path),
                "shadow skill copy: %r is a real file/dir, not a symlink" % path,
            )

    def test_guard_script_passes_on_live_tree(self):
        proc = subprocess.run(
            ["bash", SCRIPT],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=".",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
