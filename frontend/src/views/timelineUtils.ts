import type { AgentRun } from '../apiClient';
import type { JobRecord, LedgerEntry } from '../apiTypes';
import type { HistoryRow } from './historyUtils';
import { epochOf, rowTime, sentenceCase, toHistoryRows } from './historyUtils';

// Unified activity timeline (issue #812): one chronological feed
// merging the four existing surfaces — job runs (Jobs), agent
// turns (Chat Past runs), webhook pulls, and merges — client-side.
// No new endpoint: each feed keeps its own fetch, and this module
// only interleaves them newest-first with one-line summaries.

type TimelineFeed = 'run' | 'turn' | 'pull' | 'merge';

export interface TimelineItem {
  key: string;
  feed: TimelineFeed;
  summary: string;
  at: string;
  rawAt: number;
  tone: HistoryRow['tone'];
  detail: string;
}

// Runs, pulls, and merges render through the History summarizer so
// the timeline reads exactly like the History rows it mirrors
// (same plain-language summaries, same failure tones, same detail
// blocks). Single-entry calls never group, so each feed row maps
// to exactly one timeline row.
function historyItem(
  feed: TimelineFeed,
  key: string,
  jobs: JobRecord[],
  entries: LedgerEntry[],
  rawAt: number,
): TimelineItem {
  const row = toHistoryRows(jobs, entries)[0];
  return {
    key,
    feed,
    summary: row.summary,
    at: row.at,
    rawAt,
    tone: row.tone,
    detail: row.detail,
  };
}

function runItem(j: JobRecord): TimelineItem {
  return historyItem('run', `run-${j.id}`, [j], [], epochOf(j.at ?? j.created_at));
}

function ledgerItem(feed: 'pull' | 'merge', e: LedgerEntry, index: number): TimelineItem {
  return historyItem(feed, `${feed}-${index}`, [], [e], epochOf(e.ts ?? e.at));
}

// Per-turn cost slot, same vocabulary as ChatView's run line:
// local turns are free, unrated metered turns stay an honest bare
// tag instead of an invented number.
function turnCost(t: AgentRun): string {
  if (!t.metered) return 'free';
  if (t.cost_usd === null || t.cost_usd === undefined) return 'metered';
  return `$${t.cost_usd.toFixed(4).replace(/\.?0+$/, '')}`;
}

function turnItem(t: AgentRun): TimelineItem {
  const parts = [t.script, t.model].filter((v) => !!v);
  parts.push(t.ok ? 'ok' : 'FAILED');
  if (typeof t.duration_s === 'number') parts.push(`${t.duration_s}s`);
  parts.push(`${t.tokens} tok`, turnCost(t));
  const detail = [`turn: ${t.script}`, `thread: ${t.thread}`, t.preview.split('\n')[0].trim()]
    .filter((v) => !!v)
    .join('\n');
  return {
    key: `turn-${t.id}`,
    feed: 'turn',
    summary: sentenceCase(parts.join(' · ')),
    at: rowTime(t.started_ts || undefined),
    rawAt: epochOf(t.started_ts || undefined),
    tone: t.ok ? 'ok' : 'bad',
    detail,
  };
}

/**
 * Merge runs + turns + pulls + merges into flat display rows,
 * newest first. Flat on purpose: unlike History's incident
 * collapse, every event keeps its own row so the interleave is
 * exact. Ageless rows sort last with an 'unknown time' label.
 */
export function toTimelineItems(
  jobs: JobRecord[],
  turns: AgentRun[],
  pulls: LedgerEntry[],
  merges: LedgerEntry[],
): TimelineItem[] {
  const items: TimelineItem[] = [
    ...jobs.map(runItem),
    ...turns.map(turnItem),
    ...pulls.map((e, i) => ledgerItem('pull', e, i)),
    ...merges.map((e, i) => ledgerItem('merge', e, i)),
  ];
  items.sort((a, b) => b.rawAt - a.rawAt);
  return items;
}
