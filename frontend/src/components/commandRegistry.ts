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
}

// Every major action, grouped for the palette's hint chips. The base
// list alone clears the 10-command bar; per-repo switches append.
export function buildCommands(ctx: CommandContext): PaletteCommand[] {
  const base: PaletteCommand[] = [
    { id: 'go-now', label: 'Go to Now', hint: 'Navigate', keywords: 'triage inbox attention', run: () => ctx.go('now') },
    { id: 'go-chat', label: 'Go to Chat', hint: 'Navigate', keywords: 'agent ask prompt', run: () => ctx.go('chat') },
    { id: 'go-briefing', label: 'Go to Briefing', hint: 'Navigate', keywords: 'digest summary', run: () => ctx.go('briefing') },
    { id: 'go-history', label: 'Go to History', hint: 'Navigate', keywords: 'trail ledger past runs', run: () => ctx.go('history') },
    { id: 'open-review', label: 'Open Review section', hint: 'Now', keywords: 'prs issues merge', run: () => ctx.openSection('review') },
    { id: 'open-jobs', label: 'Open Jobs section', hint: 'Now', keywords: 'runs repair dispatch', run: () => ctx.openSection('jobs') },
    { id: 'report-bug', label: 'Report a bug', hint: 'Now', keywords: 'file issue create', run: () => ctx.reportBug() },
    { id: 'focus-chat', label: 'Focus chat prompt', hint: 'Chat', keywords: 'ask agent type prompt', run: () => ctx.focusChat() },
    { id: 'refresh-briefing', label: 'Refresh briefing', hint: 'Briefing', keywords: 'reload fetch latest', run: () => ctx.refreshBriefing() },
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

// Filtered registry, best matches first. Stable: ties keep registry
// order, and an empty query returns the registry untouched.
export function filterCommands(commands: PaletteCommand[], query: string): PaletteCommand[] {
  if (!query.trim()) return [...commands];
  const scored: Array<{ cmd: PaletteCommand; score: number; index: number }> = [];
  commands.forEach((cmd, index) => {
    const score = fuzzyScore(query, commandText(cmd));
    if (score !== null) scored.push({ cmd, score, index });
  });
  scored.sort((a, b) => b.score - a.score || a.index - b.index);
  return scored.map((s) => s.cmd);
}
