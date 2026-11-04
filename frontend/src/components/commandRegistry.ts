// Command palette action registry (issue #810): every major console
// action as a runnable { id, label, hint, run } entry, plus the fuzzy
// matcher the palette filters with.
export interface PaletteCommand {
  id: string;
  label: string;
  // Group chip shown beside the label (Navigate, Now, Repo, Tools).
  hint: string;
  // Extra match text (synonyms, view names) beyond label + hint.
  keywords?: string;
  run: () => void;
}

export type PaletteTab = 'now' | 'chat' | 'briefing' | 'history';

export interface CommandContext {
  go: (tab: PaletteTab) => void;
  openSection: (section: 'review' | 'jobs') => void;
  reportBug: () => void;
  focusChat: () => void;
  refreshBriefing: () => void;
  switchRepo: (name: string) => void;
  openUrl: (url: string) => void;
  terminalUrl: string;
  codeUrl: string;
  repos: Array<{ name: string }>;
  repo: string;
  // Operator verbs (issue #839), optional so older wirings keep
  // compiling: outbox sync, container stats drawer, new-issue form.
  syncNow?: () => void;
  openStats?: () => void;
  newIssue?: () => void;
}

// Palette sections (issue #839): the hint chips group into four
// headed sections instead of one flat list.
export const PALETTE_SECTIONS = ['Navigation', 'Actions', 'Repositories', 'System'] as const;

export type PaletteSection = (typeof PALETTE_SECTIONS)[number];

export function commandSection(cmd: PaletteCommand): PaletteSection {
  switch (cmd.hint) {
    case 'Navigate':
      return 'Navigation';
    case 'Repo':
      return 'Repositories';
    case 'Tools':
    case 'System':
      return 'System';
    default:
      return 'Actions';
  }
}

// Every major action, grouped for the palette's hint chips. The base
// list alone clears the 10-command bar; per-repo switches append.
export function buildCommands(ctx: CommandContext): PaletteCommand[] {
  const base: PaletteCommand[] = [
    { id: 'go-now', label: 'Go to Now', hint: 'Navigate', keywords: 'triage inbox attention', run: () => ctx.go('now') },
    { id: 'go-chat', label: 'Go to Chat', hint: 'Navigate', keywords: 'agent ask prompt', run: () => ctx.go('chat') },
    { id: 'go-briefing', label: 'Go to Briefing', hint: 'Navigate', keywords: 'digest summary', run: () => ctx.go('briefing') },
    { id: 'go-history', label: 'Go to History', hint: 'Navigate', keywords: 'trail ledger past runs', run: () => ctx.go('history') },
    { id: 'open-review', label: 'Open Review section', hint: 'Now', keywords: 'prs issues merge pr status', run: () => ctx.openSection('review') },
    { id: 'open-jobs', label: 'Open Jobs section', hint: 'Now', keywords: 'runs repair dispatch', run: () => ctx.openSection('jobs') },
    { id: 'report-bug', label: 'Report a bug', hint: 'Now', keywords: 'file issue create', run: () => ctx.reportBug() },
    {
      id: 'new-issue',
      label: 'New issue…',
      hint: 'Now',
      keywords: 'create file issue draft triage',
      run: () => {
        ctx.openSection('review');
        ctx.newIssue?.();
      },
    },
    { id: 'focus-chat', label: 'Focus chat prompt', hint: 'Chat', keywords: 'ask agent type prompt run prompt', run: () => ctx.focusChat() },
    { id: 'refresh-briefing', label: 'Refresh briefing', hint: 'Briefing', keywords: 'reload fetch latest', run: () => ctx.refreshBriefing() },
    { id: 'sync-now', label: 'Sync now', hint: 'System', keywords: 'flush outbox upload retry offline queue', run: () => ctx.syncNow?.() },
    { id: 'open-stats', label: 'Container stats', hint: 'System', keywords: 'restart container health cpu ram docker', run: () => ctx.openStats?.() },
    { id: 'open-terminal', label: 'Open Terminal', hint: 'Tools', keywords: 'shell console ssh', run: () => ctx.openUrl(ctx.terminalUrl) },
    { id: 'open-code', label: 'Open Code editor', hint: 'Tools', keywords: 'vscode edit files', run: () => ctx.openUrl(ctx.codeUrl) },
  ];
  for (const r of ctx.repos) {
    if (r.name === ctx.repo) continue;
    base.push({
      id: `switch-repo-${r.name}`,
      label: `Switch to ${r.name}`,
      hint: 'Repo',
      keywords: 'checkout repository change',
      run: () => ctx.switchRepo(r.name),
    });
  }
  return base;
}

