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
