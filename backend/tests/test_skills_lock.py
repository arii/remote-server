"""Tests for skills-lock.json integrity.

Verifies that every entry in skills-lock.json is well-formed, that each
locally installed skillPath exists on disk, and that each installed
SKILL.md matches the hash pinned in the lock (issue #1160).
"""

import hashlib
import json
import os
import unittest

LOCK_PATH = "skills-lock.json"


def _load_lock():
    with open(LOCK_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class TestSkillsLock(unittest.TestCase):
    def setUp(self):
        self.lock = _load_lock()
        self.skills = self.lock.get("skills", {})

    def _installed(self):
        """Entries whose skillPath points into the local .agents/skills tree."""
        return {
            name: entry
            for name, entry in self.skills.items()
            if entry.get("skillPath", "").startswith(".agents/skills/")
        }

    def _require_provisioned(self):
        """Skip when third-party skills are not provisioned (e.g. fresh CI
        checkouts carry only tracked project skills, never the vendored
        third-party tree that provision-skills.sh installs per machine)."""
        missing = sorted(
            name
            for name, entry in self._installed().items()
            if not os.path.isfile(entry["skillPath"])
        )
        if missing:
            self.skipTest(f"skills not provisioned: {', '.join(missing)}")

    def test_lock_has_skills(self):
        self.assertIsInstance(self.lock, dict)
        self.assertTrue(self.skills, "skills-lock.json has no skills")

    def test_every_entry_has_required_fields(self):
        for name, entry in self.skills.items():
            with self.subTest(skill=name):
                self.assertIn("source", entry)
                self.assertIn("skillPath", entry)
                self.assertIn("computedHash", entry)

    def test_every_skill_path_exists(self):
        self._require_provisioned()
        for name, entry in self._installed().items():
            with self.subTest(skill=name):
                path = entry["skillPath"]
                self.assertTrue(
                    os.path.isfile(path),
                    f"missing skill file for {name}: {entry['skillPath']}",
                )

    def test_installed_skill_md_matches_lock_hash(self):
        self._require_provisioned()
        for name, entry in self._installed().items():
            with self.subTest(skill=name):
                path = entry["skillPath"]
                with open(path, "rb") as f:
                    digest = hashlib.sha256(f.read()).hexdigest()
                self.assertEqual(
                    digest,
                    entry["computedHash"],
                    f"hash mismatch for {name} ({entry['skillPath']})",
                )


if __name__ == "__main__":
    unittest.main()
