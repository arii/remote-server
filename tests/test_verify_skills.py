"""Pin the skills-lock verifier (issue #557).

skills-lock.json pins third-party skill hashes but `muse skills` implements
no lock check, so scripts/verify-skills.sh is the enforcement path. These
tests drive the real script against a synthetic lock + temp skills dir (the
real .agents/skills is untracked and absent from fresh checkouts).
"""
import hashlib
import json
import os
import subprocess
import tempfile
import unittest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "scripts", "verify-skills.sh")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


class VerifySkillsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.skills = os.path.join(self.tmp.name, "skills")
        os.makedirs(self.skills)
        self.lock_path = os.path.join(self.tmp.name, "lock.json")
        self.files = {
            "alpha-skill": b"# alpha\n",
            "beta-skill": b"# beta\n",
        }
        self._install_all()
        self._write_lock()

    def tearDown(self):
        self.tmp.cleanup()

    def _install_all(self):
        for name, data in self.files.items():
            path = os.path.join(self.skills, name)
            os.makedirs(path, exist_ok=True)
            with open(os.path.join(path, "SKILL.md"), "wb") as f:
                f.write(data)

    def _write_lock(self, hashes=None):
        lock = {"version": 1, "skills": {}}
        for name, data in self.files.items():
            digest = hashes[name] if hashes else _sha256(data)
            lock["skills"][name] = {
                "source": "example/example",
                "sourceType": "github",
                "skillPath": "skills/%s/SKILL.md" % name,
                "computedHash": digest,
            }
        with open(self.lock_path, "w") as f:
            json.dump(lock, f, indent=2)
            f.write("\n")

    def _run(self, *args):
        return subprocess.run(
            ["bash", SCRIPT, "--lock", self.lock_path,
             "--skills-dir", self.skills, *args],
            capture_output=True, text=True,
        )

    def test_verify_ok(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("OK (2 locked skill(s) match)", proc.stdout)

    def test_tampered_skill_fails(self):
        with open(os.path.join(self.skills, "alpha-skill", "SKILL.md"), "ab") as f:
            f.write(b"<!-- injected -->\n")
        proc = self._run()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MISMATCH alpha-skill", proc.stderr)
        self.assertNotIn("MISMATCH beta-skill", proc.stderr)

    def test_missing_skill_fails(self):
        os.remove(os.path.join(self.skills, "beta-skill", "SKILL.md"))
        proc = self._run()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MISSING  beta-skill", proc.stderr)

    def test_unknown_skill_fails(self):
        rogue = os.path.join(self.skills, "rogue-skill")
        os.makedirs(rogue)
        with open(os.path.join(rogue, "SKILL.md"), "w") as f:
            f.write("# rogue\n")
        proc = self._run()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("UNKNOWN  rogue-skill", proc.stderr)

    def test_git_tracked_project_skill_passes(self):
        proc = subprocess.run(
            ["git", "-C", ROOT, "ls-files", ".agents/skills/*/SKILL.md"],
            capture_output=True, text=True,
        )
        tracked = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        if not tracked:
            self.skipTest("no git-tracked project skills in this checkout")
        name = tracked[0].split("/")[2]
        if name in self.files:
            self.skipTest("synthetic name collides with tracked skill")
        project = os.path.join(self.skills, name)
        os.makedirs(project)
        with open(os.path.join(project, "SKILL.md"), "w") as f:
            f.write("# project skill\n")
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("TRACKED  %s" % name, proc.stderr)

    def test_repin_updates_lock_then_verifies(self):
        with open(os.path.join(self.skills, "alpha-skill", "SKILL.md"), "ab") as f:
            f.write(b"v2\n")
        repin = self._run("--repin")
        self.assertEqual(repin.returncode, 0, repin.stderr)
        with open(self.lock_path) as f:
            lock = json.load(f)
        with open(os.path.join(self.skills, "alpha-skill", "SKILL.md"), "rb") as f:
            self.assertEqual(
                lock["skills"]["alpha-skill"]["computedHash"], _sha256(f.read()))
        self.assertEqual(self._run().returncode, 0)

    def test_repin_missing_skill_fails(self):
        os.remove(os.path.join(self.skills, "alpha-skill", "SKILL.md"))
        proc = self._run("--repin")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MISSING  alpha-skill", proc.stderr)

    def test_corrupt_lock_fails(self):
        with open(self.lock_path, "w") as f:
            f.write("{not json\n")
        proc = self._run()
        self.assertNotEqual(proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()
