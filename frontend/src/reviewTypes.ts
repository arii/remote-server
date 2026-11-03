export interface RepoInfo {
  name: string;
  full: string;
  branch: string;
  clean: boolean;
}

export interface ReposResponse {
  repos: RepoInfo[];
}

export interface PrRow {
  number: number;
  title: string;
  author: string;
  branch: string;
  isDraft: boolean;
  ciStatus: string;
  ciLabel: string;
  reviewDecision: string;
  mergeable: string;
  url: string;
}

export interface PrListResponse {
  repo: string;
  prs: PrRow[];
}

export interface PrFileStat {
  path: string;
  additions: number;
  deletions: number;
  changes: number;
}

export interface AiPrompt {
  id: string;
  label: string;
  prompt: string;
}

export interface PrDetailsResponse {
  repo: string;
  number: number;
  title: string;
  branch: string;
  base: string;
  ci: string;
  failing: string[];
  merge: string;
  conflicted: boolean;
  draft: boolean;
  url: string;
  files: PrFileStat[];
  stats: { total_files: number; additions: number; deletions: number };
  ai_prompts: AiPrompt[];
}

export interface IssueRow {
  number: number;
  title: string;
  state?: string;
  assignees?: Array<{ login: string } | string>;
  labels?: Array<{ name: string } | string>;
  updatedAt?: string;
  url?: string;
  body?: string;
}

export interface IssueListResponse {
  repo: string;
  issues: IssueRow[];
}

export interface IssueDraftResponse {
  ok: boolean;
  title: string;
  body: string;
}

export interface DeferredRow {
  kind: 'pr' | 'issue';
  number: number;
  deferred_at?: number;
}

export interface DeferredListResponse {
  deferred: DeferredRow[];
}

// Merge relevance, mirroring the server gates (td evaluate_pr_merge_readiness
// for batch merge; pulls._pr_action for single merge). Buttons stay enabled
// when state is unknown — these gate only on known-bad state, fail open.
export function mergeBlockReason(p: PrRow): string | null {
  if (p.isDraft) return 'is a draft';
  if (p.reviewDecision !== 'APPROVED') return `review is ${p.reviewDecision || 'unapproved'}`;
  if (p.ciStatus !== 'passing') return `CI is ${p.ciStatus || 'unknown'}`;
  if (p.mergeable !== 'MERGEABLE') return `merge state is ${p.mergeable || 'unknown'}`;
  return null;
}

export function detailsMergeBlockReason(d: PrDetailsResponse): string | null {
  if (d.ci !== 'passing' || d.merge !== 'clean') {
    return `Not mergeable: CI ${d.ci}, merge ${d.merge}.`;
  }
  return null;
}
