# Browser skill contract

A skill is versioned JSON with ordered actions and deterministic checks. No model
is needed to record activity, summarize it, replay a skill, or reproduce a trace.

## APIs

Browser HTTP routes use the existing Buttons backend and its authentication.
`snapshot`, `drive` and `state` accept `?target=grid` (default) or `?target=paired`.
Grid drive attaches to an existing agent/viewer-shared session; it does not create
one. Pair Chrome through the Browser tab for the paired target.

| Method and path | Contract |
| --- | --- |
| `GET /api/browser/snapshot` | Current URL, title and element targets. |
| `POST /api/browser/drive` | `{"op":"navigate","url":"http://example.invalid/"}`; other ops: `click` with `x,y`, `type` with `text`, `key` with `key,code`, `wheel` with `x,y,dx,dy`. Coordinates are CSS pixels. |
| `GET /api/browser/state` | Session availability, human-wheel state, pairing and learn status. |
| `GET /api/browser/activity?limit=50` | Human/agent input events, oldest first within the returned window. |
| `GET /api/browser/notes` | Shared notebook. |
| `POST /api/browser/learn` | Summarize pending activity into notebook prose; also runs automatically after 25 events, subject to cooldown. Does not export executable skill JSON. |
| `GET /api/browser/skills/` | Stored names and latest versions. |
| `GET /api/browser/skills/{name}` | Latest saved skill. Unknown name: 404. |
| `POST /api/browser/skills/` | Body `{"skill": <skill object>}`. Validates, saves a strictly newer version and pins its bytes. Invalid/conflicting save: 400. |
| `GET /api/browser/skills/{name}/runs?limit=100` | Durable run rows, oldest first within the returned window. |
| `GET /api/agent/state`; `POST /api/agent/pause`; `POST /api/agent/resume` | Shared control state and explicit takeover/resume. Refresh the snapshot after resuming. |

Replay and correction are Python APIs, not HTTP endpoints:

- `skill_runs.run_and_record(skill, driver)` executes through the human-wheel
  gate, checks each step, stops at the first failure, and persists its trace.
- `skill_patch.apply_patch(skill, {"step": 6, "selector": "[data-testid='submit-changed']", "by": "builder", "why": "layout changed"})`
  returns a new `skill` and correction `log`, or `error`. Change exactly one field:
  a nonempty selector/value, or a complete check. The version increments; the input stays
  unchanged. Save the result with `skill_store.save_skill`; retain the returned
  correction log yourself.
- `skill_store.read_skill(name)` returns the latest skill or `None`;
  `save_skill(skill)` returns name/version or `error`. Earlier versions remain.

## File format

[JSON Schema](../backend/skill_schema.json) defines structure;
`backend.skill_format.validate_skill(data)` returns a list of errors, empty on
success. Use both when building tooling. Steps are numbered 1 through N. Prefer
stable test IDs or ARIA hooks; XPath and bare-text selectors are rejected.
Actions are `navigate`, `click`, `type`, `key`, `wheel`. Each step has one check:
`url_contains`, `element_visible`, `text_contains` or `value_equals`. Only
`element_visible` permits an empty expectation. Navigate selectors are HTTP(S)
destinations. Type/key send keyboard input; ensure the intended field has focus.

This complete example targets the local fixture served by the demo below:

```json
{
  "skill": "fixture-form",
  "version": 1,
  "source_url": "http://skill-fixture.invalid/form",
  "steps": [
    {"n": 1, "action": "navigate", "selector": "http://skill-fixture.invalid/form", "value": "", "check": {"type": "url_contains", "expected": "/form"}},
    {"n": 2, "action": "click", "selector": "[data-testid='name']", "value": "", "check": {"type": "element_visible", "expected": ""}},
    {"n": 3, "action": "type", "selector": "[data-testid='name']", "value": "Demo Builder", "check": {"type": "value_equals", "expected": "Demo Builder"}},
    {"n": 4, "action": "click", "selector": "[data-testid='email']", "value": "", "check": {"type": "element_visible", "expected": ""}},
    {"n": 5, "action": "type", "selector": "[data-testid='email']", "value": "demo@example.invalid", "check": {"type": "value_equals", "expected": "demo@example.invalid"}},
    {"n": 6, "action": "click", "selector": "[data-testid='submit']", "value": "", "check": {"type": "url_contains", "expected": "/confirmation"}}
  ]
}
```

## Configuration

