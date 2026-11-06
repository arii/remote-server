---
name: architectural_audit_rubric
description: Deep semantic audit checklist evaluating acceptance criteria traceability, anti-slop compliance, tautological tests, and downstream impact.
trigger_nodes:
  - deep_semantic_auditor_node
---
# Skill: Architectural Semantic Audit Rubric

## 1. Core Evaluation Tenets
The deep semantic auditor only runs after zero-token deterministic gates (`td verify-scope` and unit tests) pass 100%. Your role is cognitive verification that code cannot easily check automatically.

## 2. Audit Verification Checklist
1. **1-to-1 Acceptance Criteria Traceability**:
   - Review every checklist item in the original Issue Spec.
   - Map each item to concrete `file:line` diff evidence in the worktree.
   - If any criterion lacks explicit implementation evidence, mark `verified: false` and reject with `CHANGES_REQUESTED`.
2. **Tautological Test Detection**:
   - Scrutinize all new or modified test suites.
   - Reject tests that only verify mocks (e.g. mocking a function and asserting that the mock returned its configured return value). Tests MUST exercise real integration or logic boundaries.
3. **Dead Abstractions & Leaky Interfaces**:
   - Verify that no unused parameters, unused exports, or dead helper functions were left behind.
4. **Downstream Blast-Radius Verification**:
   - Read `td audit-impact --json`'s `downstream_dependents`.
   - Confirm that changed function signatures or schemas did not break un-migrated downstream callers.
5. **Visual & DOM Consistency (if `ui_changed == true`)**:
   - Inspect `visual_diff` metrics. Ensure layout shift, DOM node deletion, or CSS regressions are intended by the spec.
