"""#1262 acceptance: save-time pins survive processes and surface at startup."""

from backend.tests.test_browser_config_contract import BrowserContractCase


BOOT_PROBE = """
import json
from contextlib import ExitStack
from unittest import mock
from fastapi.testclient import TestClient
from backend import server

with ExitStack() as stack:
    stack.enter_context(mock.patch("skills_lock.verify_skills", return_value=FIRST_PARTY))
    stack.enter_context(mock.patch.object(server.issues, "_ensure_schedule_ticker"))
    stack.enter_context(mock.patch.object(server.briefing, "warm_briefing_cache"))
    for name in ("list_failed_jobs", "list_failed_webhooks", "list_skipped_webhooks", "list_pending_approvals"):
        stack.enter_context(mock.patch.object(server.attention, name, return_value=[]))
    stack.enter_context(mock.patch.object(server.attention, "get_workflow_health", return_value={"workflows": []}))
    stack.enter_context(mock.patch.object(server.attention, "ollama_up", return_value=True))
    with TestClient(server.app) as client:
        response = client.get("/api/attention?repo=remote-server")
        assert response.status_code == 200, response.text
        notices = [item for item in response.json()["items"] if item["kind"] == "skills-verification"]
print(json.dumps(notices))
"""


class BrowserSkillIntegrityContractTest(BrowserContractCase):
    def setUp(self):
        super().setUp()
        self.skills = self.directory / "skills"
        self.name = "browser-integrity-probe"
        self.skill_file = self.skills / f"{self.name}.v1.json"
        self.environment = {
            "BROWSER_SKILLS_DIR": str(self.skills),
            "BROWSER_NOTES_FILE": str(self.directory / "notes.md"),
            "BROWSER_ACTIVITY_FILE": str(self.directory / "activity.jsonl"),
            "BROWSER_RUNS_FILE": str(self.directory / "runs.jsonl"),
            "BROWSER_TASKS_DIR": str(self.directory / "tasks"),
            "BUTTONS_STATE_DIR": str(self.directory / "state"),
            "AGENT_CONTROL_STATE_FILE": str(self.directory / "control.json"),
        }

    def save(self, version=1):
        result = self.probe(
            f"""
            import json
            from backend.skill_store import save_skill
            from backend.tests.test_skill_store import make_skill
            print(json.dumps(save_skill(make_skill(name={self.name!r}, version={version}))))
            """,
            self.environment,
        )
        self.assertNotIn("error", result)
        self.assertEqual(result["version"], version)

    def boot(self, first_party=None):
        return self.probe(
            f"FIRST_PARTY = {first_party or []!r}\n" + BOOT_PROBE,
            self.environment,
        )

    def assert_browser_warning(self, notices):
        self.assertTrue(
            notices,
            "startup must publish browser skill verification failures to attention",
        )
        details = " ".join(item["title"] + " " + item["detail"] for item in notices)
        self.assertIn(self.name, details)
        self.assertTrue(any(item["severity"] in ("warn", "crit") for item in notices))

    def test_one_byte_change_before_first_boot_is_visible(self):
        """#1262: pin at save, not by trusting the first startup's bytes."""
        self.save()
        self.skill_file.write_bytes(self.skill_file.read_bytes() + b" ")
        self.assert_browser_warning(self.boot())

    def test_pins_survive_a_clean_restart(self):
        """#1262: a later process detects tampering after a clean boot."""
        self.save()
        self.assertEqual(self.boot(), [])
        self.skill_file.write_bytes(self.skill_file.read_bytes() + b" ")
        self.assert_browser_warning(self.boot())

    def test_restoring_pinned_bytes_clears_the_warning(self):
        """#1262: clean recovery removes the visible verification failure."""
        self.save()
        original = self.skill_file.read_bytes()
        self.skill_file.write_bytes(original + b" ")
        self.assert_browser_warning(self.boot())
        self.skill_file.write_bytes(original)
        self.assertEqual(self.boot(), [])

    def test_saving_a_new_version_does_not_bless_old_tampering(self):
        """#1262: saving v2 must not silently re-pin a modified v1 file."""
        self.save()
        self.skill_file.write_bytes(self.skill_file.read_bytes() + b" ")
        self.save(version=2)
        self.assert_browser_warning(self.boot())

    def test_missing_pinned_skill_is_visible(self):
        """#1262: a pinned file disappearing is a verification failure."""
        self.save()
        self.skill_file.unlink()
        self.assert_browser_warning(self.boot())

    def test_browser_failure_preserves_first_party_warning(self):
        """#1262: extend the existing warning path without losing #1120 failures."""
        self.save()
        self.skill_file.write_bytes(self.skill_file.read_bytes() + b" ")
        notices = self.boot(first_party=["first-party sentinel: hash mismatch"])
        self.assert_browser_warning(notices)
        self.assertIn(
            "first-party sentinel", " ".join(item["detail"] for item in notices)
        )

    def test_empty_store_and_untampered_saved_versions_are_clean(self):
        """#1262 control: empty and valid stores produce no false warning."""
        self.assertEqual(self.boot(), [])
        self.save()
        self.assertEqual(self.boot(), [])
        self.save(version=2)
        self.assertEqual(self.boot(), [])
