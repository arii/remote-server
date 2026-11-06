---
name: write_spec_issue
description: Enforces anti-slop architectural style, deterministic structure, file:line citations, and strict acceptance criteria for Issue Specs.
trigger_nodes:
  - draft_spec_node
---
# Skill: Design Specification Authoring & Anti-Slop Guide

## 1. Cognitive Guardrails & Anti-Slop Style
- **No Vague Passive Directives**: Never write "the system should be fast" or "errors should be handled appropriately". You MUST write explicit, measurable thresholds (e.g. "timeout must be bounded to 5000ms", "must return HTTP 422 with JSON `{error: 'INVALID_SPEC'}`").
- **No Speculative Abstractions**: Never create generic adapter patterns, wrapper facades, or placeholder interfaces for "future extensibility". Implement only the minimal concrete mechanism required.
- **Zero Tolerance for Placeholders**: Any appearance of `TODO`, `TBD`, `FIXME`, or `...` will cause immediate rejection by `td validate-issue --json`.

## 2. Mandatory Structural Sections
Every spec issue generated must contain these exact Markdown headers:
1. `## Problem Statement`: Must cite exact existing file paths and line numbers (`file.py#L12-L34`) identified during investigation.
2. `## Objective`: Single paragraph describing the target functional state.
3. `## Scope & File Boundaries`:
   - `### Allowed Files (Create / Modify / Delete)`: Explicit list of file paths. No wildcards in write scopes.
   - `### Forbidden Files`: Explicit list of critical infrastructure or out-of-scope files.
4. `## Implementation Plan`: Numbered step-by-step technical plan.
5. `## Acceptance Criteria`: Unchecked markdown checklist (`- [ ]`) verifying every required functional behavior.
6. `## Verification Commands`: A single fenced bash code block containing non-empty, executable test and lint commands that exit with code 0 on success.

## 3. Repair Protocol on Validation Failure
When `td validate-issue` returns `valid == false`:
- Inspect `validation_result["errors"]`.
- If an allowed file does not exist on disk and is not marked `(CREATE)`, correct the path using findings from `codebase_investigator_node`.
- If verification commands are missing or non-executable, look up the repository's `project_config.json` test scripts and supply the exact command.
