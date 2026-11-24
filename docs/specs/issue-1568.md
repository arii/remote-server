## Problem Statement
The queue console interface currently lacks a header displaying real-time status information for active issues. Users cannot see the current issue identifier, its lifecycle phase (e.g., claimed, coding, testing, PR open), the duration spent in the current phase, or the time elapsed since the agent's last activity. Furthermore, the idle queue state is currently rendered as a blank panel rather than providing informative feedback on the waiting count, as observed in the UI rendering logic within `backend/api/routers/agent_control.py`.

## Goal
Implement a persistent header in the queue console that displays the active issue's metadata: issue ID, current phase, time in phase, and time since last activity. Ensure the idle state provides a clear "Idle" status with the current waiting queue count instead of an empty display.

## Scope & File Boundaries
### Allowed Files (Create / Modify / Delete)
- `docs/specs/issue-1568.md` (new)
* `backend/api/routers/agent_control.py`
* `backend/api/routers/agent.py`
* `backend/server.py`

### Forbidden Files
* `deep-agent/cli_fallback.py`
* `deep-agent/coder.py`
* `chrome-extension/eval/last-run.json`

## Implementation Plan
1. Modify `backend/api/routers/agent_control.py` to expose a new endpoint or update the existing state response to include `current_issue_id`, `phase`, `phase_start_time`, and `last_activity_timestamp`.
2. Update the data aggregation logic in `backend/api/routers/agent.py` to calculate the "time in phase" and "time since last activity" based on the current server time.
3. Update the response schema in `backend/server.py` to ensure the frontend receives a non-null object when the queue is idle, specifically returning a status of "Idle" and the current `waiting_count`.
4. Ensure the frontend (implied by the backend state delivery) receives the necessary fields to render the header.

## Definition of Done
* The queue console header displays the active issue ID, phase, and timestamps.
* The idle state displays "Idle" and the count of waiting issues.
* All existing agent control tests pass.

## Acceptance Criteria
- [ ] Header displays "Issue: [ID] | Phase: [PHASE] | In Phase: [TIME] | Last Activity: [TIME]".
- [ ] Idle state displays "Status: Idle | Waiting: [COUNT]".
- [ ] No blank panels are rendered when the queue is empty.
- [ ] Timestamps are calculated relative to the server's current clock.

## Verification Commands
```bash
python3 -m unittest backend.tests.test_agent_control_case.py
python3 -m pytest backend/tests/test_agent_turn.py
```

* Blocking questions:
    * Does the frontend consume the state via a specific WebSocket or REST endpoint defined in `backend/server.py`? (Assuming REST based on `backend/api/routers/agent_control.py` structure).
    * Are there existing utility functions for time delta formatting in `backend/` that should be reused?

## Investigation Evidence
# Evidence Summary — arii/remote-server#1568: Queue console shows no "now" header

## What was directly verified (tool results gathered this session)

**1. The agent control state model already carries most of the needed fields — but not phase timing.**
`backend/api/routers/agent_control.py` defines `AgentControlState` with:
- `state: Literal["RUNNING", "PAUSED_BY_USER", "IDLE"] = "IDLE"` — an idle state already exists in the model (backend/api/routers/agent_control.py, `class AgentControlState`).
- `active_issue: int | None = None` — the current issue identifier is already tracked.
- `current_step: str = ""` — a free-text step field, but **no** structured `phase`, `phase_start_time`, or `last_activity_timestamp` fields exist.
- `updated_at: float = 0.0` — a timestamp exists, but it is overwritten on every write (`write_control_state` sets `state.updated_at = time.time()`), so it reflects "last state write," not "last agent output."

**2. The REST endpoint exists and returns the raw state dict.**
`GET /api/agent/state` is implemented as `get_agent_state()` returning `read_control_state().model_dump()` (backend/api/routers/agent_control.py, `@router.get("/state")`). The router is mounted with prefix `/api/agent` (`router = APIRouter(prefix="/api/agent", tags=["agent_control"])`) and included in the app in `backend/server.py` (`app.include_router(agent_control.router)`). This answers the draft's blocking question: the state is consumed via **REST**, not WebSocket — at least for this router; no WebSocket transport was verified this session.

