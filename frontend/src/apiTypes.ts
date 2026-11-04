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
  reviews_lines: string[];
  board: BoardItem[];
  board_lines: string[];
  board_err: string;
  conflicts_lines: string[];
  overlaps_lines: string[];
  failing_runs: Array<Record<string, unknown>>;
  workflow_lines: string[];
  runs_err: string;
  cached?: boolean;
  stale?: boolean;
  generated_at?: number;
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
}

export interface LedgerResponse {
  entries: LedgerEntry[];
}
