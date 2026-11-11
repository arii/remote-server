"""Acceptance tests for persistent run history (issue arii/remote-server#1258).

Contract source: the issue text only. These tests pin the public
interfaces, not implementation internals:

- ``from backend.skill_runs import run_and_record, list_runs``
- HTTP ``GET /api/browser/skills/{name}/runs`` -> ``{"ok", "runs"}``
  on a bare FastAPI app including the router.

Authored without reading ``backend/skill_runs.py`` or
``backend/api/routers/browser_skills.py``; ``backend/skill_replay.py``
(run_skill), ``backend/skill_format.py``, and ``backend/skill_check.py``
are merged prior art used only for fixture guards.
"""

import copy
import importlib
import json
import os
import tempfile
import unittest

FORM_URL = "https://example.com/form"
CONF_URL = "https://example.com/confirmation"


def make_form_elements(submit_tid="submit", submit_id="submit-btn"):
    """Canned form page elements (drive snapshot shape)."""
    return [
        {
            "tag": "INPUT",
            "text": "",
            "x": 100,
            "y": 200,
            "w": 200,
            "h": 30,
            "tid": "name",
            "id": "name-input",
            "name": "full-name",
            "role": "textbox",
            "cls": "field",
            "val": "",
        },
        {
            "tag": "BUTTON",
            "text": "Submit",
            "x": 100,
            "y": 300,
            "w": 80,
            "h": 30,
            "tid": submit_tid,
            "id": submit_id,
            "name": "submit-btn",
            "role": "button",
            "cls": "btn",
        },
    ]


def make_confirmation_page():
    return {
        "url": CONF_URL,
        "title": "Confirmation",
        "elements": [
            {
                "tag": "DIV",
                "text": "Thank you Ariel Anders",
                "x": 10,
                "y": 10,
                "w": 200,
                "h": 30,
                "tid": "thanks",
            },
        ],
    }


def make_good_pages():
    return {
        FORM_URL: {
            "url": FORM_URL,
            "title": "Contact form",
            "elements": make_form_elements(),
        },
        CONF_URL: make_confirmation_page(),
    }


def make_changed_layout_pages():
    """Same form page with the submit target renamed, so the recorded
    ``[data-testid='submit']`` selector no longer resolves."""
    return {
        FORM_URL: {
            "url": FORM_URL,
            "title": "Contact form",
            "elements": make_form_elements(
                submit_tid="submit-renamed", submit_id="submit-btn-v2"
            ),
        },
        CONF_URL: make_confirmation_page(),
    }


class FakeDriver:
    """Scripted driver speaking the contract's structural protocol.

    Scripted pages move form -> confirmation only when a submit-like
    button itself is clicked (coordinate-checked), so a click that
    resolves to nothing never advances the page. Every call, snapshot
    included, is recorded in ``actions``.
    """

    SUBMIT_TIDS = ("submit", "submit-renamed")
    SUBMIT_IDS = ("submit-btn", "submit-btn-v2")

    def __init__(self, pages, start_url=FORM_URL):
        self._pages = copy.deepcopy(pages)
        if start_url not in self._pages:
            self._pages[start_url] = {"url": start_url, "title": "", "elements": []}
        self._current = start_url
        self.actions = []

    def snapshot(self):
        self.actions.append("snapshot")
        return copy.deepcopy(self._pages[self._current])

    def navigate(self, url):
        self.actions.append(f"navigate:{url}")
        if url not in self._pages:
            self._pages[url] = {"url": url, "title": "", "elements": []}
        self._current = url

    @staticmethod
    def _inside(x, y, el):
        try:
            fx, fy = float(x), float(y)
        except (TypeError, ValueError):
            return False
        ex, ey = float(el.get("x", 0)), float(el.get("y", 0))
        ew, eh = float(el.get("w", 0)), float(el.get("h", 0))
        return ex <= fx <= ex + ew and ey <= fy <= ey + eh

    def click(self, x, y):
        self.actions.append(f"click:{x},{y}")
        page = self._pages[self._current]
        for el in page.get("elements", []) or []:
            if not isinstance(el, dict):
                continue
            if el.get("tid") in self.SUBMIT_TIDS or el.get("id") in self.SUBMIT_IDS:
                if self._inside(x, y, el):
                    self._current = CONF_URL
                    return

    def type_text(self, text):
        self.actions.append(f"type_text:{text}")
        page = self._pages[self._current]
        for el in page.get("elements", []) or []:
            if isinstance(el, dict) and el.get("tid") == "name":
                el["val"] = text
                return

    def key(self, key):
        self.actions.append(f"key:{key}")

    def wheel(self, x, y, dx, dy):
        self.actions.append(f"wheel:{x},{y},{dx},{dy}")


