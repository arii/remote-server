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
  // #848: false when GitHub reports no review decision, meaning no
  // required-review rule is in play — the merge gate must not demand
  // an approval then. Missing (older payloads, fixtures) means strict.
  reviewRequired?: boolean;
}

export interface PrListResponse {
  repo: string;
  prs: PrRow[];
  // #848: login gh acts as; blank/unknown when the backend could not
  // determine it (Approve stays enabled, endpoint 400 is fallback).
  viewer?: string;
}

interface PrFileStat {
  path: string;
  additions: number;
  deletions: number;
  changes: number;
}

interface AiPrompt {
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
// Sync guard (#553): mergeReadiness.fixtures.json pins this predicate to the
// backend port (inbox.merge_block_reason) via exact-equality tests on both
// sides — update the table with any semantic change.
export function mergeBlockReason(p: PrRow): string | null {
  if (p.isDraft) return 'is a draft';
  // #848: only demand an approval the target repo actually requires.
  // A missing reviewRequired (fixtures, older payloads) stays strict
  // so the #553 cross-language pin still holds; false with no
  // negative review signal fails open. An explicit CHANGES_REQUESTED
  // — or GitHub's own REVIEW_REQUIRED — still blocks either way.
  const reviewRequired = p.reviewRequired ?? true;
  if (p.reviewDecision !== 'APPROVED') {
    if (reviewRequired) return `review is ${p.reviewDecision || 'unapproved'}`;
    if (p.reviewDecision === 'CHANGES_REQUESTED' || p.reviewDecision === 'REVIEW_REQUIRED') {
      return `review is ${p.reviewDecision}`;
    }
  }
  if (p.ciStatus !== 'passing') return `CI is ${p.ciStatus || 'unknown'}`;
  if (p.mergeable !== 'MERGEABLE') return `merge state is ${p.mergeable || 'unknown'}`;
  return null;
}

// Approve relevance, decided per merge-block reason (#616):
// drafts stay disabled (approving unfinished work is premature);
// an already-approved PR stays disabled (no duplicate-approval
// noise). The CI and merge-state reasons do NOT apply — a review
// is valid whatever the checks say. Request-changes is never
// row-disabled: any open PR, draft or not, can take feedback.
// #848: the console user cannot approve their own PR (GitHub rejects
// with "Can not approve your own pull request"). The backend reports
// its gh login as PrListResponse.viewer; a matching author means the
// Approve tap would fail, so the chip disables with the reason inline.
export function isOwnPr(p: PrRow, viewer?: string): boolean {
  if (!viewer || !p.author) return false;
  return p.author.toLowerCase() === viewer.toLowerCase();
}

export function approveBlockReason(p: PrRow, viewer?: string): string | null {
  if (p.isDraft) return 'is a draft';
  if (p.reviewDecision === 'APPROVED') return 'already approved';
  if (isOwnPr(p, viewer)) return 'you authored this PR — GitHub forbids self-approval';
  return null;
}

export function detailsMergeBlockReason(d: PrDetailsResponse): string | null {
  if (d.ci !== 'passing' || d.merge !== 'clean') {
    return `Not mergeable: CI ${d.ci}, merge ${d.merge}.`;
  }
  return null;
}
