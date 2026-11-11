"""Acceptance tests for the skill store API (issue arii/remote-server#1257).

Contract source: the issue text plus the delegated contract only. These
tests pin public interfaces, not implementation internals:

- module functions ``from backend.skill_store import
  list_skills, read_skill, save_skill``
- HTTP routes on a bare FastAPI app including
  ``backend.api.routers.browser_skills.router``

Storage is always redirected to a tmp dir via ``BROWSER_SKILLS_DIR``
(os.environ + addCleanup); the real path is never touched.
"""

import copy
import os
import tempfile
import unittest


def make_skill(
    name="fill-contact-form", version=1, source_url="https://example.com/form"
):
    """Build a valid skill in the frozen format (prior art, issue #1253)."""
    return {
        "skill": name,
        "version": version,
        "source_url": source_url,
        "steps": [
            {
                "n": 1,
                "action": "type",
                "selector": "[data-testid='name']",
                "value": "Ariel Anders",
                "check": {
                    "type": "value_equals",
                    "expected": "Ariel Anders",
                },
            },
            {
                "n": 2,
                "action": "click",
                "selector": "[data-testid='submit']",
                "check": {
                    "type": "url_contains",
                    "expected": "confirmation",
                },
            },
        ],
    }


def _tmp_files(tmpdir):
    found = []
    for root, _dirs, files in os.walk(tmpdir):
        for name in files:
            found.append(os.path.join(root, name))
    return found


class SkillStoreModuleBase(unittest.TestCase):
    """Redirect BROWSER_SKILLS_DIR to a tmp dir for every module test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        old = os.environ.get("BROWSER_SKILLS_DIR")
        os.environ["BROWSER_SKILLS_DIR"] = tmp.name

        def _restore():
            if old is None:
                os.environ.pop("BROWSER_SKILLS_DIR", None)
            else:
                os.environ["BROWSER_SKILLS_DIR"] = old

        self.addCleanup(_restore)
        # Re-import after setting the env so import-time path capture
        # (if any) observes the tmp dir, not the real path.
        import importlib

        import backend.skill_store as store_mod

        importlib.reload(store_mod)
        from backend.skill_store import list_skills, read_skill, save_skill

        self.list_skills = list_skills
        self.read_skill = read_skill
        self.save_skill = save_skill


class SaveLoadContractTest(SkillStoreModuleBase):
    def test_save_then_load_identical(self):
        """Contract acceptance: saving then loading returns the identical skill."""
        skill = make_skill()
        result = self.save_skill(copy.deepcopy(skill))
        self.assertNotIn("error", result)
        self.assertEqual(result.get("skill"), skill["skill"])
        self.assertEqual(result.get("version"), skill["version"])
        loaded = self.read_skill(skill["skill"])
        self.assertEqual(loaded, skill)

    def test_list_shows_latest_versions(self):
        """Contract: list_skills() returns [{"skill", "version"}] at latest versions."""
        alpha_v1 = make_skill(name="alpha-skill", version=1)
        beta_v1 = make_skill(name="beta-skill", version=1)
        self.save_skill(copy.deepcopy(alpha_v1))
        self.save_skill(copy.deepcopy(beta_v1))
        alpha_v2 = make_skill(
            name="alpha-skill", version=2, source_url="https://example.com/form-v2"
        )
        self.save_skill(copy.deepcopy(alpha_v2))
        rows = self.list_skills()
        by_name = {row["skill"]: row["version"] for row in rows}
        self.assertEqual(by_name.get("alpha-skill"), 2)
        self.assertEqual(by_name.get("beta-skill"), 1)

    def test_invalid_skill_refused_with_no_file(self):
        """Contract: frozen validator decides validity; invalid skill rejected, nothing written."""
        result = self.save_skill({})
        self.assertIn("error", result)
        self.assertEqual(_tmp_files(self.tmpdir), [])
        self.assertEqual(self.list_skills(), [])

    def test_traversal_name_rejected(self):
        """Contract: names are filenames [A-Za-z0-9_-]; anything else rejected, no traversal."""
        evil = make_skill(name="../evil", version=1)
        result = self.save_skill(copy.deepcopy(evil))
        self.assertIn("error", result)
        self.assertEqual(_tmp_files(self.tmpdir), [])
        self.assertIsNone(self.read_skill("../evil"))

    def test_downgrade_refused(self):
        """Contract: versions only move forward; older or equal version refused."""
        v1 = make_skill(name="downgrade-skill", version=1)
        v2 = make_skill(
            name="downgrade-skill", version=2, source_url="https://example.com/form-v2"
        )
        self.save_skill(copy.deepcopy(v1))
        self.save_skill(copy.deepcopy(v2))
        # Older version refused.
        refused_old = self.save_skill(copy.deepcopy(v1))
        self.assertIn("error", refused_old)
        # Equal version refused.
        v2_other = make_skill(
            name="downgrade-skill", version=2, source_url="https://example.com/other"
        )
        refused_same = self.save_skill(copy.deepcopy(v2_other))
        self.assertIn("error", refused_same)
        # Latest still wins.
        self.assertEqual(self.read_skill("downgrade-skill"), v2)

    def test_read_missing_returns_none(self):
        """Contract: read_skill(name) returns the skill dict or None."""
        self.assertIsNone(self.read_skill("no-such-skill-xyz"))

    def test_latest_wins_after_v1_v2(self):
        """Contract: reads serve the latest version."""
        v1 = make_skill(name="latest-skill", version=1)
        v2 = make_skill(
            name="latest-skill", version=2, source_url="https://example.com/form-v2"
        )
        self.save_skill(copy.deepcopy(v1))
        self.save_skill(copy.deepcopy(v2))
        self.assertEqual(self.read_skill("latest-skill"), v2)


class BrowserSkillsHttpBase(unittest.TestCase):
    """Serve browser_skills.router on a bare FastAPI app with tmp-dir storage."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        old = os.environ.get("BROWSER_SKILLS_DIR")
        os.environ["BROWSER_SKILLS_DIR"] = tmp.name

        def _restore():
            if old is None:
                os.environ.pop("BROWSER_SKILLS_DIR", None)
            else:
                os.environ["BROWSER_SKILLS_DIR"] = old

        self.addCleanup(_restore)
        import importlib

        import backend.skill_store as store_mod

        importlib.reload(store_mod)
        import backend.api.routers.browser_skills as skills_mod

        importlib.reload(skills_mod)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(skills_mod.router)
        self.client = TestClient(app, raise_server_exceptions=False)