def make_v1_skill(name="history-probe-form", version=1):
    """Three-step skill: navigate, click submit, confirm.

    Step 2 is the layout-sensitive step: it resolves on the good
    pages and misses on the relayout pages.
    """
    return {
        "skill": name,
        "version": version,
        "source_url": FORM_URL,
        "steps": [
            {
                "n": 1,
                "action": "navigate",
                "selector": FORM_URL,
                "check": {"type": "url_contains", "expected": "example.com"},
            },
            {
                "n": 2,
                "action": "click",
                "selector": "[data-testid='submit']",
                "check": {"type": "url_contains", "expected": "confirmation"},
            },
            {
                "n": 3,
                "action": "navigate",
                "selector": CONF_URL,
                "check": {"type": "url_contains", "expected": "confirmation"},
            },
        ],
    }


def make_v2_skill(name="history-probe-form"):
    """Corrected skill (v2): step 2 tracks the renamed submit target."""
    skill = make_v1_skill(name=name, version=2)
    skill["steps"][1] = dict(skill["steps"][1])
    skill["steps"][1]["selector"] = "[data-testid='submit-renamed']"
    return skill


def make_invalid_skill(name="history-probe-form", version=1):
    """Same shape as v1 but with an unknown action, which the frozen
    format rejects."""
    skill = make_v1_skill(name=name, version=version)
    skill["steps"][1] = dict(skill["steps"][1])
    skill["steps"][1]["action"] = "hover"
    return skill


