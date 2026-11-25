# Problem Statement (part 1 of 2)
The queue console interface currently lacks a header displaying real-time status information for active issues. Users cannot see the current issue identifier, its lifecycle phase (e.g., claimed, coding, testing, PR open), the duration spent in the current phase, or the time elapsed since the agent's last activity. Furthermore, the idle queue state is currently rendered as a blank panel rather than providing informative feedback on the waiting count, as observed in the UI rendering logic within `backend/api/routers/agent_control.py`.

## Acceptance
- Header displays "Issue: [ID] | Phase: [PHASE] | In Phase: [TIME] | Last Activity: [TIME]".
- Idle state displays "Status: Idle | Waiting: [COUNT]".

### Allowed Files
- `docs/specs/issue-1568.md`
- `backend/api/routers/agent_control.py`
- `backend/api/routers/agent.py`
- `backend/server.py`