// Fuzzy subsequence score, case-insensitive. Word starts outrank
// consecutive runs, which outrank scattered letters; null = no match.
// An empty query matches everything with score 0 (registry order kept).
export function fuzzyScore(query: string, text: string): number | null {
  const q = query.toLowerCase().replace(/\s+/g, '');
  if (!q) return 0;
  const t = text.toLowerCase();
  let score = 0;
  let ti = 0;
  let lastMatch = -2;
  for (let qi = 0; qi < q.length; qi++) {
    const found = t.indexOf(q[qi], ti);
    if (found === -1) return null;
    if (found === 0 || /[\s\-_/:]/.test(t[found - 1])) score += 3;
    else if (found === lastMatch + 1) score += 2;
    else score += 1;
    lastMatch = found;
    ti = found + 1;
  }
  return score;
}

function commandText(cmd: PaletteCommand): string {
  return `${cmd.label} ${cmd.hint} ${cmd.keywords ?? ''}`;
}

// Prefix sub-modes (issue #839): a leading sigil narrows the pool
// before the fuzzy match runs. `>` actions (non-navigation), `@`
// repos, `#` review (PRs/issues/merges), `/` chat prompts.
export type PalettePrefix = 'all' | 'actions' | 'repos' | 'review' | 'chat';

export interface PaletteQuery {
  mode: PalettePrefix;
  rest: string;
}

export function parsePalettePrefix(query: string): PaletteQuery {
  const first = query.charAt(0);
  if (first === '>') return { mode: 'actions', rest: query.slice(1) };
  if (first === '@') return { mode: 'repos', rest: query.slice(1) };
  if (first === '#') return { mode: 'review', rest: query.slice(1) };
  if (first === '/') return { mode: 'chat', rest: query.slice(1) };
  return { mode: 'all', rest: query };
}

const REVIEW_MODE_RE = /\bprs?\b|issue|review|merge|defer|approve/;
const CHAT_MODE_RE = /chat|prompt|template|agent|send|ask/;

export function inPrefixMode(cmd: PaletteCommand, mode: PalettePrefix): boolean {
  if (mode === 'all') return true;
  if (mode === 'actions') return cmd.hint !== 'Navigate';
  if (mode === 'repos') return cmd.hint === 'Repo';
  const text = commandText(cmd).toLowerCase();
  if (mode === 'review') return REVIEW_MODE_RE.test(text);
  return CHAT_MODE_RE.test(text);
}

// Filtered registry, best matches first. Stable: ties keep registry
// order, and an empty query returns the registry untouched.
export function filterCommands(commands: PaletteCommand[], query: string): PaletteCommand[] {
  const { mode, rest } = parsePalettePrefix(query.trim());
  const pool = mode === 'all' ? commands : commands.filter((cmd) => inPrefixMode(cmd, mode));
  if (!rest.trim()) return [...pool];
  const scored: Array<{ cmd: PaletteCommand; score: number; index: number }> = [];
  pool.forEach((cmd, index) => {
    const score = fuzzyScore(rest, commandText(cmd));
    if (score !== null) scored.push({ cmd, score, index });
  });
  scored.sort((a, b) => b.score - a.score || a.index - b.index);
  return scored.map((s) => s.cmd);
}
