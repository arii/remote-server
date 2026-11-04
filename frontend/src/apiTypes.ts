export interface BoardItem {
  branch?: string;
  issue?: string;
  status?: string;
}

export interface BriefingResponse {
  repo: string;
  attention: string[];
  prs: Array<Record<string, unknown>>;
  prs_err: string;
  enrichment_note?: string;
  enrichment_total?: number;
  enrichment_shown?: number;
  enrichment_truncated?: boolean;
  reviews_lines: string[];
  reviews_err: Record<string, string>;
  board: BoardItem[];
  board_lines: string[];
  board_err: string;
  conflicts_lines: string[];
  conf_err: string;
  overlaps_lines: string[];
  failing_runs: Array<Record<string, unknown>>;
  workflow_lines: string[];
  runs_err: string;
  cached?: boolean;
  stale?: boolean;
  generated_at?: number;
  narrative?: string | null;
  narrative_at?: number | null;
}

export interface JobRecord {
  id: string;
  title?: string;
  name?: string;
  status?: string;
  state?: string;
  kind?: string;
  repo?: string;
  done?: boolean;
  exit_code?: number | null;
  error?: string;
  detail?: string;
  at?: number;
  created_at?: number;
  // Recorded td argv for run-td rows ([] for plain-action rows):
  // replay params for one-click re-run.
  td_argv?: string[];
}

export interface JobsResponse {
  jobs: JobRecord[];
}

// Cross-repo lane dispatch queue (issue #853): one item per
// (repo, issue), mirroring the backend DispatchItem shape.
export interface DispatchItem {
  repo: string;
  issue: number;
  state: string;
  added_at?: number | null;
  started_at?: number | null;
  finished_at?: number | null;
  detail?: string;
}

export interface DispatchQueueResponse {
  items: DispatchItem[];
}

export interface DispatchGatesResponse {
  repo: string;
  issue: number;
  ready: boolean;
  reasons: string[];
  gates: Record<string, unknown>;
}

export interface LedgerEntry {
  kind: string;
  repo?: string;
  event?: string;
  action?: string;
  at?: number | string;
  ts?: number;
  ok?: boolean;
  status?: string;
  error?: string;
  detail?: string;
  session_id?: string;
  exit?: number;
  elapsed?: number;
  // Full-field extras the backend writes on some kinds: chat entries
  // carry the session prompt, run entries the td command + elapsed_s,
  // agent-turn rows the model provider.
  prompt?: string;
  command?: string;
  elapsed_s?: number;
  provider?: string;
  // Normalized cli-turn fields from the deep-agent ledger (#590).
  script?: string;
  model?: string;
  thread_id?: string;
  duration_s?: number;
  usage?: { input_tokens?: number; output_tokens?: number; total_tokens?: number } | null;
  cost_usd?: number | null;
  metered?: boolean | null;
  tools?: string[];
}

export interface LedgerResponse {
  entries: LedgerEntry[];
}
