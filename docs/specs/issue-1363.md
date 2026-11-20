**Step 1 — Repo file read (required before spec authoring):**

Quoting `backend/api/routers/agent_control.py` (read via `read_repo_file`):

> """Agent co-browsing pause/resume control (follow_up Issue 11).
>
> Atomic state transitions (RUNNING <-> PAUSED_BY_USER) backed by a
> shared JSON state file, so FastAPI and CLI worker processes
> (deep-agent/pipeline.py) read and write the same lock. Compose sets
> AGENT_CONTROL_STATE_FILE to /var/lib/buttons/agent_control_state.json
> on buttons and terminal (shared buttons-data volume); /tmp is
> per-container and would split-brain the pause (#1104).
> """

and its line 98–100 region (via grep):

> `./.worktrees/issue-1359/backend/api/routers/agent_control.py:100:    return state.handoff_reason is not None`

Also quoting `backend/tests/test_skill_format.py` (lines 1–14):

> """Acceptance tests freezing the skill file format (issue #1253).
>
> Contract source: the issue text only. These tests pin the public
> interface ``backend.skill_format.validate_skill`` /
> ``validate_skill_file`` behavior, not implementation internals.
> """
> ...
> from backend.skill_format import validate_skill, validate_skill_file

---

## Problem Statement

