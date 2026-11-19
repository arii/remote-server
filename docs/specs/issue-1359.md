# Issue #1359 — Remove dead `plan_history_trim` wrapper

## Problem

`plan_history_trim` in `deep-agent/deep_chat.py` was a one-line wrapper
over `trim_history_by_count(...)[1]`. Its only production call site was
removed in commit `2fece05` (#1305, console rebuild); trimming now flows
through `trim_thread_history` / `trim_history_by_count` directly. The
wrapper survived only as dead code referenced by tests.

## Change

- Deleted `plan_history_trim` from `deep-agent/deep_chat.py`.
- `tests/test_deep_chat.py`: repointed all 6 references in
  `TestHistoryTrim` to `deep_chat.trim_history_by_count(...)[1]`
  (equivalent semantics; no assertions dropped).
- `tests/test_token_deferrals_r3.py`: the "state trim / compaction
  nodes" CLAIMS entry now pins `def trim_history_by_count` instead of
  `def plan_history_trim`, with a note that the wrapper was removed as
  dead code under #1359; the `TestTrimLocationPinned` guard loop
  iterates over `("trim_history_by_count", "trim_thread_history")`.

## Out of scope / unchanged

`trim_history_by_count` and `trim_thread_history` bodies are untouched;
no trimming behavior change; no `backend/` changes.

## Verification

```
python3 -m pytest tests/test_deep_chat.py tests/test_token_deferrals_r3.py
```

`grep -rn plan_history_trim` (excluding `.worktrees/`) returns zero
matches.