class BrowserSkillsHttpTest(BrowserSkillsHttpBase):
    def test_post_then_get_identical(self):
        """Contract: POST saves, GET reads back; saving then loading is identical."""
        skill = make_skill(name="http-skill", version=1)
        resp = self.client.post(
            "/api/browser/skills/", json={"skill": copy.deepcopy(skill)}
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(body.get("skill"), skill["skill"])
        self.assertEqual(body.get("version"), skill["version"])
        got = self.client.get(f"/api/browser/skills/{skill['skill']}")
        self.assertEqual(got.status_code, 200)
        got_body = got.json()
        self.assertTrue(got_body.get("ok"))
        self.assertEqual(got_body.get("skill"), skill)
        listed = self.client.get("/api/browser/skills/")
        self.assertEqual(listed.status_code, 200)
        list_body = listed.json()
        self.assertTrue(list_body.get("ok"))
        self.assertIn(
            {"skill": skill["skill"], "version": skill["version"]},
            list_body.get("skills"),
        )

    def test_get_missing_404(self):
        """Contract: GET /api/browser/skills/{name} returns 404 when missing."""
        resp = self.client.get("/api/browser/skills/no-such-skill-xyz")
        self.assertEqual(resp.status_code, 404)

    def test_post_invalid_400(self):
        """Contract: POST invalid skill rejected by frozen validator with 400."""
        resp = self.client.post("/api/browser/skills/", json={"skill": {}})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(_tmp_files(self.tmpdir), [])

    def test_post_traversal_400(self):
        """Contract: POST traversal name rejected with 400; nothing written."""
        evil = make_skill(name="../evil", version=1)
        resp = self.client.post(
            "/api/browser/skills/", json={"skill": copy.deepcopy(evil)}
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(_tmp_files(self.tmpdir), [])


if __name__ == "__main__":
    unittest.main()
