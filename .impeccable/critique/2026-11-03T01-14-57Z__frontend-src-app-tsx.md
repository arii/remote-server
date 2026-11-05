---
target: S5 console SPA
total_score: 14
max_score: 40
na_heuristics: 
p0_count: 1
p1_count: 3
target_identity: "file:/srv/remote-server/frontend/src/App.tsx"
target_fingerprint: "sha256:06bf86bed0d16e99f020842766c464f4fb086c5cda0849309215fe450f316052"
target_path: /srv/remote-server/frontend/src/App.tsx
timestamp: 2026-11-03T01-14-57Z
slug: frontend-src-app-tsx
---
# Critique: S5 console SPA (dark rewrite) — 2026-11-03

Method: dual-agent (A: 01a19ee3-b2ba-74d3-b986-3e56ae1f0180 · B: 01a19ee3-b3c4-7d51-8c63-1ed0e18a05a8)
Target: frontend/src/App.tsx (live: http://172.18.0.7:5173/, served from program/ux3-parity worktree ==
origin/main content at audit time)
Mode: Operate. Heuristics total: 14/40 (all 10 scored). P0: 1, P1: 3, P2: 1.
Legacy /buttons baseline (2026-11-02): 11/40 — the rewrite gains +3 (hold-gating, consistency,
minimalism) but inherits the same trust-critical feedback gaps.

## Heuristic scores

| # | Heuristic | Score | Key Issue |
|---|---|---|---|
| 1 | Visibility of System Status | 1 | Briefing loads silently >2s; hold progress 0px; absolute-time-only freshness |
| 2 | Match System / Real World | 1 | UI speaks backend: `run-td`, `- fix/…`, `Chat session activity ×6`, `chat rejectd` typo |
| 3 | User Control and Freedom | 2 | Abort/cancel ok; no undo; ambiguous Clear-jobs-vs-Clear-all |
| 4 | Consistency and Standards | 2 | One card language, but hold ≡ normal buttons; Chat entry duplicated; history rows unstyled |
| 5 | Error Prevention | 2 | Hold-gating exists; free-text assignee already caused `user 'ghost' does not exist` |
| 6 | Recognition Rather Than Recall | 1 | 12 undescribed job codes; recreate-vs-recreate-code; Clear-vs-Clear-all |
| 7 | Flexibility and Efficiency | 1 | No shortcuts/bulk/deep-links; manual Refresh everywhere; 15 assign inputs to tab through |
| 8 | Aesthetic and Minimalist Design | 2 | Now is calm; Review/History carry dead weight (assignee inputs, chat chatter rows) |
| 9 | Error Recovery | 1 | Raw `{error}` interpolation; robotic "How to recover: check status and retry" ×~10 |
| 10 | Help and Documentation | 1 | Zero in-app explanation for Take-over-terminal, warm-context, Draft-from-finding, job codes |
| **Total** | | **14/40** | **Poor — trust-critical feedback missing** |

## Design Specificity Verdict

**Category-interchangeable.** A competent generic dark admin template, not an authored operate-console:
no product name/wordmark (tab title "remote-server" only), system-ui + blue-accent cards could be any
dashboard, nothing expresses "Ari, night, home-server, calm triage" except two copy lines. The single
most product-specific mechanism, hold-to-confirm, is visually nonexistent. Could be mistaken for a CRUD
scaffold.

**Deterministic scan**: CLI `impeccable detect` on source: 0 findings (regex-only on .ts/.tsx, expected
undercount). Browser overlay on all 6 live views: 30 element findings — pressed-nav contrast 3.7:1
(independently verified 3.68, genuinely < 4.5:1), input contrast 3.65:1, flat type hierarchy (1.15–1.18:1
steps vs 1.25 target), Review column overflow (290% vs 34% viewport), 15× low-contrast assignee inputs.
Zero false positives proven. Overlay pixels lag view changes by one view (scan API is current).

**Agreement**: detector corroborates A on assignee-input dead weight, pressed-state/distinction failures,
and type-hierarchy weakness. A caught what the detector cannot: invisible hold feedback, backend-ese copy,
IA overload, undiscoverable approvals.

**Visual overlays**: injection succeeded via LAN forwarder (live-server is loopback-only, laptop Chrome is
remote); overlay shown during run, page reloaded and tab closed after — no overlay persists.

## Overall Impression

The architecture is right (hold-gating, approval cards, calm Now) but the rendering betrays it: the safety
mechanism looks broken, machine vocabulary leaks everywhere, and the core triage promise has a hole —
approvals surface only mid-stream inside Chat, nothing on Now/nav tells phone-Ari one is waiting. Biggest
opportunity: make the hold control visibly, physically trustworthy, then delete the single-operator dead
weight strangling Review.

## What's Working

1. Now view nails the core promise: one-glance all-clear ("Nothing needs you right now" + 8/8 pills is the
product's best moment), stack pills, bug reports prefilled with repo+status context.
2. Coherent calm dark system: consistent cards, `:focus-visible`, keyboard hold path, reduced-motion handling.
3. Hold-to-confirm + `{intent, confirmed}` and in-chat approval cards are the right architecture — shape
correct, rendering not.

## Priority Issues

- **[P0] Invisible hold-to-confirm (trust-critical).**
What: 0px progress element (measured 0×19px, no CSS), destructive buttons pixel-identical to safe ones
(same surface-2 bg/border), armed state is label-text-only; pressed-state contrast 3.7:1 < 4.5:1.
Why it matters: Ari cannot tell arm from fire on a destructive phone gesture; the safety mechanism feels broken.
Fix: visible fill bar driven by `data-progress`, red/warn destructive variant, persistent "hold 1s to arm"
hint, one-line consequence text per action ("restarts all 8 containers"), contrast-safe pressed state.
Suggested command: $impeccable harden
- **[P1] Backend-ese rendered as UI.**
What: raw `- …` briefing lines, `×N` collapse-ese, `rejectd` typo (×2, live), repeated generic recovery copy.
Why it matters: forces Ari to parse machine output at night.
Fix: human summary layer, relative times ("2m ago"), collapse routine chatter by default, fix typo at source.
Suggested command: $impeccable clarify
- **[P1] Review view overload.**
What: 15 input+button assignee pairs, 5 simultaneous foci (PRs+issues+details+create+draft); detector: 21
findings, column overflow 290% vs 34%.
Why it matters: single-operator dead weight buries the 2 PRs that matter (live issue #259 agrees: cut assignee inputs).
Fix: delete assignee inputs, compact issue rows, disclose create/draft forms behind buttons, fix columns.
Suggested command: $impeccable distill
- **[P1] Jobs dispatch wall.**
What: 12 undescribed codes sharing a bar with 3 adjacent destructive holds; one mis-tap from `health` to
`recreate-code` with no descriptions or separation.
Why it matters: catastrophic mis-tap proximity with zero recall support.
Fix: group with one-line descriptions (Observe / Refresh / Danger), move Clear/Takeover to a separated
danger zone with consequence text.
Suggested command: $impeccable layout
- **[P2] History noise + inconsistency.**
What: chat lifecycle spam buries signal; rows lack `.Detail` cards; repeats listed, not truly collapsed.
Why it matters: "never wonder what the automation did" fails when the log is unreadable.
Fix: default-collapse routine rows, card the rows, one row per incident with count, targeted recovery actions.
Suggested command: $impeccable distill

## Persona Red Flags

**Ari-on-phone-triage**: toolbar Terminal/Code links 21px tall (nav buttons 40px, under 44px); hold-with-no-feedback
on touch ("did it register?"); ~10 persistent header/nav choices; absolute timestamps force night mental math;
Briefing slow-load with no skeleton; approval-waiting undiscoverable from Now.
**Ari-on-laptop-review**: PR "details" = file stats + prompts, no diff — cannot actually review; PR data duplicated
yet inconsistent between Review and Briefing; 15-issue scroll; no keyboard flow, no deep links, manual refresh.

## Minor Observations

Repo chip duplicates picker value; `0 approved job(s)` grammar; toolbar Chat button duplicates nav Chat; "Details
(optional)" arrives prefilled; selected PR resets on repo switch; Mem 70% gets no warn color though `--warn` exists;
success notices too subtle; no skip link; nav uses `aria-pressed` buttons instead of tablist semantics; detector
severity asymmetry on identical-color inputs (engine-internal, unprovable from observables).

## Cognitive load: HIGH (7 of 8 checklist items fail)

Single focus fails on 5 of 6 views; chunking fails (15 issue cards, dozens of history rows, 12 job actions);
briefing cards create false importance equality; severity is text-only, never visual; Jobs bar fires 5 actions at
once; decision-point counts: nav 6 + toolbar ≈ 10 persistent, Jobs select 12 + 4 buttons, Review 15×2. Working
memory: job-code meanings, Clear-vs-Clear-all scope, run-meta vocabulary all demand recall. Disclosure poor except
PR-details-on-click and run-trace-on-click.

## Emotional journey

Peak: the all-clear. End: every flow ends weakly (tiny muted "Done: …"). Valleys: Briefing's bare "Loading…" (>2s);
History's alarming wall with no recourse; the `rejectd` typo erodes trust. High-stakes reassurance: absent — hold
feels broken, consequence text nowhere, approvals undiscoverable from Now.

## Questions to Consider

1. If only human decisions should surface, why does Ari ever see `warm-context`, `td` codes, or token counts?
2. What does "Clear all" destroy that "Clear jobs" spares — and why must that be a memory test at 2am?
3. If an approval arrives while Ari is on Now, what — precisely, pixel by pixel — tells her?
4. Which view would Ari open to *read a diff*, and why is the answer "none of them"?
5. If Jules is dead, why do assignee inputs, `jules` labels, and Jules-flavored copy still dominate Review?
