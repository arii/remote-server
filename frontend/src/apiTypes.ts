export interface BriefingResponse {
  repo: string;
  summary?: string;
  open_prs?: Array<Record<string, unknown>>;
  ci?: Record<string, unknown>;
  error?: string;
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
}

export interface LedgerResponse {
  entries: LedgerEntry[];
}
