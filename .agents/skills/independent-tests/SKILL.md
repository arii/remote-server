---
name: independent-tests
description: Write tests that actually verify behavior. The implementation author never writes its own acceptance tests; an independent party re-derives every assertion from the contract, proves red-green, and kills tautologies.
---

# Independent Tests

The author of a change is blind to its bugs. A test written by the same
hand that wrote the code re-states the code and passes with the bug
still in it. Every acceptance test in this repo is written or reviewed
by a party independent of the implementation.

## The independence rule

- The agent (or turn) that implements a change MUST NOT author its
  acceptance tests. A different agent re-derives assertions from the
  contract alone: the issue's acceptance criteria, the gate, the
  invariant. It never reads the diff first; it reads the diff only to
  check the tests pin real behavior, not implementation details.
- Self-checks the implementer runs (repro scripts, smoke runs) stay
  in `/tmp`, never in the repo. Only independently-authored tests land
  in tracked test files.

## Red-green proof (mandatory, evidenced)

- A new test must FAIL on the pre-fix tree (or with the fix reverted)
  and PASS with the fix. Paste both results in the report. A test
  that was never seen red is not a test; it is a comment.
- Reverting the fix to prove red must be trivially reversible
  (`git stash` in a worktree, never on a shared checkout).

## What makes a test real

- Assert BEHAVIOR through the public surface: exit codes, outputs,
  state changes, gate verdicts. Each test names the contract clause
  it pins (issue number, gate, invariant) in its docstring.
- FORBIDDEN: string-presence checks (`assertIn` on README/docs),
  tautologies (recomputing the implementation's own expression),
  tests that pass with the code deleted, tests that only run the
  author's own helper, network-dependent tests without a hermetic
  fake, time-dependent tests without injected clocks.
- Adversarial check before submitting: try to make the test pass
  with deliberately WRONG code. If it passes, delete the test and
  write a real one.

## Scope and determinism

- Small, deterministic, hermetic. No network, no wall-clock, no
  ordering dependence between tests. Env-dependent tests skip
  explicitly (`skipTest` naming the missing precondition), never fail.
- One behavior per test. Name the behavior, not the function:
  `test_stale_hash_fails_verify`, not `test_verify_skills_2`.
- Match the suite's conventions (unittest style in backend/tests and
  tests/, root-relative paths, documented cwd=repo-root contract).
  No `sys.path` hacks, no `__file__` climbing — entry points declare
  the path (see scripts/verify-python-paths.py).

## Review checklist (for the independent reviewer)

1. Could these tests have been written without reading the diff? (yes)
2. Was each test observed red before green? (evidence pasted)
3. Does each test fail if the behavior breaks, not just if the text changes?
4. Is there any test here the implementation author could have used to
   hide a bug? (if yes, rewrite it)