class RunsModuleBase(unittest.TestCase):
    """Redirect BROWSER_RUNS_FILE to a tmp dir for every module test."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmpdir = tmp.name
        self.runs_path = os.path.join(tmp.name, "runs.jsonl")
        old = os.environ.get("BROWSER_RUNS_FILE")
        os.environ["BROWSER_RUNS_FILE"] = self.runs_path

        def _restore():
            if old is None:
                os.environ.pop("BROWSER_RUNS_FILE", None)
            else:
                os.environ["BROWSER_RUNS_FILE"] = old

        self.addCleanup(_restore)
        import backend.skill_runs as runs_mod

        importlib.reload(runs_mod)
        from backend.skill_runs import list_runs, run_and_record

        self.run_and_record = run_and_record
        self.list_runs = list_runs

    def _jsonl_lines(self):
        with open(os.environ["BROWSER_RUNS_FILE"], encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


class ThreeRunSequenceTest(RunsModuleBase):
    def test_three_runs_record_numbers_1_2_3_with_right_versions(self):
        """Contract: three runs show three rows with the right versions —
        run 1 of v1 passes, run 2 (changed layout) fails at the changed
        step, run 3 of the corrected skill (v2) passes, with actual run
        numbers 1/2/3."""
        v1 = make_v1_skill()
        v2 = make_v2_skill()

        r1 = self.run_and_record(
            copy.deepcopy(v1), FakeDriver(make_good_pages(), FORM_URL)
        )
        self.assertEqual(r1.get("run_number"), 1)
        self.assertTrue(r1.get("passed"))
        self.assertIsNone(r1.get("failed_step"))

        r2 = self.run_and_record(
            copy.deepcopy(v1), FakeDriver(make_changed_layout_pages(), FORM_URL)
        )
        self.assertEqual(r2.get("run_number"), 2)
        self.assertFalse(r2.get("passed"))
        self.assertEqual(r2.get("failed_step"), 2)

        r3 = self.run_and_record(
            copy.deepcopy(v2), FakeDriver(make_changed_layout_pages(), FORM_URL)
        )
        self.assertEqual(r3.get("run_number"), 3)
        self.assertTrue(r3.get("passed"))
        self.assertIsNone(r3.get("failed_step"))

        rows = self.list_runs()
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["run_number"] for r in rows], [1, 2, 3])
        self.assertEqual([r["skill"] for r in rows], [v1["skill"]] * 3)
        self.assertEqual([r["version"] for r in rows], [1, 1, 2])
        self.assertEqual([r["passed"] for r in rows], [True, False, True])
        self.assertEqual([r["failed_step"] for r in rows], [None, 2, None])
        # One row per run on disk: JSONL, oldest first.
        self.assertEqual(len(self._jsonl_lines()), 3)


class InvalidSkillRecordedTest(RunsModuleBase):
    def test_invalid_skill_still_recorded_as_failed_with_failed_step_none(self):
        """Contract: an invalid skill still appends a failed-attempt row
        (passed False, failed_step None) instead of vanishing."""
        bad = make_invalid_skill()
        result = self.run_and_record(
            copy.deepcopy(bad), FakeDriver(make_good_pages(), FORM_URL)
        )
        self.assertFalse(result.get("passed"))
        self.assertIsNone(result.get("failed_step"))
        self.assertEqual(result.get("run_number"), 1)
        rows = self.list_runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["skill"], bad["skill"])
        self.assertEqual(rows[0]["version"], bad["version"])
        self.assertFalse(rows[0]["passed"])
        self.assertIsNone(rows[0]["failed_step"])
        self.assertEqual(rows[0]["run_number"], 1)


class RowShapeTest(RunsModuleBase):
    def test_row_has_timestamp_and_reason_keys(self):
        """Contract: one row per run carries skill/version/run_number/
        passed/failed_step/timestamp — plus the replay reason; not
        notebook prose."""
        v1 = make_v1_skill()
        self.run_and_record(copy.deepcopy(v1), FakeDriver(make_good_pages(), FORM_URL))
        rows = self.list_runs()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        for key in (
            "skill",
            "version",
            "run_number",
            "passed",
            "failed_step",
            "reason",
        ):
            self.assertIn(key, row, f"row missing {key!r}: {sorted(row)}")
        # Timestamp key: contract says "timestamp"; repo ledger
        # convention is "ts" — accept either, but require one present.
        stamp = row.get("timestamp", row.get("ts"))
        self.assertTrue(stamp, f"row missing timestamp/ts: {sorted(row)}")
        self.assertIsInstance(row["reason"], str)
        self.assertIsInstance(row["passed"], bool)


class ListFilterTest(RunsModuleBase):
    def test_list_runs_filters_by_skill_oldest_first(self):
        """Contract: list_runs(skill=None, limit=100) returns rows
        oldest-first, optionally filtered by skill name."""
        alpha = make_v1_skill(name="history-probe-alpha", version=1)
        beta = make_v1_skill(name="history-probe-beta", version=1)
        self.run_and_record(
            copy.deepcopy(alpha), FakeDriver(make_good_pages(), FORM_URL)
        )
        self.run_and_record(
            copy.deepcopy(beta), FakeDriver(make_good_pages(), FORM_URL)
        )
        self.run_and_record(
            copy.deepcopy(alpha), FakeDriver(make_good_pages(), FORM_URL)
        )
        only_alpha = self.list_runs(skill="history-probe-alpha")
        self.assertEqual(len(only_alpha), 2)
        self.assertTrue(all(r["skill"] == "history-probe-alpha" for r in only_alpha))
        self.assertEqual([r["run_number"] for r in only_alpha], [1, 2])
        only_beta = self.list_runs(skill="history-probe-beta")
        self.assertEqual(len(only_beta), 1)
        self.assertEqual(only_beta[0]["run_number"], 1)


class ListLimitTest(RunsModuleBase):
    def test_list_runs_limit_returns_oldest_rows(self):
        """Contract: list_runs(limit=N) caps the oldest-first row list to
        the most recent N, still oldest-first."""
        v1 = make_v1_skill()
        for _ in range(4):
            self.run_and_record(
                copy.deepcopy(v1), FakeDriver(make_good_pages(), FORM_URL)
            )
        self.assertEqual(len(self.list_runs()), 4)
        limited = self.list_runs(limit=2)
        self.assertEqual(len(limited), 2)
        self.assertEqual([r["run_number"] for r in limited], [3, 4])


class RunsHttpBase(unittest.TestCase):
    """Serve browser_skills.router on a bare FastAPI app with tmp-dir runs."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old_runs = os.environ.get("BROWSER_RUNS_FILE")
        os.environ["BROWSER_RUNS_FILE"] = os.path.join(tmp.name, "runs.jsonl")
        old_skills = os.environ.get("BROWSER_SKILLS_DIR")
        os.environ["BROWSER_SKILLS_DIR"] = os.path.join(tmp.name, "skills")

        def _restore():
            if old_runs is None:
                os.environ.pop("BROWSER_RUNS_FILE", None)
            else:
                os.environ["BROWSER_RUNS_FILE"] = old_runs
            if old_skills is None:
                os.environ.pop("BROWSER_SKILLS_DIR", None)
            else:
                os.environ["BROWSER_SKILLS_DIR"] = old_skills

        self.addCleanup(_restore)
        import backend.skill_runs as runs_mod

        importlib.reload(runs_mod)
        import backend.api.routers.browser_skills as skills_mod

        importlib.reload(skills_mod)
        from backend.skill_runs import list_runs, run_and_record

        self.run_and_record = run_and_record
        self.list_runs = list_runs
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(skills_mod.router)
        self.client = TestClient(app, raise_server_exceptions=False)


