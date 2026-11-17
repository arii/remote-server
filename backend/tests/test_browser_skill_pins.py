import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import skill_store
from backend.tests.test_skill_replay import make_three_step_skill


class BrowserSkillPinsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = mock.patch.dict(os.environ, {"BROWSER_SKILLS_DIR": str(self.root)})
        env.start()
        self.addCleanup(env.stop)

    def test_unpinned_file_is_not_blessed_by_a_save(self):
        (self.root / "legacy.v1.json").write_text("{}")
        self.assertNotIn("error", skill_store.save_skill(make_three_step_skill()))
        self.assertTrue(
            any(
                "legacy.v1.json" in line and "not pinned" in line
                for line in skill_store.verify_skills()
            )
        )

    def test_bad_lock_is_visible_and_cannot_be_overwritten(self):
        lock = self.root / "skills-lock.json"
        for contents in (
            "not json",
            "[]",
            '{"skills": []}',
            '{"skills": {"../escape": {"computedHash": "abc"}}}',
        ):
            with self.subTest(contents=contents):
                lock.write_text(contents)
                self.assertTrue(skill_store.verify_skills())
                self.assertIn("error", skill_store.save_skill(make_three_step_skill()))
                self.assertEqual(lock.read_text(), contents)
                self.assertFalse(list(self.root.glob("*.v*.json")))

    def test_failed_pin_write_rolls_back_new_version(self):
        skill = make_three_step_skill()
        self.assertNotIn("error", skill_store.save_skill(skill))
        lock = self.root / "skills-lock.json"
        original = lock.read_bytes()
        skill["version"] = 2
        with mock.patch.object(
            skill_store, "_write_pins", side_effect=OSError("disk full")
        ):
            self.assertIn("disk full", skill_store.save_skill(skill)["error"])
        self.assertEqual(lock.read_bytes(), original)
        self.assertFalse((self.root / f"{skill['skill']}.v2.json").exists())
        self.assertEqual(skill_store.verify_skills(), [])