**3. The state file is shared between FastAPI and the CLI worker.**
The module docstring states the design: "Atomic state transitions (RUNNING <-> PAUSED_BY_USER) backed by a shared JSON state file, so FastAPI and CLI worker processes (deep-agent/pipeline.py) read and write the same lock" (backend/api/routers/agent_control.py, module docstring). Compose confirms the shared volume: `AGENT_CONTROL_STATE_FILE: /var/lib/buttons/agent_control_state.json` on both services (docker-compose.yml:196 and docker-compose.yml:355), with a comment that the file lives on the buttons-data volume (docker-compose.yml:351). This means **the worker (deep-agent/pipeline.py) is the natural writer of phase/last-activity fields**, and the forbidden-file boundary on `deep-agent/*` in the draft is a real constraint to flag: if the worker must emit phase timestamps, that file would need modification or the fields must be derived server-side.

**4. Idle default is a real model, not a blank panel — server-side.**
`read_control_state()` returns `AgentControlState(updated_at=time.time())` when the file is missing or unparsable (backend/api/routers/agent_control.py). So the API never returns null for idle; a blank console panel would be a **frontend rendering gap**, which was not verifiable this session (no frontend files were read).

**5. Existing tests confirm the endpoint contract.**
`tests/test_co_browsing_api.py` exercises `agent_control.get_agent_state()`, `pause_agent()`, `resume_agent()`, and handoff flows (tests/test_co_browsing_api.py:30–108), and imports the router directly (tests/test_co_browsing_api.py:11). `tests/test_pipeline_e2e.py` imports `AgentControlTestCase` from `backend.tests.agent_control_case` (tests/test_pipeline_e2e.py:20) — note the draft's verification command references `backend.tests.test_agent_control_case.py`, which does not match this observed module name (`backend.tests.agent_control_case`); the draft command should be corrected. `tests/test_compose_guards.py:129` (`test_agent_control_shared_state`) and :161 assert the shared `AGENT_CONTROL_STATE_FILE` compose setting, so any state-schema change must keep the file-based contract intact.

## What was NOT verified (explicit gaps)

- **No frontend/console files were read.** The claim that the console renders a blank panel when idle is unverified; the draft asserts it "as observed in the UI rendering logic within `backend/api/routers/agent_control.py`," but that file contains no rendering logic — it is a pure API router. The blank-panel behavior must be located in the frontend before implementation.
- **`backend/api/routers/agent.py` was not read** — the draft's plan step 2 (aggregation there) is unverified.
- **No phase-tracking source was found.** Nothing read this session shows where "claimed/coding/testing/PR open" phases are recorded. The queue docs (`docs/twt-queue.md:152`) describe queue_runner retry semantics and manifest entries but contain no phase-field evidence in the cited excerpts.
- **No time-delta formatting utility was found** in the files read; the draft's second blocking question remains open.

## Related context (pre-seeded, cited as given)

- ROADMAP.md:61 lists open console-adjacent items including #1491 ("remaining findings need a pass against the deployed console") — related but distinct from #1568.
- docs/boomtick-issues.md:76 confirms no open BoomTick-side items overlap this work.

## Recommended plan corrections

1. Add `phase`, `phase_started_at`, and `last_activity_at` to `AgentControlState` (backend/api/routers/agent_control.py) rather than a new endpoint — the `/api/agent/state` GET already exists and is tested.
2. Resolve the forbidden-file tension: if `deep-agent/pipeline.py` is the phase writer (per the module docstring's shared-state design), the scope boundary must be revisited or the fields derived from existing `current_step`/`updated_at` server-side.
3. Fix the verification command to `backend.tests.agent_control_case` (per tests/test_pipeline_e2e.py:20) and add coverage in `tests/test_co_browsing_api.py` style.
4. Locate and fix the frontend blank-panel rendering before claiming the idle-state acceptance criterion.

## Q&A Context
- **Are there existing utility functions for time delta formatting in `backend/` that should be reused?**:
