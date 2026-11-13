---
name: co_browsing_state_takeover
description: Protocol governing human-in-the-loop co-browsing, immediate takeover handling, command buffer suppression, and post-takeover DOM re-indexing.
trigger_nodes:
  - intent_explain
  - intent_fix
  - intent_implement
  - intent_review
---
# Skill: Co-Browsing Interaction & Immediate Takeover Protocol

## 1. Visual Pre-Verification Tenet
- Before executing any programmatic DOM click, form fill, or navigation action:
  - Take a snapshot and verify target element coordinates and visibility.
  - Never blindly send coordinates from an outdated turn.

## 2. Immediate Human Takeover Protocol
- The user has priority authority over the shared browser head at all times.
- When an incoming `POST /api/agent/pause` event is triggered by user interaction (pointerdown/keydown):
  1. **Drop In-Flight Buffer**: Discard any scheduled or queued browser commands in the current step immediately.
  2. **State Transition**: Set `agent_execution_state = "PAUSED_BY_USER"` in the persistent ledger.
  3. **Halt Loop**: Yield execution without throwing an error. Notify the UI: *"Manual takeover active. Agent standing by."*
  4. **No Mutation While Paused**: Do not issue any CDP or DOM commands until an explicit resume signal is received.

## 3. Mandatory Post-Takeover Resumption Protocol
- When `POST /api/agent/resume` is received:
  1. **DO NOT** reuse prior element references, CSS selectors, or snapshot IDs (`e1`, `e2`, etc.) — the user may have navigated, filled forms, or opened new tabs.
  2. **Mandatory Re-Snapshot**: Immediately capture a fresh DOM footprint (`snapshot -i` or `td audit-impact --json`).
  3. **Re-Plan From Fresh State**: Re-evaluate the active task against the updated page state before resuming automated execution.
