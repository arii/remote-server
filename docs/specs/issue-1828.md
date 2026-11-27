## Problem Statement
In `scripts/queue_runner.py`, the `SafetyStop` exception is raised when an issue's ledger contains unknown metered costs. This exception currently propagates to the top-level queue loop, causing the entire `twt-queue` service to halt globally. This behavior is overly aggressive for per-issue ledger inconsistencies, which should instead result in parking the specific offending issue while allowing the queue to continue processing other pending tasks.

## Goal
Modify `scripts/queue_runner.py` to catch `SafetyStop` exceptions related to "unknown metered costs" within the issue processing loop. When such an error occurs, the system must mark the specific issue as blocked/parked and proceed to the next item in the queue, reserving global service termination only for critical infrastructure failures like canonical-ledger sync errors or gateway billing failures.

## Scope & File Boundaries
### Allowed Files (Create / Modify / Delete)
- `docs/specs/issue-1828.md` (new)
* scripts/queue_runner.py

### Forbidden Files
* All other files in the repository.

## Implementation Plan
1.  Modify the main processing loop in `scripts/queue_runner.py` where issue ledger validation occurs.
2.  Wrap the ledger validation logic in a `try...except SafetyStop` block.
3.  Inside the `except` block, inspect the `SafetyStop` message. If the message indicates "unknown metered costs" (or similar ledger-specific validation failure), log the error, mark the current issue as parked/failed in the local state, and continue the loop.
4.  Ensure that other `SafetyStop` triggers (e.g., canonical sync failures, gateway billing failures) continue to raise the exception to the top level to trigger a global halt as required.
5.  Update the state management to ensure the offending issue is excluded from subsequent queue cycles until manually retried.

## Definition of Done
* The `twt-queue` service no longer halts globally when a single issue encounters an "unknown metered cost" error.
* The offending issue is correctly identified and parked in the local state.
* The queue continues to process subsequent issues after encountering a ledger-specific `SafetyStop`.
* Global halts are preserved for canonical sync and gateway billing failures.

## Acceptance Criteria
- [ ] `scripts/queue_runner.py` catches `SafetyStop` for ledger cost errors.
- [ ] The queue runner logs the specific issue failure and proceeds to the next issue.
- [ ] Global service halt is still triggered by non-ledger `SafetyStop` exceptions.
- [ ] Verification tests pass without regressions in existing queue logic.

## Verification Commands
```bash
python3 -m pytest tests/test_queue_runner.py
```

*   **Blocking Questions**:
    *   Does the current `SafetyStop` implementation provide a reliable way to distinguish between "unknown metered costs" and "canonical sync failures" via the exception message or a subclass? (I will assume string matching on the exception message based on the provided excerpt).

## Investigation Evidence
To gather evidence for the required changes in `scripts/queue_runner.py`, I have analyzed the provided excerpts and repository context.

### Evidence Analysis

1.  **Current Global Halt Behavior**:
    The `SafetyStop` exception is defined as a `RuntimeError` in `scripts/queue_runner.py:26`. The provided excerpt shows that `SafetyStop` is raised in multiple contexts, including ledger validation (`scripts/queue_runner.py:1500+` - inferred from context of ledger cost checks). Because it is a standard exception, any unhandled `SafetyStop` raised within the main loop of `queue_runner.py` will propagate to the top level, terminating the process.

2.  **Ledger Cost Validation**:
    The logic for checking ledger costs is located in `scripts/queue_runner.py`. Specifically, the function `total += cost` followed by `if cost is None: raise SafetyStop("unknown or incomplete TwT cost: " + trace)` (inferred from the provided excerpt) is the source of the "unknown metered costs" error.

3.  **Distinguishing Error Types**:
    The current implementation uses a single `SafetyStop` class for various error conditions. To implement the requested change, I will need to differentiate between:
    *   **Per-issue ledger errors**: These should be caught and handled by parking the issue.
    *   **Critical infrastructure failures**: These (e.g., canonical sync failures, gateway billing) should continue to propagate `SafetyStop` to trigger a global halt.

    Based on the provided code, the error messages are passed as strings to the `SafetyStop` constructor. I will use these strings to categorize the exceptions.

### Verification of Requirements

*   **Problem**: `scripts/queue_runner.py` halts globally on ledger cost errors.
*   **Required Change**: Scope the halt to the offending issue.
*   **Target**: `scripts/queue_runner.py`.
*   **Verification**: `python3 -m pytest tests/test_queue_runner.py`.

### Implementation Strategy

I have sufficient information to proceed. I will:
1.  Locate the main loop in `scripts/queue_runner.py` that iterates over issues.
2.  Implement a `try-except` block around the ledger validation logic.
3.  Use string matching on the `SafetyStop` exception message to identify "unknown metered costs" (e.g., checking for "unknown or incomplete TwT cost").
4.  If matched, log the error, update the issue's state to "parked" or "failed" (using existing state management methods in `queue_runner.py`), and `continue` the loop.
5.  If not matched, re-raise the `SafetyStop` to maintain the existing global halt behavior for critical failures.

No further information gathering is required. I am ready to proceed with the implementation.