class HttpRunsTest(RunsHttpBase):
    def test_http_get_runs_returns_the_three_rows(self):
        """Contract: HTTP GET /api/browser/skills/{name}/runs returns
        {"ok", "runs"} with the three recorded rows."""
        v1 = make_v1_skill()
        v2 = make_v2_skill()
        self.run_and_record(copy.deepcopy(v1), FakeDriver(make_good_pages(), FORM_URL))
        self.run_and_record(
            copy.deepcopy(v1), FakeDriver(make_changed_layout_pages(), FORM_URL)
        )
        self.run_and_record(
            copy.deepcopy(v2), FakeDriver(make_changed_layout_pages(), FORM_URL)
        )
        resp = self.client.get(f"/api/browser/skills/{v1['skill']}/runs")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body.get("ok"))
        self.assertIn("runs", body)
        runs = body["runs"]
        self.assertEqual(len(runs), 3)
        self.assertEqual([r["run_number"] for r in runs], [1, 2, 3])
        self.assertEqual([r["version"] for r in runs], [1, 1, 2])
        self.assertEqual([r["passed"] for r in runs], [True, False, True])


class PerSkillCountingTest(RunsModuleBase):
    def test_second_skill_runs_do_not_interleave_run_numbers(self):
        """Contract: run numbers count per skill — a second skill's runs
        start at 1 and do not interleave the first skill's sequence."""
        alpha = make_v1_skill(name="history-probe-alpha", version=1)
        beta = make_v1_skill(name="history-probe-beta", version=1)
        a1 = self.run_and_record(
            copy.deepcopy(alpha), FakeDriver(make_good_pages(), FORM_URL)
        )
        a2 = self.run_and_record(
            copy.deepcopy(alpha), FakeDriver(make_good_pages(), FORM_URL)
        )
        b1 = self.run_and_record(
            copy.deepcopy(beta), FakeDriver(make_good_pages(), FORM_URL)
        )
        self.assertEqual(
            (a1["run_number"], a2["run_number"], b1["run_number"]), (1, 2, 1)
        )
        a3 = self.run_and_record(
            copy.deepcopy(alpha), FakeDriver(make_good_pages(), FORM_URL)
        )
        self.assertEqual(a3["run_number"], 3)
        self.assertEqual(
            [r["run_number"] for r in self.list_runs(skill="history-probe-alpha")],
            [1, 2, 3],
        )
        self.assertEqual(
            [r["run_number"] for r in self.list_runs(skill="history-probe-beta")],
            [1],
        )


if __name__ == "__main__":
    unittest.main()
