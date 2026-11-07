---
name: cobrowse_handoff_specialist
description: Owner for drive/takeover exchanges in the co-browse view; parks the agent on a human handoff with an explicit waiting state instead of stalling silently.
trigger_nodes:
  - visual_interaction_node
---
# Skill: Co-Browse Handoff Specialist

One owner for drive/takeover exchanges in the Buttons co-browse view.
When the driving agent hits a wall it cannot pass (login, MFA/2FA,
CAPTCHA, payment confirmation, ambiguous UI), it does NOT stall
silently: it parks on a human handoff and the viewer shows an
explicit waiting state until the human hands back.

## The loop

1. **Detect**: page needs a human (auth wall, MFA, CAPTCHA, payment,
   or UI state the agent cannot resolve after one retry).
2. **Request**: call `request_human_cobrowse_handoff(thread_id, reason,
   current_url)` (deep-agent/cobrowse_handoff.py). The tool POSTs the
   backend handoff contract (`POST /api/agent/handoff/request`) and
   returns the `browser_handoff_requested` interrupt payload —
   `thread_id`, `reason`, `current_url`, `live_view_url`,
   instructions. Raise it as the graph interrupt; the run pauses.
3. **Wait**: the Buttons drawer polls `/api/agent/state` and renders
   the waiting banner with the reason. The agent does nothing until
   resume — no polling the page, no retries.
4. **Resume**: the human acts in the live view and hands back (the
   drawer's Hand Back button → `POST /api/agent/handoff/resolve`).
   Resolve clears the handoff and resumes with a mandatory DOM
   re-snapshot, so the viewer shows the post-handoff state.
   Re-run from the fresh snapshot; never assume pre-handoff DOM.

## Rules

- Request with a specific reason (what is blocked and what the human
  must do). Blank reasons are rejected (400).
- Re-requesting refreshes the wait (idempotent) — never stack
  duplicate handoffs.
- `live_view_url` blank means "open the Buttons console co-browse
  view" (derived from `BUTTONS_PUBLIC_URL` when set).
- Side effects before the interrupt must be idempotent: resume
  re-runs the node from the top.
- Never touch cloud sandboxes, hosted browsers, or credentials.
  Local compose stack only.
