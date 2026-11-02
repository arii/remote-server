export interface BriefingResponse {
  repo: string;
  summary?: string;
  open_prs?: Array<Record<string, unknown>>;
  ci?: Record<string, unknown>;
  error?: string;
}

export interface JobRecord {
  id: string;
  title: string;
  status: string;
  done?: boolean;
}

export interface JobsResponse {
  jobs: JobRecord[];
}

export interface LedgerEntry {
  kind: string;
  repo?: string;
  event?: string;
  at?: number | string;
}

export interface LedgerResponse {
  entries: LedgerEntry[];
}
