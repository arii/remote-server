# Issue 1786: Ledger Read Logic Tests

## Goal
Implement unit tests for `fold_latest_per_key` in `scripts/ledger_read.py` to ensure correct behavior when handling various combinations of "Link" and "Item / Note" fields.

## Edge Cases Covered
- Standard URL keys in the "Link" column.
- Blank "Link" column with `task-<id>` present in "Item / Note".
- Blank "Link" column with no `task-<id>` in "Item / Note" (fallback to note text).
- Multiple rows with same key (latest row overrides).

## Implementation
- Created `tests/test_ledger_read.py` with `test_fold_latest_per_key`.
- Verified with `python3 -m unittest tests.test_ledger_read`.
