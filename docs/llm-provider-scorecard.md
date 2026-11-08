# LLM Provider Scorecard — Ollama (local) vs Nebius (cloud)

Living record of what each model provider has actually done, with evidence
links. Updated when a provider completes, fails, or is probed on real work.
Read this before routing agent work to a provider.

Rule (standing): reviews and approvals flow through the existing framework —
the repo's `AI Review & Impact Analysis` CI check and the lane reviewer
protocol (`audit-gemini-talker` skill for Gemini). This file records
outcomes; it does not create a parallel review framework.

## Current status (2026-11-08)

| Provider | Reachable now | Role tonight | Tonight's spend |
|---|---|---|---|
| Ollama local (`qwen3:8b` + 5 more) | Yes — `/api/tags` lists 6 models, generate works | Advisory review probes | $0 (local) |
| Nebius (GLM, `NEBIUS_*` env set, spend cap set) | Configured, not invoked tonight | None (no suitable task materialized past native capacity) | $0 tonight; ~$0.11 lifetime (proof-2 + round-3) |
| Gemini reviewer framework | Via skill + CI check | Unchanged (not reimplemented) | — |

## Task history (evidence-linked)

Verified = checked live for this scorecard; registry-cited = recorded in
`lanes/registry.md` 2026-11-06/07 entries, not re-checked tonight.

| Date | Provider | Task | Outcome | Evidence |
|---|---|---|---|---|
| 11-08 | Ollama `qwen3:8b` | Review 8-line scope-probe diff (live API probe) | FAIL (false positive on truncated diff) | This session log; rerun with full context before trusting |
| 11-08 | Ollama `qwen3:8b` | Prior review round | PASS (NO-BUGS verdict) | Session `/tmp/ollama-verdict.txt` (ephemeral — see gap 3) |
| 11-08 | Nebius GLM-5.3-Flash | Proof-2: plan-approve-resume e2e | PASS (~$0.0485) | [RS #1174 merged](https://github.com/arii/remote-server/pull/1174) |
| 11-07 | Nebius | Round-3 implement+fixups on BT #1179 (visited-keyed guard, cache clear, 2-phase warm-cache test) | PASS after 3 review iterations (~$0.06) | [BT #1179 comment](https://github.com/arii/boomtick/issues/1179#issuecomment-6708687348), [status note](https://github.com/arii/boomtick/issues/1179#issuecomment-6709578024) |
| 11-07 | Nebius | Spec validation verdicts (#1125/#1127/#1129 READY/NEEDS-WORK) | PASS (registry-cited) | `lanes/registry.md` 2026-11-07 |
| 11-07 | Nebius | Guards batch (#1162/#1163): partial, needed orchestrator rescue | MIXED — strong implementer, weak investigator (registry-cited) | `lanes/registry.md` 2026-11-07 Nebius verdict |
| 11-07 | Nebius | Validator batch (#1181/#1182): zero edits in 100 steps | FAIL (registry-cited) | `lanes/registry.md` 2026-11-07 Nebius verdict |
| 11-07 | Nebius | Review BLOCKs with adjudication (#1135 6 findings, #1142, #1154 approve) | PASS (registry-cited) | `lanes/registry.md` 2026-11-07 |
| Ongoing | Ollama | `keyless-Ollama proof` CI job on every RS PR | PASS (recurring gate) | Any recent RS PR checks, e.g. #1187/#1188 runs |

## Spec-validation record (the question this file answers)

- Issues validated as designed specs by a model: Nebius verdicts on BT
  #1125/#1127/#1129 (registry-cited, 2026-11-07). No local-LLM spec
  validation has been attempted — Ollama has done code review only.
- Tonight: no spec required model validation; all audit findings were
  tree-verified by agents directly.

## Standing rules (from evidence)

1. Nebius gets surgical mechanical briefs with pre-verified facts; never
   open-ended investigation (validator batch: 0 edits/100 steps).
2. Small-model (Ollama) reviews are advisory only and need full diff
   context — a truncated 8-line diff drew a hallucinated BUG verdict
   tonight. Never gate merges on them; the repo's AI-review check gates.
3. Costs stay ledgered per run; spend cap enforced by env.
4. Verdicts and proofs link here; ephemeral `/tmp` proof files are not
   evidence — promote anything worth keeping to a PR comment.

## Gaps

1. No local-LLM spec-validation attempt on record — untried capability.
2. `/tmp/ollama-verdict.txt` already lost (ephemeral); future verdicts go
   to PR comments.
3. Comment-ID links above were spot-checked (2 of 3 resolve); full
   re-verification is per-use, not per-write.