`backend/browser_config.py` resolves paths relative to the repository, not the
working directory. Absolute paths and `~` work. A per-file override wins over
the data root. Defaults below are portable; deployment overrides live in Compose.

| Variable | Default / meaning |
| --- | --- |
| `BROWSER_DATA_DIR` | `data/browser` under the repository. |
| `BROWSER_GRID_URL` | `http://127.0.0.1:4444`; HTTP(S) Grid base without query/fragment. Discovery and derived WS/WSS connections share this value. |
| `BROWSER_ACTIVITY_FILE` | `browser_activity.jsonl` under the data root. |
| `BROWSER_NOTES_FILE` | `browser_notes.md` under the data root. |
| `BROWSER_SKILLS_DIR` | `skills` under the data root. |
| `BROWSER_RUNS_FILE` | `browser_runs.jsonl` under the data root. |
| `BROWSER_TASKS_DIR` | `browser_tasks` under the data root. |
| `BROWSER_PROFILE_DIR` | `agent` under the data root; Grid agent profile. |
| `BROWSER_DRIVER` | `grid`, `playwright` or `browser-mcp`; default `grid`. |
| `BROWSER_HEADLESS` | Playwright: `1` by default; `0` opens a window. |
| `BROWSER_MCP_URL` | Relay base, default `http://127.0.0.1:9010`. |
| `AGENT_CONTROL_STATE_FILE` | Control JSON, default `/tmp/agent_control_state.json`; deployment overrides it. |

The Grid profile path must also be valid inside the browser container.

## Driver interface

`backend.skill_drivers.get_driver()` selects the configured driver. There is no
automatic switch to another backend on failure. Implement this synchronous
`BrowserDriver` protocol; `check_driver(driver)` reports missing methods:

```python
class Driver:
    def snapshot(self) -> dict: ...  # {url, title, elements}
    def navigate(self, url: str) -> None: ...
    def click(self, x: int, y: int) -> None: ...
    def type_text(self, text: str) -> None: ...
    def key(self, key: str) -> None: ...
    def wheel(self, x: int, y: int, dx: int, dy: int) -> None: ...
```

Coordinates use the snapshot's CSS-pixel space. Elements carry selector hooks,
position, type/value and focus/sensitivity metadata; preserve credential markers
and mark protected values `[redacted]`. Raise `BrowserDriverError` on failure.
Use `run_and_record` rather than composing unguarded primitives: local Playwright
and relay primitives do not themselves enforce the shared human-wheel gate.

## Safety and trace semantics

- Login, MFA, CAPTCHA, payments and message sends stay human. Do not encode them
  in skills. The wheel gate blocks mutations while `PAUSED_BY_USER`; it is not
  an intent classifier or approval system.
- Run/activity persistence redacts protected input and field-check expectations,
  credential URLs and echoed errors before writing. Type/key sensitivity follows
  both target and focus; navigation and URL-match checks are not secret field
  values. Saved skill JSON and returned correction logs retain literal values:
  never put real credentials in them.
- Saves pin SHA-256 bytes in `skills-lock.json` alongside the skills. Startup
  verification publishes missing/tampered/unpinned files through the existing
  attention warning. It does not block reads or execution. Investigate a warning;
  do not silence it by repinning unknown content.
- Run rows have `trace_version: 1`, run/skill versions, context and ordered steps.
  Each step includes input, proposal, gate verdict, execution flag, outcome,
  page hash, model/gate versions and live/replay mode. Outcomes distinguish
  passed, failed check, driver error and blocked gate. A hash identifies observed
  page state, not proof of safety; context records whether it is pre/post action.
  Unobservable live dialog/error states explicitly mark an unavailable hash.
- `skill_runs.replay_trace(row)` validates and copies one stored row, reproducing
  its sequence, failure point and recorded verdicts offline. No browser, network,
  skill lookup or action execution occurs. Redacted inputs gain
  `replay_status: human_intervention`; their original values are never recovered.
  Execution flags remain historical. Unsupported, incomplete or contradictory
  rows raise `ValueError`; zero-step validation-failure rows are supported.

## Three-act demo

From the repository root, use Python 3.11+ with backend dependencies, Playwright
and its Chromium binary already installed (the terminal image includes the
package; browser installation is separate). The timed demo needs no server,
Grid, relay or model. It scripts a synthetic human demonstration, not a live
human recording. The real fixture, browser, notebook, store and replay run below
are not mocked. Artifacts remain in a fresh directory under your home.

