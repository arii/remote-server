"""First-party skill pin verification (issue #1120). Stdlib unittest only."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "deep-agent"))

import skills_lock

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_SKILLS = REPO_ROOT / "deep-agent" / "skills"
REPO_LOCK = REPO_ROOT / "deep-agent" / "skills-lock.json"

try:
    from backend.server import app as _app, _lifespan

    HAS_SERVER = True
except ImportError:
    HAS_SERVER = False


def _write_skill(directory, name, body="# Skill\n"):
    path = Path(directory) / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: Test skill.\ntrigger_nodes:\n  - node_a\n---\n{body}",
        encoding="utf-8",
    )
    return path


def _locked_tree():
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    skills = root / "skills"
    skills.mkdir()
    for name in ("alpha", "beta"):
        _write_skill(skills, name)
    lock_path = root / "skills-lock.json"
    lock_path.write_text(
        json.dumps({"version": 1, "skills": {}}, indent=2), encoding="utf-8"
    )
    skills_lock.refresh_lock(skills_dir=skills, lock_path=lock_path)
    return tmp, skills, lock_path


class VerifySkillsTest(unittest.TestCase):
    def test_clean_tree_verifies(self):
        tmp, skills, lock = _locked_tree()
        try:
            self.assertEqual(skills_lock.verify_skills(skills, lock), [])
        finally:
            tmp.cleanup()

    def test_tampered_skill_detected(self):
        tmp, skills, lock = _locked_tree()
        try:
            (skills / "alpha.md").write_text("tampered", encoding="utf-8")
            problems = skills_lock.verify_skills(skills, lock)
            self.assertEqual(len(problems), 1)
            self.assertIn("alpha", problems[0])
            self.assertIn("mismatch", problems[0])
        finally:
            tmp.cleanup()

    def test_missing_skill_detected(self):
        tmp, skills, lock = _locked_tree()
        try:
            (skills / "beta.md").unlink()
            problems = skills_lock.verify_skills(skills, lock)
            self.assertEqual(len(problems), 1)
            self.assertIn("beta", problems[0])
            self.assertIn("missing", problems[0])
        finally:
            tmp.cleanup()

    def test_unpinned_skill_detected(self):
        tmp, skills, lock = _locked_tree()
        try:
            _write_skill(skills, "gamma")
            problems = skills_lock.verify_skills(skills, lock)
            self.assertEqual(len(problems), 1)
            self.assertIn("gamma", problems[0])
            self.assertIn("not pinned", problems[0])
        finally:
            tmp.cleanup()

    def test_missing_lock_is_a_problem(self):
        tmp, skills, _lock = _locked_tree()
        try:
            problems = skills_lock.verify_skills(skills, skills / "nope.json")
            self.assertEqual(len(problems), 1)
            self.assertIn("lock missing", problems[0])
        finally:
            tmp.cleanup()

    def test_refresh_repin_heals_tamper(self):
        tmp, skills, lock = _locked_tree()
        try:
            (skills / "alpha.md").write_text("tampered", encoding="utf-8")
            self.assertEqual(len(skills_lock.verify_skills(skills, lock)), 1)
            skills_lock.refresh_lock(skills_dir=skills, lock_path=lock)
            self.assertEqual(skills_lock.verify_skills(skills, lock), [])
        finally:
            tmp.cleanup()


class RepoLockTest(unittest.TestCase):
    def test_lock_pins_exactly_the_shipped_skills(self):
        lock = skills_lock.load_lock(REPO_LOCK)
        self.assertEqual(
            sorted(lock["skills"]),
            ["architectural_audit_rubric", "co_browsing_state_takeover",
             "cobrowse_handoff_specialist", "coder_root_cause_analysis",
             "dispatch_brief", "repo_review", "shared_browser_access",
             "write_spec_issue"],
        )

    def test_lock_matches_tree(self):
        # CI enforcement: the committed pins must match the files or
        # the lock is stale and must be re-pinned in the same PR.
        self.assertEqual(
            skills_lock.verify_skills(REPO_SKILLS, REPO_LOCK), [])


@unittest.skipUnless(HAS_SERVER, "fastapi not installed")
class LifespanWiringTest(unittest.TestCase):
    def _run_lifespan(self):
        import asyncio

        async def go():
            async with _lifespan(_app):
                return list(getattr(_app.state, "skills_verification_errors", None))

        return asyncio.run(go())

    def test_startup_records_clean_verification(self):
        self.assertEqual(self._run_lifespan(), [])

    def test_startup_records_problems_without_raising(self):
        import skills_lock as live

        real = live.verify_skills
        live.verify_skills = lambda *a, **k: ["skill 'x': hash mismatch"]
        try:
            self.assertEqual(
                self._run_lifespan(), ["skill 'x': hash mismatch"])
        finally:
            live.verify_skills = real


if __name__ == "__main__":
    unittest.main()
