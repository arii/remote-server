"""Verify immutable skill refs with real Git history (#1322)."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path("scripts/verify-skills.sh").resolve()


class PinnedUpstreamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "upstream"
        self.git = shutil.which("git")
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Skill test",
            "GIT_AUTHOR_EMAIL": "skill-test@example.test",
            "GIT_COMMITTER_NAME": "Skill test",
            "GIT_COMMITTER_EMAIL": "skill-test@example.test",
        }
        self._git("init", "-q", str(self.repo))
        self.files = {"alpha": b"# alpha\n", "beta": b"# beta\n"}
        for name, data in self.files.items():
            path = self.repo / "skills" / name / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(data)
        self.paths = [f"skills/{name}/SKILL.md" for name in self.files]
        self._git("-C", str(self.repo), "add", *self.paths)
        self._git("-C", str(self.repo), "commit", "-qm", "locked skills")
        self.pin = self._git("-C", str(self.repo), "rev-parse", "HEAD").strip()
        (self.repo / self.paths[0]).unlink()
        (self.repo / self.paths[1]).write_bytes(b"# changed beta\n")
        self._git("-C", str(self.repo), "add", *self.paths)
        self._git("-C", str(self.repo), "commit", "-qm", "move default branch")
        self.head = self._git("-C", str(self.repo), "rev-parse", "HEAD").strip()
        self.lock = self.root / "lock.json"
        self.log = self.root / "git.jsonl"
        bindir = self.root / "bin"
        bindir.mkdir()
        wrapper = bindir / "git"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "args = sys.argv[1:]\n"
            "with open(os.environ['SKILL_TEST_LOG'], 'a') as log:\n"
            "    log.write(json.dumps(args) + '\\n')\n"
            "args = [os.environ['SKILL_TEST_REPO'] if arg == "
            "'https://github.com/example/skills' else arg for arg in args]\n"
            "os.execv(os.environ['SKILL_TEST_GIT'], ['git', *args])\n"
        )
        wrapper.chmod(0o755)
        self.env.update(
            PATH=f"{bindir}:{self.env['PATH']}",
            SKILL_TEST_LOG=str(self.log),
            SKILL_TEST_REPO=str(self.repo),
            SKILL_TEST_GIT=self.git,
        )

    def _git(self, *args):
        return subprocess.check_output(
            [self.git, *args], env=self.env, text=True, stderr=subprocess.PIPE
        )

    def _entries(self):
        return {
            name: {
                "source": "example/skills",
                "sourceType": "github",
                "skillPath": f"skills/{name}/SKILL.md",
                "computedHash": hashlib.sha256(data).hexdigest(),
                "ref": self.pin,
            }
            for name, data in self.files.items()
        }

    def _run(self, entries):
        self.lock.write_text(json.dumps({"version": 1, "skills": entries}))
        self.log.unlink(missing_ok=True)
        return subprocess.run(
            ["bash", str(SCRIPT), "--lock", str(self.lock), "--check-upstream"],
            env=self.env,
            capture_output=True,
            text=True,
        )

    def _fetches(self):
        if not self.log.exists():
            return []
        return [
            args
            for line in self.log.read_text().splitlines()
            if "fetch" in (args := json.loads(line))
        ]

    def test_pinned_content_survives_changed_and_deleted_default_paths(self):
        entries = self._entries()
        result = self._run(entries)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all 2 skill(s)", result.stdout)
        self.assertEqual(len(self._fetches()), 1)
        self.assertEqual(self._fetches()[0][-1], self.pin)
        self.assertEqual(json.loads(self.lock.read_text())["skills"], entries)

    def test_same_source_can_have_distinct_pinned_commits(self):
        entries = self._entries()
        entries["beta"]["ref"] = self.head
        entries["beta"]["computedHash"] = hashlib.sha256(
            b"# changed beta\n"
        ).hexdigest()
        result = self._run(entries)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([args[-1] for args in self._fetches()], [self.pin, self.head])

    def test_empty_lock(self):
        result = self._run({})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("all 0 skill(s)", result.stdout)
        self.assertEqual(self._fetches(), [])

    def test_missing_or_mutable_ref_is_rejected_without_fetch(self):
        for ref in (None, "main", self.pin[:7]):
            with self.subTest(ref=ref):
                entry = self._entries()["alpha"]
                if ref is None:
                    del entry["ref"]
                else:
                    entry["ref"] = ref
                result = self._run({"alpha": entry})
                self.assertEqual(result.returncode, 1)
                self.assertIn("UNPINNED alpha", result.stderr)
                self.assertEqual(self._fetches(), [])

    def test_missing_commit_fails_without_default_branch_fallback(self):
        entry = self._entries()["alpha"]
        entry["ref"] = "f" * 40
        result = self._run({"alpha": entry})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self._fetches()), 1)
        self.assertEqual(self._fetches()[0][-1], entry["ref"])

    def test_missing_path_at_pin_fails(self):
        entry = self._entries()["alpha"]
        entry["skillPath"] = "skills/missing/SKILL.md"
        result = self._run({"alpha": entry})
        self.assertEqual(result.returncode, 1)
        self.assertIn("NO-UPSTREAM-PATH alpha", result.stderr)

    def test_wrong_hash_fails_without_rewriting_lock(self):
        entry = self._entries()["alpha"]
        entry["computedHash"] = "0" * 64
        result = self._run({"alpha": entry})
        self.assertEqual(result.returncode, 1)
        self.assertIn("UPSTREAM-MISMATCH alpha", result.stderr)
        self.assertEqual(json.loads(self.lock.read_text())["skills"]["alpha"], entry)
