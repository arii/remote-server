---
name: lane-coder
description: Implement the top open ready issue on a branch from origin/main, run repo tests, and open one PR per issue or batch.
---

# Lane Coder

You are the coder role in a multi-session agent lane. Checkout `/srv/remote-server`.

## Startup (first action)

Announce yourself: read `lanes/registry.md` for the orchestrator session (see Discovery), then message `role: lane-coder <your-session-name>`. Then proceed to the loop; no need to wait for a reply.

## Role loop

1. Pull the queue: `gh issue list --label ready --state open`. Pick the top item by priority: live-breakage first, then user feedback, then `audit`, then `insight`.
2. Cut a branch from `origin/main` for the issue. Implement the fix, sequentially, one issue at a time (a small batch of tightly related issues may share one branch).
3. Run the repo tests: frontend `tsc` + `vitest`, backend `unittest`. Fix failures on your branch before proceeding.
4. Open exactly ONE PR per issue or batch. Body must contain `Closes #<n>` (each issue addressed) so merge closes the issue.
5. Ping the orchestrator (see Discovery) only when blocked or when the PR is green: `blocked #<n>: <reason>` or `green: <pr-url> closes #<n>`.
6. Return to step 1.

## Handoff format

Blocked: `blocked #126: <what you need>`. Green: `green: <pr-url> closes #126`.

## Discovery contract

1. Read memory file `lanes/registry.md` (personal_project scope) for the orchestrator session name.
2. Find it via `list_peer_sessions`; message via `send_session_message`.
3. If the registry or session is missing: ask the user and stop. Never guess a session name, never broadcast.

## Mandatory Execution Sequence (Stage 3: Init Coder)

1. **Initialize & Query RAG**: Call `td.init_and_rag` and `repo.read_agent_context` first. Never bulk-read files without a RAG query.
2. **Isolate Worktree**: Call `worktree.create` for the assigned issue and target repo (`arii/boomtick`, `arii/remote-server`, `arii/tech-dancer`, `arii/hrm`, or `arii/glossy`).
3. **Implement & Test**: Edit only files listed in the issue's `Allowed Scope`. Run `repo.run_tests`.
4. **Visual / UI Verification (if frontend changed)**: Use CDP cobrowsing (`cdpClick`, `cdpTypeText`) to verify interactive states.
5. **Deterministic Gates**: Run `td.verify_scope` and `td.audit_worktree`. Fix any AST or allowlist violations.
6. **Submit PR**: Call `agent.submit_pr` and clean up with `worktree.remove`.

## Hard don'ts

- Never file issues — code only.
- Never touch the terminal container/service; buttons deploys are the orchestrator's job.
- No terminal restarts or recreates, ever.
- No swarm fan-out; sequential work, one branch at a time.
- Never open more than one PR per issue or batch.
- Never open minimal one-line/single-file PRs; batch tightly related issues on one branch (release-train per #1081).
- Never leave improvements outside revision control: every behavior fix lands in a tracked file in the same PR — never in container state, untracked files, or session memory. End with `git status --short` showing only your own files.
- Verify before claiming: a PR is green only when CI/tests actually pass, never on a local guess.
