---
target: S5 control panel /buttons
total_score: 11
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 2
target_identity: "url:https://dev-onasafari.ddns.net:8443/buttons"
timestamp: 2026-11-02T20-31-44Z
slug: dev-onasafari-ddns-net-buttons
---
# Critique: S5 Control Panel (/buttons) — 2026-11-02

Method: dual-agent (A: 01a19df4-7ae0-7d62-a7af-77f360014219 · B: 01a19df4-7bb8-70d3-8432-83357c6126df)
Target: https://dev-onasafari.ddns.net:8443/buttons (source buttons/server.py + buttons/ux/)
Mode: Operate. Heuristics total: 11/40 (all 10 scored). P0: 1, P1: 2, P2: 2.

## Heuristic scores

| # | Heuristic | Score | Key Issue |
|---|---|---|---|
| 1 | Visibility of System Status | 1 | Bare "Loading…" spinners; failed-job steps hidden behind "Show steps" |
| 2 | Match System / Real World | 1 | Raw git stderr as copy; unexplained td/briefing/preflight jargon |
| 3 | User Control and Freedom | 2 | Destructive actions beside read-only with no undo/dry-run |
| 4 | Consistency and Standards | 1 | Duplicated Report bug; 4 refresh verbs; filter/tab divergence |
| 5 | Error Prevention | 1 | One-tap merge/recreate/deploy/clear, parenthetical warnings only |
| 6 | Recognition Rather Than Recall | 1 | Repo-from-picker-above recall; actions hidden in collapsed details |
| 7 | Flexibility and Efficiency | 1 | No shortcuts/palette; one-at-a-time dispatch; per-section refresh |
| 8 | Aesthetic and Minimalist Design | 1 | Actions tab vertical stack of everything; no primary action |
| 9 | Error Recovery | 1 | ~30 FAILED webhook rows, zero suggested next steps; "unknown entry" |
| 10 | Help and Documentation | 1 | Only parenthetical doc pointers; no contextual help |

## Design-specificity verdict

LLM: category-interchangeable ops console; could be relabeled for any side project. No "needs me now" triage object; six tools sharing a header. Fixed-actions-only claim contradicted by three free-text inputs.
Detector: CLI scan clean (0 findings — server-rendered, no markup files to scan; not evidence of quality). Overlay injection failed: live-server on container localhost unreachable from paired laptop Chrome (topology limit). Page APIs all 200.

## Overall impression

Opens reassuring ("No failing runs ✓"), slides into anxiety: decision-dread Actions wall, opaque failed Jobs, Ledger valley of 30 red git-stderr rows with no recovery. Ends on "on fire, won't tell me what to do."

## What's working

1. Briefing tab calm empty states — the model for the rest.
2. Fixed-actions-only + approval-gated jobs — right phone-ops posture.
3. Ledger backbone (chronological, Kind-filtered) — needs plain rows + recovery links.

## Priority issues

- [P0] Ledger renders raw git stderr (~30 FAILED rows) + "unknown entry" with no next step. Fix: collapse repeats into incident rows with one recovery button; never render "unknown entry". ($impeccable clarify)
- [P1] No cross-tab "needs attention" triage; six tabs each with own refresh. Fix: top-of-page attention queue, tabs as drill-down. ($impeccable layout)
- [P1] Destructive actions same-weight as read-only, no confirm. Fix: two-tap/type-to-confirm + dry-run + visual demotion. ($impeccable harden)
- [P2] Fragile active-repo context gates every button. Fix: restate repo per action group + sticky repo chip. ($impeccable clarify)
- [P2] Project tab = 7 stacked tools, no primary action. Fix: one primary section per state, collapse rest, single refresh. ($impeccable distill)

## Persona red flags

- Alex (power): no shortcuts/palette/bulk; per-section refresh; will defect to terminal.
- Sam (a11y): div-based tabs/cards, char-split text nodes, unlabeled icon buttons, color-only status, unannounced loaders; keyboard triage not credible.
- Casey (mobile): destructive mid-scroll, tiny tap targets, loaders restart per tab, typing where selection would do, 40-row Ledger scroll.

## Minor observations

Duplicate Report bug; td/briefing-td verb soup; Jobs badge counts history as work; dead NTFY Test push/Send digest; per-tab polling; chat drawer occludes content on narrow phones.

## Questions to consider

- One "needs attention" screen + drill-down instead of six tabs?
- Two tabs ("Now", "History") instead of six?
- Every red row ships with its fix button?