A dead-code sweep at commit `4445742` (arii/remote-server#1363) identified two public helpers with no production callers:

1. `validate_skill_file` — defined at `backend/skill_format.py#L209` (introduced in #1272, commit `e8e9f13`). Its only references are in `backend/tests/test_skill_format.py` (import at line 14, contract test at line 141). All runtime paths (`backend/skill_replay.py#L107`, `backend/skill_patch.py#L59`, `backend/skill_store.py#L181`) call `validate_skill(data)` directly. `skill_format.py` has no `__main__` block, so the helper is not reachable as an operator CLI entry point today.
2. `handoff_pending` — defined at `backend/api/routers/agent_control.py#L98-L100` (introduced in #1108, commit `1681fe7`). Its body is `return state.handoff_reason is not None`. Its only caller is `tests/test_co_browsing_api.py#L91,L127`. The production routes test `state.handoff_reason is not None` inline instead of calling the predicate.

## Goal

Eliminate the two test-only dead-code helpers by deleting `validate_skill_file` and its contract tests, and by giving `handoff_pending` a real production caller (replacing the inline `handoff_reason is not None` checks in the routes) so the predicate is live code rather than dead code.

## Scope

In scope: removing `validate_skill_file` from `backend/skill_format.py`; removing its import and test cases from `backend/tests/test_skill_format.py`; replacing inline `handoff_reason is not None` checks in `backend/api/routers/agent_control.py` route handlers with calls to `handoff_pending(state)`; keeping the existing `tests/test_co_browsing_api.py` tests passing unchanged (they already exercise `handoff_pending`).

Out of scope: adding a `__main__` CLI to `skill_format.py`; changing `validate_skill` semantics; modifying any other skill modules (`skill_replay.py`, `skill_patch.py`, `skill_store.py`); changing handoff state-file semantics or the `AgentControlState` model.

### Allowed Files (Create / Modify / Delete)
- `docs/specs/issue-1363.md` (new)

- backend/skill_format.py (MODIFY — delete `validate_skill_file` at line 209)
- backend/tests/test_skill_format.py (MODIFY — remove `validate_skill_file` import and its test cases, including the contract test at line 141)
- backend/api/routers/agent_control.py (MODIFY — replace inline `state.handoff_reason is not None` checks in route handlers with `handoff_pending(state)` calls)
- tests/test_co_browsing_api.py (MODIFY only if assertions need mechanical adjustment; behavior must not change)

### Forbidden Files

- backend/skill_replay.py
- backend/skill_patch.py
- backend/skill_store.py
- backend/api/routers/ files other than agent_control.py
- docs/skill-contract.md
- docker-compose*, Makefile, package.json

## Implementation Plan

1. In `backend/skill_format.py`, delete the `validate_skill_file` function (line 209 onward) and any now-unused imports it required (e.g. file-reading helpers, `os`/`json` if solely used there).
2. In `backend/tests/test_skill_format.py`, remove `validate_skill_file` from the import at line 14 and delete the test class/cases that exercise it (including the contract test at line 141 and its docstring reference). Keep all `validate_skill` tests intact.
3. In `backend/api/routers/agent_control.py`, locate each route handler that tests `state.handoff_reason is not None` inline and replace the expression with `handoff_pending(state)`. `handoff_pending` is defined at line 98 in the same module; ensure call sites appear after the definition or rely on module-level function resolution (calls happen at request time, so definition order is not a constraint).
4. Run the targeted backend test modules and the co-browsing API tests to confirm no behavior change.
5. Run a repo-wide grep for `validate_skill_file` to confirm zero remaining references.

## Definition of Done

- `validate_skill_file` no longer exists in `backend/skill_format.py` and no file in the repo references it.
- `backend/tests/test_skill_format.py` imports and tests only `validate_skill`.
- `handoff_pending` in `backend/api/routers/agent_control.py` has at least one production caller inside the same module's route handlers; no route handler tests `handoff_reason is not None` inline.
- `python3 -m unittest backend.tests.test_skill_format` and the co-browsing API tests pass with zero failures.
- `git diff` shows changes confined to the Allowed Files list.

## Acceptance Criteria

- [ ] `validate_skill_file` is deleted from `backend/skill_format.py`
- [ ] `backend/tests/test_skill_format.py` no longer imports or tests `validate_skill_file`
- [ ] All remaining `validate_skill` tests in `backend/tests/test_skill_format.py` pass
- [ ] Route handlers in `backend/api/routers/agent_control.py` call `handoff_pending(state)` instead of testing `state.handoff_reason is not None` inline
- [ ] `handoff_pending` retains its existing signature `def handoff_pending(state: AgentControlState) -> bool`
- [ ] `tests/test_co_browsing_api.py` passes without modification to its `handoff_pending` assertions
- [ ] Repo-wide grep for `validate_skill_file` returns zero matches
- [ ] No changes outside the Allowed Files list

## Verification Commands

```bash
python3 -m unittest backend.tests.test_skill_format
python3 -m unittest tests.test_co_browsing_api
python3 -m pytest backend/tests/test_skill_format.py tests/test_co_browsing_api.py
git diff --stat
```

---

**Blocking questions:**

- The issue's `Verify` section suggests `npm run test:backend && npm run test:root`, which is disallowed by the verification policy; the spec substitutes targeted `unittest`/`pytest` commands. Are there additional existing test modules (e.g. `backend.tests.test_skill_patch`) that must pass because they import from `skill_format`?
- For `handoff_pending`: the issue offers "delete both" or "wire it up" as alternatives. This spec chooses wiring it up (lower risk, keeps tested public API). Confirm that deleting it instead is not preferred.
- The caveat asks to confirm `validate_skill_file` is not intended as a future operator/CLI entry point. Is there a roadmap item (#1272 follow-ups) that plans a `skill_format` CLI? If yes, should the helper be kept and marked with a caller instead?

## Investigation Evidence
grep_repo {"pattern": "validate_skill_file"}: ./.worktrees/issue-1359/backend/tests/test_skill_format.py:5:``validate_skill_file`` behavior, not implementation internals.
./.worktrees/issue-1359/backend/tests/test_skill_format.py:14:from backend.skill_format import validate_skill, validate_skill_file
./.worktrees/issue-1359/backend/tests/test_skill_format.py:141:        """Contract: validate_skill_file reports file content that is not valid JSON/skill."""
./.worktrees/issue-1359/backend/tests/test_skill_format.py:146:            errors = va
grep_repo {"pattern": "handoff_pending"}: ./.worktrees/issue-1359/tests/test_co_browsing_api.py:91:        self.assertTrue(agent_control.handoff_pending(
./.worktrees/issue-1359/tests/test_co_browsing_api.py:127:        self.assertFalse(agent_control.handoff_pending(
./.worktrees/issue-1359/backend/api/routers/agent_control.py:98:def handoff_pending(state: AgentControlState) -> bool:
./.worktrees/wt-skill1261/tests/test_co_browsing_api.py:91:        self.assertTrue(agent_control.handoff_pending(
./.worktrees/wt-skill1261/tests/test_co_b
read_repo_file {"offset": 6000, "path": "backend/skill_format.py"}: d(f"step {step.n}: unknown action {step.action!r}")
        errors.extend(_selector_errors(step.selector, step.n, step.action))
        if step.action == "type" and not step.value:
            errors.append(f"step {step.n}: type needs a value")
        if step.check.type not in CHECK_TYPES:
            errors.append(f"step {step.n}: unknown check type {step.check.type!r}")
        if (
            step.check.type not in _EMPTY_EXPECTED_CHECK_TYPES
            and not step.check.expected
        
read_repo_file {"path": "backend/api/routers/agent_control.py"}: """Agent co-browsing pause/resume control (follow_up Issue 11).

Atomic state transitions (RUNNING <-> PAUSED_BY_USER) backed by a
shared JSON state file, so FastAPI and CLI worker processes
(deep-agent/pipeline.py) read and write the same lock. Compose sets
AGENT_CONTROL_STATE_FILE to /var/lib/buttons/agent_control_state.json
on buttons and terminal (shared buttons-data volume); /tmp is
per-container and would split-brain the pause (#1104).
"""

from __future__ import annotations

import json
i
read_repo_file {"offset": 4600, "path": "backend/api/routers/agent_control.py"}: ing - mandatory DOM re-snapshot scheduled"
    current.requires_dom_refresh = True
    return write_control_state(current).model_dump()

grep_repo {"pattern": "handoff_reason is not None"}: ./.worktrees/issue-1359/backend/api/routers/agent_control.py:100:    return state.handoff_reason is not None
./.worktrees/wt-skill1261/backend/api/routers/agent_control.py:100:    return state.handoff_reason is not None
./.worktrees/issue-1357/backend/api/routers/agent_control.py:100:    return state.handoff_reason is not None
./.worktrees/issue-1358/backend/api/routers/agent_control.py:100:    return state.handoff_reason is not None
./.worktrees/issue-1360/backend/api/routers/agent_control.py:1
read_repo_file {"offset": 3000, "path": "tests/test_co_browsing_api.py"}: ROL_STATE_FILE", None)
        else:
            os.environ["AGENT_CONTROL_STATE_FILE"] = self._old

    def test_request_parks_with_waiting_state(self):
        from backend.api.routers.agent_control import HandoffRequest
        got = agent_control.request_handoff(
            HandoffRequest(reason="login wall", current_url="https://x/login"))
        self.assertEqual(got["state"], "PAUSED_BY_USER")
        self.assertTrue(agent_control.handoff_pending(
            agent_control.read_control_s
grep_repo {"pattern": "validate_skill("}: ./.worktrees/issue-1359/docs/skill-contract.md:43:`backend.skill_format.validate_skill(data)` returns a list of errors, empty on
./.worktrees/issue-1359/docs/skill-contract.md:181:assert not validate_skill(skill)
./.worktrees/issue-1359/backend/skill_replay.py:107:    errors = validate_skill(skill)
./.worktrees/issue-1359/backend/skill_patch.py:59:    errors = validate_skill(new_skill)
./.worktrees/issue-1359/backend/skill_store.py:181:    errors = validate_skill(data)
./.worktrees/issue-1359/ba
read_repo_file {"offset": 0, "path": "backend/tests/test_skill_format.py"}: """Acceptance tests freezing the skill file format (issue #1253).

Contract source: the issue text only. These tests pin the public
interface ``backend.skill_format.validate_skill`` /
``validate_skill_file`` behavior, not implementation internals.
"""

import copy
import json
import os
import tempfile
import unittest

from backend.skill_format import validate_skill, validate_skill_file


def make_example_skill():
    """Build the contract's canonical fill-contact-form v1 example."""
    return {
read_repo_file {"offset": 4600, "path": "backend/tests/test_skill_format.py"}: _rejected(self):
        """Contract: a skill needs at least one step; empty steps is invalid."""
        skill = make_example_skill()
        skill["steps"] = []
        self.assertTrue(validate_skill(skill))

    def test_version_zero_rejected(self):
        """Contract: version is int >= 1; version 0 is invalid."""
        skill = make_example_skill()
        skill["version"] = 0
        self.assertTrue(validate_skill(skill))

    def test_non_http_source_url_rejected(self):
        """Contra
grep_repo {"pattern": "from backend.skill_format import"}: ./.worktrees/issue-1359/docs/skill-contract.md:169:from backend.skill_format import validate_skill
./.worktrees/issue-1359/backend/skill_replay.py:30:from backend.skill_format import validate_skill
./.worktrees/issue-1359/backend/skill_patch.py:22:from backend.skill_format import validate_skill
./.worktrees/issue-1359/backend/skill_store.py:26:from backend.skill_format import validate_skill
./.worktrees/issue-1359/backend/tests/test_skill_patch.py:13:from backend.skill_format import validate_ski
grep_repo {"pattern": "handoff_reason"}: ./.worktrees/issue-1359/tests/test_co_browsing_api.py:93:        self.assertEqual(got["handoff_reason"], "login wall")
./.workt

## Q&A Context
- **The caveat asks to confirm `validate_skill_file` is not intended as a future operator/CLI entry point. Is there a roadmap item (#1272 follow-ups) that plans a `skill_format` CLI? If yes, should the helper be kept and marked with a caller instead?**: 
- **The issue's `Verify` section suggests `npm run test:backend && npm run test:root`, which is disallowed by the verification policy; the spec substitutes targeted `unittest`/`pytest` commands. Are there additional existing test modules (e.g. `backend.tests.test_skill_patch`) that must pass because they import from `skill_format`?**:
