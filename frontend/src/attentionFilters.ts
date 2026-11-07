import type { AttentionItem } from './attentionTypes';

// Non-required workflows never page the operator (issue #1109.1).
// Matched by GitHub workflow name: console-e2e runs in CI as
// "console E2E smoke (non-required)". If that gate flips to
// required, delete this list — do not extend it with more names.
const NON_REQUIRED_WORKFLOWS = [/console[- ]?e2e/i];

export function isNonRequiredWorkflowFailure(text: string): boolean {
  return NON_REQUIRED_WORKFLOWS.some((re) => re.test(text));
}

// Run-name slot: server lines read "- failing: {name} on {branch}
// ({event})…" and "Failing run: {name} on {branch} ({event}) —
// likely cause:…", item titles "Failing workflow: {name}". Match the
// name slot only — a branch or job cause that mentions console-e2e
// must never hide a required failure (a whole-line match would
// all-clear while required CI is red). Unprefixed text falls back
// to the whole string, i.e. fail-open.
const RUN_NAME_RE = /^(?:- failing:|Failing run:|Failing workflow:)\s*(.+?)\s+on\s+/;

export function failureNameSlot(text: string): string {
  const m = RUN_NAME_RE.exec((text || '').trimStart());
  return m ? m[1] : text;
}

// Now/attention feed: drop non-required workflow failures, keep
// everything else (approvals, failed jobs, required CI).
export function isActionableAttention(item: AttentionItem): boolean {
  if (item.kind === 'failing-run' && isNonRequiredWorkflowFailure(failureNameSlot(item.title)))
    return false;
  return true;
}

// Briefing string lines (attention + workflow_lines): same rule on
// the run-name slot, never the whole line.
export function isActionableLine(line: string): boolean {
  return !isNonRequiredWorkflowFailure(failureNameSlot(line));
}

// Briefing dedup (issue #1109.5): the server lists each failing run
// twice — once as a "Failing run:" attention line, once as a
// "- failing:" workflow line. The Failing runs section owns them
// (it carries the Investigate action); Needs attention keeps them
// only as a fallback when that section has no run data.
// Bracket-tag label derivation (issue #1109.8): cards whose title is
// blank (or a bare "Attention" chip) read back one tag from the body
// — "[CI] 2 failing: build…" titles as "CI". One tag's worth; deeper
// taxonomy stays out.
const BRACKET_TAG_RE = /^\s*\[([^\]\n]{1,24})\]/;

export function deriveCardLabels(label: string, lines: string[]): string {
  if (label && label.trim() !== '' && label !== 'Attention') return label;
  for (const line of lines || []) {
    const m = BRACKET_TAG_RE.exec(line || '');
    if (m) return m[1].trim();
  }
  return label;
}

export function dedupAttentionLines(attention: string[], workflowLines: string[]): string[] {
  const hasRunSection = workflowLines.some((line) => line.trimStart().startsWith('- failing:'));
  if (!hasRunSection) return attention;
  return attention.filter((line) => !line.trimStart().startsWith('Failing run:'));
}
