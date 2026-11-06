---
name: coder_root_cause_analysis
description: Enforces systematic root-cause tracing and hypothesis validation during coder re-planning loops to prevent hallucination cycles.
trigger_nodes:
  - validated_replanner_node
---
# Skill: Coder Root Cause Analysis & Replanning Runbook

## 1. Anti-Hallucination Re-Planning Protocol
When deterministic gates (`td verify-scope`, `td audit-code`, or verification commands) fail consecutively:
- **DO NOT** attempt immediate shallow edits.
- **DO NOT** wrap failing assertions in `try/except` or add type suppressions (`# type: ignore`, `// @ts-ignore`).
- You MUST execute the following 4-step diagnostic loop before producing revised code.

## 2. Four-Step Diagnostic Loop
1. **Stack Trace Deconstruction**:
   - Read the exact failing line and assertion message from test stdout/stderr.
   - Locate the caller and callee frames inside the active worktree.
2. **Component Isolation & Dependency Inspection**:
   - Query `td rag query --query "<failing symbol or module>" --json` to inspect the authoritative contracts of all interacting components.
   - Verify whether the failure is caused by an unfulfilled interface assumption or a broken import.
3. **Explicit Hypothesis Formulation**:
   - Write an explicit one-sentence hypothesis in the persistent decision ledger (`decisions.py`):
     `"Failure occurred because [Root Cause]; fix requires modifying [Component] to [Action]."`
4. **Scope-Preserving Plan Adjustment**:
   - Ensure the repair does not add files outside `Allowed Files`. If an unlisted file genuinely needs modification, invoke `td validate-issue` to verify the amended scope before proceeding.