Run this single command; expect three acts in under two minutes after setup.
The private Playwright page hook is used only to serve the fixture and block
external requests. Layout changes affect served bytes, not the source file.

```bash
python3 - <<'PY'
import copy
import json
import os
from pathlib import Path
import tempfile
import time

from backend import skill_patch, skill_runs, skill_store
from backend.api.routers import browser_view
from backend.skill_drivers import PlaywrightDriver
from backend.skill_format import validate_skill

started = time.monotonic()
root = Path(tempfile.mkdtemp(prefix="skill-demo-", dir=Path.home()))
os.environ.update(BROWSER_DATA_DIR=str(root),
    BROWSER_ACTIVITY_FILE=str(root / "activity.jsonl"),
    BROWSER_NOTES_FILE=str(root / "notes.md"),
    BROWSER_SKILLS_DIR=str(root / "skills"),
    BROWSER_RUNS_FILE=str(root / "runs.jsonl"),
    AGENT_CONTROL_STATE_FILE=str(root / "control.json"))
doc = Path("docs/skill-contract.md").read_text()
skill = json.loads(doc.split("```json\n", 1)[1].split("\n```", 1)[0])
assert not validate_skill(skill)
url = skill["source_url"]
fixture = Path("tests/fixtures/browser/form.html").read_text()
changed, blocked = False, []
driver = PlaywrightDriver(headless=True)
try:
    page = driver._ensure()
    def serve(route):
        if route.request.url != url:
            blocked.append(route.request.url)
            return route.abort()
        source = fixture.replace('data-testid="submit"', 'data-testid="submit-changed"') if changed else fixture
        route.fulfill(content_type="text/html", body=source)
    page.route("**/*", serve)
    for step in skill["steps"]:
        before = driver.snapshot()
        action, selector, value = step["action"], step["selector"], step["value"]
        if action == "navigate":
            driver.navigate(selector)
        elif action == "click":
            page.locator(selector).click()
        else:
            driver.type_text(value)
        browser_view._log_activity(action, value, via="human", action=action,
            selector=selector, url=url if action == "navigate" else before["url"],
            page_hash=browser_view.page_hash_of(before), target="playwright")
    events = browser_view.recent_activity(6)
    recorded = copy.deepcopy(skill)
    for step, event in zip(recorded["steps"], events, strict=True):
        step.update(action=event["action"], selector=event["selector"], value=event["detail"])
    learned = browser_view.run_learn_pass(manual=True)
    assert learned["wrote"] and learned["events_covered"] == 6, learned
    assert "error" not in skill_store.save_skill(recorded)
    print("Act 1: recorded 6 inputs; notebook learned 6 events; saved v1")
    def replay(stored):
        row = skill_runs.run_and_record(stored, driver)
        if row["passed"]:
            text = page.locator("[data-testid='confirmation']").inner_text()
            assert "Demo Builder" in text and "demo@example.invalid" in text
        return row
    stored = skill_store.read_skill(skill["skill"])
    first = replay(stored)
    assert first["passed"], first
    print("Act 2: v1 replay passed; confirmation contains both entered values")
    changed = True
    broken = replay(stored)
    assert not broken["passed"] and broken["failed_step"] == 6, broken
    assert "/confirmation" not in page.url
    patch = skill_patch.apply_patch(stored, {"step": 6,
        "selector": "[data-testid='submit-changed']", "by": "demo-builder",
        "why": "fixture submit hook renamed"})
    assert "error" not in patch, patch
    assert "error" not in skill_store.save_skill(patch["skill"])
    (root / "correction.json").write_text(json.dumps(patch["log"], indent=2))
    corrected = replay(skill_store.read_skill(skill["skill"]))
    assert corrected["passed"] and corrected["version"] == 2, corrected
    assert not blocked, blocked
    print("Act 3: layout broke step 6; one selector correction; v2 replay passed")
    rows = skill_runs.list_runs(skill["skill"])
    reproduced = [skill_runs.replay_trace(row) for row in rows]
    print("Offline verdicts:", [row["passed"] for row in reproduced])
    print("Failure points:", [row["failed_step"] for row in reproduced])
    print("Artifacts:", root)
finally:
    driver.close()
print(f"Elapsed: {time.monotonic() - started:.1f}s")
PY
```

Expect verdicts `[True, False, True]` and failure points `[None, 6, None]`.
If Playwright/Chromium is missing, install it before timing; a skipped test is
not a completed demo. For a blocked external site, use this fixture instead of
trying to evade the block. The demo does not verify a live Grid or MCP relay.
